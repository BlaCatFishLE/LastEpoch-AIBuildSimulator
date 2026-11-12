"""通用贡献映射与属性归一化（统一 DPS 第一步 · S2/S3）。

本模块把 ``calculation-input-v1`` 的词条、被动天赋行目录数据整理成
:class:`le_calc.model.Contribution`，只表达**属性语义**：

* 不判断某个技能命中模型/组件是否使用该行——适用性由调用方按显式策略处理
  （DoT-only 保留或排除、近战/法术标签筛选等）；
* 不改写共享 ``model.Contribution`` 契约，也不发明第二种贡献对象；
* 历史 ``hit_inputs`` 通过 :class:`MappingPolicy` 的兼容字段委托到这里，
  以保持 Flame Reave 命中模型的支持范围不变（见 ``hit_inputs.map_mod``）。

扩展 stat 词表（仅新 dps 路径启用，历史 hit 路径保持原词表）：

``movement_speed`` / ``effect_of`` / ``damage_taken`` / ``ailment_chance`` /
``mana_cost`` / ``skill_level`` / ``state_marker``
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import model
from .condition_codes import DEFAULT_HIT_STATES, evaluate_condition

# ---------------------------------------------------------------------------
# coreDB property ids（名称对照 dataset stats 表；与 hit_inputs 保持同一份数据语义）
# ---------------------------------------------------------------------------

PROP_DAMAGE = 0
PROP_AILMENT_CHANCE = 1
PROP_ATTACK_SPEED = 2
PROP_CAST_SPEED = 3
PROP_CRIT_CHANCE = 4
PROP_CRIT_MULTIPLIER = 5
PROP_DAMAGE_TAKEN = 6
PROP_HEALTH = 7
PROP_MANA = 8
PROP_MOVEMENT_SPEED = 9
PROP_ARMOR = 10
PROP_STUN_AVOIDANCE = 12
PROP_FIRE_RESISTANCE = 13
PROP_HEALTH_REGEN = 17
PROP_MANA_REGEN = 18
PROP_STRENGTH = 19
PROP_VITALITY = 20
PROP_INTELLIGENCE = 21
PROP_DEXTERITY = 22
PROP_ATTUNEMENT = 23
PROP_VOID_RESISTANCE = 26
PROP_BLOCK_CHANCE = 29
PROP_WARD = 39
PROP_ALL_ATTRIBUTES = 46
PROP_ELEMENTAL_RESISTANCE = 52
PROP_BLOCK_EFFECTIVENESS = 53
PROP_STUN_IMMUNITY = 56
PROP_PENETRATION = 59
PROP_PHYSICAL_RESISTANCE = 64
PROP_MANA_COST = 66
PROP_COOLDOWN_RECOVERY = 70
PROP_STUN_DURATION_RECEIVED = 71
PROP_ENDURANCE_THRESHOLD = 76
PROP_SKILL_LEVEL = 88
PROP_DAMAGE_TAKEN_NEARBY = 110
PROP_BONUS_CRIT_DAMAGE_TAKEN = 114
PROP_AREA = 116
PROP_EFFECT_OF = 120
PROP_PARRY_CHANCE = 121
PROP_IDOL_REFRACTION = 130

ATTRIBUTE_PROPERTIES: dict[int, str] = {
    PROP_STRENGTH: "strength",
    PROP_VITALITY: "vitality",
    PROP_INTELLIGENCE: "intelligence",
    PROP_DEXTERITY: "dexterity",
    PROP_ATTUNEMENT: "attunement",
}

RESISTANCE_BY_NAME: dict[str, str] = {
    "fire resistance": "fire",
    "cold resistance": "cold",
    "lightning resistance": "lightning",
    "physical resistance": "physical",
    "void resistance": "void",
    "necrotic resistance": "necrotic",
    "poison resistance": "poison",
    "elemental resistance": "elemental",
    "all resistances": "all",
}

DAMAGE_TYPE_TAGS: tuple[str, ...] = (
    "Physical",
    "Fire",
    "Cold",
    "Lightning",
    "Void",
    "Necrotic",
    "Poison",
)

#: 新 dps 路径启用的扩展 stat_key（历史 hit 路径不产生这些行）
STAT_MOVEMENT_SPEED = "movement_speed"
STAT_EFFECT_OF = "effect_of"
STAT_DAMAGE_TAKEN = "damage_taken"
STAT_SKILL_LEVEL = "skill_level"
STAT_STATE_MARKER = "state_marker"

#: 覆盖分类：决定未映射行是否构成“可能影响当前伤害”的缺口
COVERAGE_DEFENSIVE = "defensive"
COVERAGE_UTILITY = "utility"
COVERAGE_STATE_MARKER = "state_marker"
COVERAGE_SKILL_LEVEL = "skill_level"
COVERAGE_DECLARED = "declared_assumption"
COVERAGE_DAMAGE_RELEVANT = "damage_relevant_unknown"
COVERAGE_OUT_OF_SCOPE = "out_of_scope"
COVERAGE_IDOL_REFRACTION = "idol_refraction"

#: 只有这些分类进入覆盖缺口（其余视为已确认无关或已声明不模拟）
COVERAGE_GAP_CLASSES = frozenset(
    {COVERAGE_DAMAGE_RELEVANT, COVERAGE_IDOL_REFRACTION, COVERAGE_OUT_OF_SCOPE}
)

DEFENSIVE_TOKENS = (
    "armor", "health", "ward", "endurance", "dodge", "stun", "leech",
    "resistance", "regen", "healing", "potion", "block", "parry",
    "immunity", "threshold", "avoidance", "mitigation", "damage taken",
    "taken", "survival", "life", "take 0 damage", "when hit",
)
UTILITY_TOKENS = (
    "movement skill", "area", "range", "chill", "shock", "ignite", "freeze",
    "slow", "blind", "frailty", "shred", "experience", "cooldown",
    "cast speed of", "proc", "aegis", "totem", "minion",
)
#: 明确与“造成伤害的能力”相关的词（用于未映射行的覆盖缺口判定）
DAMAGE_RELEVANT_TOKENS = (
    "damage", "critical", "penetration", "attack speed", "effect of frenzy",
    "effect of haste", "ambition", "brutality", "ailment", "ignite chance",
    "bleed chance", "more melee", "increased damage",
)
#: 已在 profile limitations 中声明“获取/叠层/时序不模拟”的词
DECLARED_ASSUMPTION_TOKENS = (
    "gain a stack", "on potion use", "when you use a potion", "when you hit a boss",
)

_SKILL_INDEX_RE = re.compile(r"_(\d+)_Stat$")


class MappingError(ValueError):
    """输入数据无法按已声明语义归一化（调用方决定兼容错误类型）。"""


def decode_tag_names(bitmask: Any) -> list[str]:
    """Decode an LE Tools AT tag bitmask (int or numeric string) into tag names."""
    if bitmask is None or isinstance(bitmask, bool):
        return []
    try:
        value = int(bitmask)
    except (TypeError, ValueError):
        return []
    if value <= 0:
        return []
    return model.decode_tags(value)


def catalog_locator(dataset_id: str, sql: str) -> str:
    return f"data/releases/{dataset_id}/data.sqlite#{sql}"


def evidence(kind: str, detail: str, locator: Optional[str] = None) -> model.Evidence:
    return model.Evidence(kind=kind, detail=detail, locator=locator)


# ---------------------------------------------------------------------------
# 兼容策略：历史 hit 路径与统一 dps 路径的显式差异
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MappingPolicy:
    """词条归一化策略。

    ``extended_stats=False`` 时只产出历史 hit 词表内的 stat_key，未映射行按
    ``legacy_*`` 分类与文案返回——这就是 Flame Reave 命中模型的兼容边界。
    """

    name: str = "generic"
    extended_stats: bool = True
    #: DoT-only Damage 行：``keep``（保留，组件阶段判断）| ``exclude``（历史命中模型）
    dot_only_damage: str = "keep"
    dot_only_reason: Optional[str] = None
    #: 未映射属性文案（按覆盖分类覆盖）；None 时使用本模块中性文案
    reason_templates: dict[str, str] = field(default_factory=dict)
    #: 兼容：历史 hit 的 evidence locator 前缀（配合 calc_input_label 的 basename）
    legacy_evidence_prefix: Optional[str] = None
    #: 历史 hit：自适应/无类型 added damage 的说明后缀
    adaptive_added_note: Optional[str] = None
    #: 历史 hit：技能等级行的排除理由
    skill_level_reason: Optional[str] = None
    #: 历史 hit 分类器（保持原防御/功能文案不变）
    legacy_classifier: bool = False
    #: 历史 hit：把 Damage 之外的全部未映射行归入 out_of_scope 文案
    collapse_unknown_to_out_of_scope: bool = False


GENERIC_POLICY = MappingPolicy()

NEUTRAL_REASONS = {
    COVERAGE_DEFENSIVE: "防御属性；不进入本次伤害分量",
    COVERAGE_UTILITY: "功能/行为属性；不进入本次伤害分量",
    COVERAGE_STATE_MARKER: "状态标记词条；由来源绑定规则验证后使用",
    COVERAGE_SKILL_LEVEL: "技能等级不直接作为通用伤害倍率；由节点与点数绑定表达",
    COVERAGE_DECLARED: "获取/叠层/时序过程已在 profile limitations 声明为不模拟",
    COVERAGE_DAMAGE_RELEVANT: "可能是伤害相关属性，但当前模型无对应规则（覆盖缺口）",
    COVERAGE_OUT_OF_SCOPE: "未映射属性；当前计算范围未包含（覆盖缺口）",
    COVERAGE_IDOL_REFRACTION: "神像折射词条强度；折射是否放大尚未确认（覆盖缺口）",
}


def _legacy_classify(lowered: str) -> str:
    if any(token in lowered for token in DEFENSIVE_TOKENS[:11]):
        return COVERAGE_DEFENSIVE
    if any(
        token in lowered
        for token in (
            "movement", "area", "range", "chill", "shock", "ignite", "freeze",
            "slow", "blind", "frailty", "armor shred", "shred", "experience",
        )
    ):
        return COVERAGE_UTILITY
    return COVERAGE_OUT_OF_SCOPE


def classify_unmapped(
    stat_name: str,
    *,
    property_id: Any = None,
    stat_type: Optional[int] = None,
    tags: Optional[int] = None,
    policy: MappingPolicy = GENERIC_POLICY,
) -> str:
    """给未映射的属性行一个覆盖分类（不猜数值，只决定是否进入覆盖缺口）。"""
    lowered = (stat_name or "").lower()
    if policy.legacy_classifier:
        return _legacy_classify(lowered)
    # 1) dataset/planner 的已知数据语义优先
    try:
        prop = int(property_id) if property_id is not None else None
    except (TypeError, ValueError):
        prop = None
    if prop == PROP_SKILL_LEVEL:
        return COVERAGE_SKILL_LEVEL
    if prop == PROP_IDOL_REFRACTION:
        return COVERAGE_IDOL_REFRACTION
    if prop == PROP_DAMAGE_TAKEN or prop == PROP_DAMAGE_TAKEN_NEARBY:
        return COVERAGE_DEFENSIVE
    if prop in (
        PROP_HEALTH, PROP_MANA, PROP_ARMOR, PROP_STUN_AVOIDANCE,
        PROP_FIRE_RESISTANCE, PROP_HEALTH_REGEN, PROP_VOID_RESISTANCE,
        PROP_BLOCK_CHANCE, PROP_WARD, PROP_ELEMENTAL_RESISTANCE,
        PROP_BLOCK_EFFECTIVENESS, PROP_STUN_IMMUNITY, PROP_PHYSICAL_RESISTANCE,
        PROP_STUN_DURATION_RECEIVED, PROP_ENDURANCE_THRESHOLD,
        PROP_BONUS_CRIT_DAMAGE_TAKEN, PROP_AREA, PROP_PARRY_CHANCE, PROP_MANA_REGEN,
    ):
        return COVERAGE_DEFENSIVE
    if prop == -1:
        # planner 未映射属性：Minion 限定行不影响自身伤害；more 乘区视为伤害相关
        if tags is not None and (int(tags) & model.AT_BITS["Minion"]):
            return COVERAGE_UTILITY
        if stat_type == 2:
            return COVERAGE_DAMAGE_RELEVANT
        return COVERAGE_UTILITY
    # 2) 名称词表：先排除已确认防御/功能，再判定可能的伤害相关性
    if any(token in lowered for token in DECLARED_ASSUMPTION_TOKENS):
        return COVERAGE_DECLARED
    if any(token in lowered for token in DEFENSIVE_TOKENS):
        return COVERAGE_DEFENSIVE
    if any(token in lowered for token in DAMAGE_RELEVANT_TOKENS) and not any(
        token in lowered for token in ("damage taken", "taken", "leeched")
    ):
        return COVERAGE_DAMAGE_RELEVANT
    if any(token in lowered for token in UTILITY_TOKENS):
        return COVERAGE_UTILITY
    return COVERAGE_OUT_OF_SCOPE


# ---------------------------------------------------------------------------
# contribution 构造
# ---------------------------------------------------------------------------

def condition_status(code: Optional[str], states: Optional[dict[str, Any]]) -> str:
    """数据词条上的条件代码求值；未知代码保持 unknown 而不是默认 false。"""
    if not code:
        return model.CONDITION_ALWAYS
    value = evaluate_condition(code, states or DEFAULT_HIT_STATES)
    if value is None:
        return model.CONDITION_UNKNOWN
    return model.CONDITION_SATISFIED if value else model.CONDITION_NOT_SATISFIED


def make_contribution(
    *,
    contribution_id: str,
    source_kind: str,
    source_label: str,
    source_path: str,
    stat_name: str,
    stat_key: str,
    operation: str,
    value: float,
    unit: str = "flat",
    damage_tags: Optional[list[str]] = None,
    target: Optional[str] = None,
    condition_code: Optional[str] = None,
    group: Optional[str] = None,
    included: bool = True,
    exclusion_reason: Optional[str] = None,
    value_source: str = "source",
    status: str = model.STATUS_CONFIRMED,
    evidence_rows: Optional[list[model.Evidence]] = None,
    notes: Optional[str] = None,
    raw: Optional[dict[str, Any]] = None,
    states: Optional[dict[str, Any]] = None,
) -> model.Contribution:
    return model.Contribution(
        contribution_id=contribution_id,
        source_kind=source_kind,
        source_label=source_label,
        source_path=source_path,
        stat_name=stat_name,
        stat_key=stat_key,
        operation=operation,
        value=float(value),
        unit=unit,
        damage_tags=list(damage_tags or []),
        target=target,
        condition_code=condition_code,
        condition_status=condition_status(condition_code, states),
        group=group,
        included=included,
        exclusion_reason=exclusion_reason,
        value_source=value_source,
        status=status,
        evidence=evidence_rows or [],
        notes=notes,
        raw=raw,
    )


def unsupported(
    *,
    contribution_id: str,
    source_kind: str,
    source_label: str,
    source_path: str,
    stat_name: str,
    operation: str = "flag",
    value: float = 0.0,
    unit: str = "flat",
    reason: str,
    status: str = model.STATUS_CONFIRMED,
    evidence_rows: Optional[list[model.Evidence]] = None,
    raw: Optional[dict[str, Any]] = None,
) -> model.Contribution:
    return make_contribution(
        contribution_id=contribution_id,
        source_kind=source_kind,
        source_label=source_label,
        source_path=source_path,
        stat_name=stat_name,
        stat_key=model.STAT_UNSUPPORTED,
        operation=operation,
        value=value,
        unit=unit,
        included=False,
        exclusion_reason=reason,
        status=status,
        evidence_rows=evidence_rows,
        raw=raw,
    )


ITEM_SOURCE_BY_PREFIX = (
    ("equipment.idol_altar", "idol_altar", "Idol Altar"),
    ("equipment.", "equipment", "Equipment"),
    ("idols.", "idol", "Idol"),
    ("blessings.", "blessing", "Blessing"),
)


def source_kind_for_instance(instance_id: str) -> tuple[str, str]:
    for prefix, kind, label in ITEM_SOURCE_BY_PREFIX:
        if instance_id.startswith(prefix):
            return kind, label
    return "equipment", "Equipment"


def item_lookup(calc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["instance_id"]): item
        for item in (calc.get("items") or [])
        if item.get("instance_id")
    }


def item_label(item: Optional[dict[str, Any]]) -> str:
    if not item:
        return ""
    display = item.get("display_name") or ""
    base = item.get("base_name") or item.get("item_type") or ""
    if display and base and display != base:
        return f"{display} ({base})"
    return display or base


def mod_evidence(
    mod: dict[str, Any],
    dataset_id: str,
    *,
    locator: Optional[str],
) -> list[model.Evidence]:
    rows = [
        evidence(
            "catalog",
            "calculation-input-v1 modifier line (affix-db recomputed export)",
            locator,
        )
    ]
    provenance = mod.get("provenance") or {}
    if provenance.get("source_path"):
        rows.append(
            evidence(
                "catalog",
                f"build source path {provenance.get('source_path')}",
                catalog_locator(dataset_id, f"build spec {provenance.get('source_path')}"),
            )
        )
    return rows


_LEGACY_DOUBLED_FIRE_PREFIX = "increased fire damage. this effect is doubled"


def _damage_operation(modifier_type: Optional[int]) -> tuple[str, str]:
    if modifier_type == 0:
        return "added", "flat"
    if modifier_type == 1:
        return "increased", "percent"
    if modifier_type == 2:
        return "more", "percent"
    return "flag", "flat"


def _is_dot_only(tags: Any) -> bool:
    if not tags:
        return False
    try:
        value = int(tags)
    except (TypeError, ValueError):
        return False
    return bool(value & model.AT_BITS["DoT"]) and not bool(value & model.AT_BITS["Hit"])


def _reason_for(classification: str, policy: MappingPolicy) -> str:
    if policy.collapse_unknown_to_out_of_scope and classification in (
        COVERAGE_DAMAGE_RELEVANT,
        COVERAGE_OUT_OF_SCOPE,
        COVERAGE_IDOL_REFRACTION,
    ):
        classification = COVERAGE_OUT_OF_SCOPE
    return policy.reason_templates.get(classification) or NEUTRAL_REASONS.get(
        classification, NEUTRAL_REASONS[COVERAGE_OUT_OF_SCOPE]
    )


def map_mod(
    mod: dict[str, Any],
    *,
    item: Optional[dict[str, Any]] = None,
    dataset_id: str = "",
    states: Optional[dict[str, Any]] = None,
    calc_input_label: str = "calculation-input.affix-db.corrected.json",
    policy: MappingPolicy = GENERIC_POLICY,
) -> list[model.Contribution]:
    """把一条冻结的 ``calculation-input`` 词条行归一化成贡献。

    与历史 ``hit_inputs.map_mod`` 的区别只在于**适用性归属**：本函数不按
    某个技能命中模型排除 DoT-only 词条，排除与否由 ``policy`` 决定。
    """
    states = states or DEFAULT_HIT_STATES
    stat = mod.get("stat") or {}
    name = (stat.get("name") or "").strip()
    stat_unit = stat.get("unit") or "flat"
    instance_id = str(mod.get("item_instance_id") or "")
    source_kind, _ = source_kind_for_instance(instance_id)
    source_label = item_label(item) or instance_id
    source_path = (mod.get("provenance") or {}).get("source_path") or f"data.{instance_id}"
    mod_id = str(mod.get("mod_id") or "")
    contribution_id = f"mod:{mod_id}"
    locator = calc_input_label
    if policy.legacy_evidence_prefix:
        locator = policy.legacy_evidence_prefix + Path(calc_input_label).name
    base_evidence = mod_evidence(mod, dataset_id, locator=locator)
    tags = mod.get("tags")
    special_tag = mod.get("special_tag")
    property_id = mod.get("property")
    modifier_type = mod.get("modifier_type")
    value = _num(mod.get("value_numeric"))
    notes = mod.get("readable_text")
    raw: dict[str, Any] = {
        "mod_id": mod_id,
        "item_instance_id": instance_id,
        "property": property_id,
        "tags": tags,
        "special_tag": special_tag,
        "modifier_type": modifier_type,
        "display_value": mod.get("display_value"),
        "readable_text": mod.get("readable_text"),
    }
    value_status = mod.get("value_status")
    status = model.STATUS_CONFIRMED if value_status in (None, "actual") else model.STATUS_DERIVED
    value_source = mod.get("value_source") or "source"

    def make(**kwargs: Any) -> model.Contribution:
        payload = dict(
            contribution_id=contribution_id,
            source_kind=source_kind,
            source_label=source_label,
            source_path=source_path,
            stat_name=name,
            operation="flag",
            value=value,
            unit=stat_unit,
            evidence_rows=base_evidence,
            notes=notes,
            raw=raw,
            value_source=value_source,
            status=status,
            states=states,
        )
        payload.update(kwargs)
        return make_contribution(**payload)

    if mod.get("kind") == "description":
        return []  # 由调用方收集到 unique descriptions
    if not name:
        return [
            unsupported(
                contribution_id=contribution_id,
                source_kind=source_kind,
                source_label=source_label,
                source_path=source_path,
                stat_name=mod.get("readable_text") or "(unresolved modifier)",
                reason="modifier line has no resolved stat (informational only)",
                status=model.STATUS_UNRESOLVED,
                evidence_rows=base_evidence,
                raw=raw,
            )
        ]

    lowered = name.lower()

    # --- 数据怪癖（与技能无关） -------------------------------------------
    if lowered.startswith(_LEGACY_DOUBLED_FIRE_PREFIX):
        base = make(
            stat_key=model.STAT_DAMAGE,
            operation="increased",
            value=value,
            unit="percent",
            damage_tags=["Fire"],
            notes=(notes or "") + " [base value]",
        )
        bonus = make(
            contribution_id=f"{contribution_id}#doubled",
            stat_key=model.STAT_DAMAGE,
            operation="increased",
            value=value,
            unit="percent",
            damage_tags=["Fire"],
            condition_code="mana_ge_300",
            notes=(notes or "") + " [doubling bonus, requires 300+ maximum mana]",
        )
        return [base, bonus]

    # --- Damage / added damage -------------------------------------------
    if name == "Damage":
        operation, unit = _damage_operation(modifier_type)
        tags_list = decode_tag_names(tags)
        if _is_dot_only(tags):
            if policy.dot_only_damage == "exclude":
                return [
                    unsupported(
                        contribution_id=contribution_id,
                        source_kind=source_kind,
                        source_label=source_label,
                        source_path=source_path,
                        stat_name=name,
                        operation=operation,
                        value=value,
                        unit=unit,
                        reason=policy.dot_only_reason
                        or "DoT-only modifier excluded by this model policy",
                        evidence_rows=base_evidence,
                        raw=raw,
                    )
                ]
            return [
                make(
                    stat_key=model.STAT_ADDED_DAMAGE if operation == "added" else model.STAT_DAMAGE,
                    operation=operation,
                    value=value,
                    unit=unit if operation == "added" else "percent",
                    damage_tags=tags_list,
                    raw={**raw, "coverage_class": "dot_only_damage"},
                )
            ]
        if operation == "added":
            typed_tags = [tag for tag in tags_list if tag in DAMAGE_TYPE_TAGS]
            if not typed_tags:
                note = (notes or "")
                if policy.adaptive_added_note:
                    note = note + policy.adaptive_added_note
                return [
                    make(
                        stat_key=model.STAT_ADDED_DAMAGE,
                        operation="added",
                        value=value,
                        unit="flat",
                        damage_tags=tags_list,
                        status=model.STATUS_CONFIRMED,
                        notes=note,
                    )
                ]
            return [
                make(
                    stat_key=model.STAT_ADDED_DAMAGE,
                    operation="added",
                    value=value,
                    unit="flat",
                    damage_tags=tags_list,
                )
            ]
        return [
            make(
                stat_key=model.STAT_DAMAGE,
                operation=operation,
                value=value,
                unit="percent",
                damage_tags=tags_list,
            )
        ]

    # --- 速度 / 暴击 / 穿透 / 冷却 ---------------------------------------
    if name == "Attack Speed":
        operation, _ = _damage_operation(modifier_type)
        operation = "increased" if operation == "more" else operation
        return [
            make(
                stat_key=model.STAT_ATTACK_SPEED,
                operation=operation,
                value=value,
                unit="percent" if operation == "increased" else "flat",
                damage_tags=decode_tag_names(tags),
            )
        ]
    if name in ("Cast Speed", "Increased Cast Speed"):
        return [
            make(
                stat_key=model.STAT_CAST_SPEED,
                operation="increased",
                value=value,
                unit="percent",
                damage_tags=decode_tag_names(tags),
            )
        ]
    if name == "Critical Strike Chance":
        operation = "increased" if modifier_type == 1 else "added"
        return [
            make(
                stat_key=model.STAT_CRIT_CHANCE,
                operation=operation,
                value=value,
                unit="percent",
                damage_tags=decode_tag_names(tags),
            )
        ]
    if name == "Critical Strike Multiplier":
        operation = "increased" if modifier_type == 1 else "added"
        return [
            make(
                stat_key=model.STAT_CRIT_MULTIPLIER,
                operation=operation,
                value=value,
                unit="percent",
                damage_tags=decode_tag_names(tags),
            )
        ]
    if lowered.endswith("penetration"):
        element = name[: -len(" penetration")].strip().lower()
        return [
            make(
                stat_key=model.STAT_PENETRATION,
                operation="added",
                value=value,
                unit="percent",
                damage_tags=[element.title()] if element in {t.lower() for t in DAMAGE_TYPE_TAGS} else [],
                target=element,
            )
        ]
    if "cooldown recovery" in lowered:
        return [
            make(
                stat_key=model.STAT_COOLDOWN_RECOVERY,
                operation="increased",
                value=value,
                unit="percent",
            )
        ]

    # --- 属性 --------------------------------------------------------------
    if name == "All Attributes":
        return [
            make(stat_key=model.STAT_ALL_ATTRIBUTES, operation="added", value=value, unit="flat")
        ]
    attribute_key = {
        "Strength": "strength",
        "Vitality": "vitality",
        "Intelligence": "intelligence",
        "Dexterity": "dexterity",
        "Attunement": "attunement",
    }.get(name)
    if attribute_key:
        return [
            make(
                stat_key=model.STAT_ATTRIBUTE,
                operation="added",
                value=value,
                unit="flat",
                target=attribute_key,
            )
        ]

    # --- 抗性 --------------------------------------------------------------
    resistance_target = RESISTANCE_BY_NAME.get(lowered)
    if resistance_target:
        return [
            make(
                stat_key=model.STAT_RESISTANCE,
                operation="added",
                value=value,
                unit="percent",
                target=resistance_target,
            )
        ]

    # --- 法力 --------------------------------------------------------------
    if name == "Mana":
        return [
            make(
                stat_key=model.STAT_MANA,
                operation="increased" if modifier_type == 1 else "added",
                value=value,
                unit="percent" if modifier_type == 1 else "flat",
            )
        ]
    if name in ("Mana Regen", "Increased Mana Regen"):
        return [
            make(
                stat_key=model.STAT_MANA_REGEN,
                operation="increased",
                value=value,
                unit="percent",
            )
        ]

    # --- 技能等级（不按等级给通用倍率；由节点/点数绑定表达） ---------------
    if name == "Level of Skills":
        return [
            unsupported(
                contribution_id=contribution_id,
                source_kind=source_kind,
                source_label=source_label,
                source_path=source_path,
                stat_name=name,
                operation="added",
                value=value,
                unit="flat",
                reason=policy.skill_level_reason
                or NEUTRAL_REASONS[COVERAGE_SKILL_LEVEL],
                status=model.STATUS_CONFIRMED,
                evidence_rows=base_evidence,
                raw=raw,
            )
        ]

    # --- 新 dps 路径启用的扩展词表 ----------------------------------------
    if policy.extended_stats:
        mapped = _map_extended_mod(
            mod,
            make=make,
            name=name,
            lowered=lowered,
            property_id=property_id,
            special_tag=special_tag,
            modifier_type=modifier_type,
            value=value,
            stat_unit=stat_unit,
        )
        if mapped is not None:
            return mapped

    # --- 其它：保留在账本，不静默丢弃 --------------------------------------
    classification = classify_unmapped(
        name,
        property_id=property_id,
        stat_type=modifier_type,
        tags=tags,
        policy=policy,
    )
    ledger_raw = dict(raw)
    if policy.extended_stats:
        ledger_raw["coverage_class"] = classification
    return [
        unsupported(
            contribution_id=contribution_id,
            source_kind=source_kind,
            source_label=source_label,
            source_path=source_path,
            stat_name=name,
            operation="flag",
            value=value,
            unit=stat_unit,
            reason=_reason_for(classification, policy),
            evidence_rows=base_evidence,
            raw=ledger_raw,
        )
    ]


def _map_extended_mod(
    mod: dict[str, Any],
    *,
    make: Callable[..., model.Contribution],
    name: str,
    lowered: str,
    property_id: Any,
    special_tag: Any,
    modifier_type: Any,
    value: float,
    stat_unit: str,
) -> Optional[list[model.Contribution]]:
    """新路径扩展属性：movement/effect_of/ailment chance/mana cost/damage taken。"""

    def prop() -> Optional[int]:
        try:
            return int(property_id) if property_id is not None else None
        except (TypeError, ValueError):
            return None

    pid = prop()
    if name.endswith(" Mana Cost"):
        return [
            make(
                stat_key=model.STAT_MANA_COST,
                operation={0: "added", 1: "increased", 2: "more"}.get(modifier_type, "added"),
                value=value,
                unit="percent" if modifier_type in (1, 2) else "flat",
            )
        ]
    if pid == PROP_MOVEMENT_SPEED:
        return [
            make(
                stat_key=STAT_MOVEMENT_SPEED,
                operation={0: "added", 1: "increased", 2: "more"}.get(modifier_type, "increased"),
                value=value,
                unit="percent" if modifier_type in (1, 2) else "flat",
            )
        ]
    if pid == PROP_AILMENT_CHANCE:
        return [
            make(
                stat_key=model.STAT_AILMENT_CHANCE,
                operation="added",
                value=value,
                unit="percent",
                target=str(special_tag) if special_tag is not None else None,
            )
        ]
    if pid == PROP_EFFECT_OF:
        return [
            make(
                stat_key=STAT_EFFECT_OF,
                operation={0: "added", 1: "increased", 2: "more"}.get(modifier_type, "increased"),
                value=value,
                unit="percent" if modifier_type in (1, 2) else "flat",
                target=str(special_tag) if special_tag is not None else None,
            )
        ]
    if pid == PROP_MANA_COST:
        return [
            make(
                stat_key=model.STAT_MANA_COST,
                operation={0: "added", 1: "increased", 2: "more"}.get(modifier_type, "added"),
                value=value,
                unit="percent" if modifier_type in (1, 2) else "flat",
            )
        ]
    if pid == PROP_DAMAGE_TAKEN:
        return [
            make(
                stat_key=STAT_DAMAGE_TAKEN,
                operation={0: "added", 1: "increased", 2: "more"}.get(modifier_type, "increased"),
                value=value,
                unit="percent" if modifier_type in (1, 2) else "flat",
                damage_tags=decode_tag_names(mod.get("tags")),
                raw={
                    "mod_id": mod.get("mod_id"),
                    "item_instance_id": mod.get("item_instance_id"),
                    "property": property_id,
                    "tags": mod.get("tags"),
                    "special_tag": special_tag,
                    "modifier_type": modifier_type,
                    "readable_text": mod.get("readable_text"),
                    "coverage_class": COVERAGE_DEFENSIVE,
                },
            )
        ]
    if pid in (58,):
        return [
            make(
                stat_key=STAT_STATE_MARKER,
                operation="flag",
                value=value,
                unit=stat_unit,
                target=str(special_tag) if special_tag is not None else None,
                included=False,
                exclusion_reason=NEUTRAL_REASONS[COVERAGE_STATE_MARKER],
                status=model.STATUS_CONFIRMED,
                raw={
                    "mod_id": mod.get("mod_id"),
                    "item_instance_id": mod.get("item_instance_id"),
                    "property": property_id,
                    "tags": mod.get("tags"),
                    "special_tag": special_tag,
                    "readable_text": mod.get("readable_text"),
                    "coverage_class": COVERAGE_STATE_MARKER,
                },
            )
        ]
    return None


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 被动天赋行映射
# ---------------------------------------------------------------------------


def map_passive_stat(
    property_id: Any,
    stat_type: int,
    tags: Any,
    *,
    extended: bool = False,
    special_tag: Any = None,
) -> tuple[str, list[str], Optional[str]]:
    """把一条 planner 被动 stat 映射到 stat 词表；不猜测未知属性。"""
    try:
        property_int = int(property_id)
    except (TypeError, ValueError):
        property_int = -1
    tags_list = decode_tag_names(tags)
    if property_int in ATTRIBUTE_PROPERTIES:
        return model.STAT_ATTRIBUTE, [], ATTRIBUTE_PROPERTIES[property_int]
    if property_int == PROP_ALL_ATTRIBUTES:
        return model.STAT_ALL_ATTRIBUTES, [], None
    if property_int == PROP_DAMAGE:
        if stat_type == 0:
            return model.STAT_ADDED_DAMAGE, tags_list, None
        return model.STAT_DAMAGE, tags_list, None
    if property_int == PROP_ATTACK_SPEED:
        return model.STAT_ATTACK_SPEED, tags_list, None
    if property_int == PROP_CAST_SPEED:
        return model.STAT_CAST_SPEED, tags_list, None
    if property_int == PROP_CRIT_CHANCE:
        return model.STAT_CRIT_CHANCE, tags_list, None
    if property_int == PROP_CRIT_MULTIPLIER:
        return model.STAT_CRIT_MULTIPLIER, tags_list, None
    if property_int == PROP_PENETRATION:
        return model.STAT_PENETRATION, tags_list, None
    if property_int == PROP_COOLDOWN_RECOVERY:
        return model.STAT_COOLDOWN_RECOVERY, tags_list, None
    if property_int == PROP_MANA:
        return model.STAT_MANA, tags_list, None
    if property_int == PROP_MANA_REGEN:
        return model.STAT_MANA_REGEN, tags_list, None
    if property_int == 41:  # adaptive spell damage
        return model.STAT_ADDED_DAMAGE, ["Spell"], None
    if extended:
        if property_int == PROP_MOVEMENT_SPEED:
            return STAT_MOVEMENT_SPEED, tags_list, None
        if property_int == PROP_AILMENT_CHANCE:
            return model.STAT_AILMENT_CHANCE, tags_list, (
                str(special_tag) if special_tag is not None else None
            )
        if property_int == PROP_EFFECT_OF:
            return STAT_EFFECT_OF, tags_list, (
                str(special_tag) if special_tag is not None else None
            )
        if property_int == PROP_MANA_COST:
            return model.STAT_MANA_COST, tags_list, None
        if property_int == PROP_DAMAGE_TAKEN:
            return STAT_DAMAGE_TAKEN, tags_list, None
        if property_int == PROP_SKILL_LEVEL:
            return STAT_SKILL_LEVEL, tags_list, None
    return model.STAT_UNSUPPORTED, tags_list, None


def no_scaling_total(value: float, points: int, raw: dict[str, Any]) -> float:
    """按点数缩放单点数值；``noScaling`` 阈值以下为 0。"""
    if raw.get("noScaling"):
        threshold = raw.get("noScalingPointThreshold")
        if threshold is None:
            threshold = 1
        return value if points >= int(threshold) else 0.0
    return value * points


def attribute_detail(
    contributions: Iterable[model.Contribution],
    class_base: dict[str, Any],
) -> dict[str, Any]:
    """从活动贡献汇总五维属性（与历史 hit 快照逐位一致的运算顺序）。"""
    attributes = {key: 0.0 for key in model.ATTRIBUTE_ORDER}
    rows: dict[str, list[dict[str, Any]]] = {key: [] for key in model.ATTRIBUTE_ORDER}
    for contribution in contributions:
        if not contribution.active:
            continue
        targets: list[tuple[str, float]] = []
        if contribution.stat_key == model.STAT_ATTRIBUTE and contribution.target in attributes:
            targets = [(str(contribution.target), contribution.value)]
        elif contribution.stat_key == model.STAT_ALL_ATTRIBUTES:
            targets = [(key, contribution.value) for key in model.ATTRIBUTE_ORDER]
        for key, value in targets:
            attributes[key] += value
            rows[key].append(
                {
                    "contribution_id": contribution.contribution_id,
                    "source_kind": contribution.source_kind,
                    "value": value,
                }
            )
    return {
        "attributes": {key: round(value, 6) for key, value in attributes.items()},
        "sources": rows,
        "class_base": {
            "baseStrength": class_base.get("baseStrength"),
            "baseDexterity": class_base.get("baseDexterity"),
            "baseIntelligence": class_base.get("baseIntelligence"),
            "baseAttunement": class_base.get("baseAttunement"),
            "baseVitality": class_base.get("baseVitality"),
        },
    }


# ---------------------------------------------------------------------------
# 目录数据抽取（catalog-backed）；历史 hit 路径同样委托到这里
# ---------------------------------------------------------------------------


def class_base(catalog: Any, class_entity_id: str) -> dict[str, Any]:
    row = catalog.one("SELECT extensions FROM classes WHERE entity_id = ?", (class_entity_id,))
    if row is None:
        raise MappingError(f"class row not found in catalog: {class_entity_id}")
    extensions = _json_or_none(row.get("extensions")) or {}
    character_class = extensions.get("characterClass") or {}
    if not character_class:
        raise MappingError(f"class row has no characterClass: {class_entity_id}")
    return character_class


def quest_attribute_points(catalog: Any) -> tuple[int, Optional[str]]:
    record = catalog.source_record(
        "ability_config", "LEAbilities.attributePointsFromQuests", "attributePointsFromQuests"
    )
    if not record:
        return 0, None
    payload = record.get("raw_payload") or {}
    try:
        points = int(payload.get("attributePointsFromQuests"))
    except (TypeError, ValueError):
        points = 0
    locator = record.get("locator") or (
        f"data/releases/{catalog.dataset_id()}/data.sqlite#source_records "
        "entity_kind=ability_config source_id=attributePointsFromQuests"
    )
    return points, locator


def base_contributions(catalog: Any, calc: dict[str, Any], dataset_id: str) -> list[model.Contribution]:
    """职业基础属性 + 任务属性点（source_kind=base_character）。"""
    character = calc.get("character") or {}
    class_entity_id = ((character.get("class") or {}).get("entity_id")) or ""
    if not class_entity_id:
        raise MappingError("calculation input character has no class entity id")
    base = class_base(catalog, class_entity_id)
    locator = catalog_locator(dataset_id, f"classes entity_id={class_entity_id}")
    labels = {
        "strength": "baseStrength",
        "vitality": "baseVitality",
        "intelligence": "baseIntelligence",
        "dexterity": "baseDexterity",
        "attunement": "baseAttunement",
    }
    contributions: list[model.Contribution] = []
    for attribute in model.ATTRIBUTE_ORDER:
        key = labels[attribute]
        contributions.append(
            make_contribution(
                contribution_id=f"base:attribute:{attribute}",
                source_kind="base_character",
                source_label=f"{character.get('class', {}).get('name', 'class')} class base",
                source_path="data.character.class",
                stat_name=key,
                stat_key=model.STAT_ATTRIBUTE,
                operation="added",
                value=_num(base.get(key)),
                unit="flat",
                target=attribute,
                value_source="source",
                status=model.STATUS_CONFIRMED,
                evidence_rows=[evidence("catalog", f"catalog classes.characterClass.{key}", locator)],
                notes="class base attribute",
            )
        )
    quest_points, quest_locator = quest_attribute_points(catalog)
    # calculation-input 已含任务奖励行时不得再叠加 catalog 派生任务点（否则重复计入属性）。
    has_quest_reward = any(
        str(mod.get("source") or "") == "quest_reward"
        and "all attributes" in str(((mod.get("stat") or {}).get("name") or "")).lower()
        for mod in (calc.get("mods") or [])
    )
    if quest_points and not has_quest_reward:
        contributions.append(
            make_contribution(
                contribution_id="quest:attribute:all",
                source_kind="base_character",
                source_label="quest attribute points (campaign)",
                source_path="data.quests",
                stat_name="All Attributes",
                stat_key=model.STAT_ALL_ATTRIBUTES,
                operation="added",
                value=float(quest_points),
                unit="flat",
                value_source="source",
                status=model.STATUS_CONFIRMED,
                evidence_rows=[
                    evidence("catalog", "LEAbilities.attributePointsFromQuests", quest_locator)
                ],
                notes="campaign quest reward: +N to all attributes",
            )
        )
    return contributions


def mod_contributions(
    calc: dict[str, Any],
    *,
    dataset_id: str,
    states: dict[str, Any],
    calc_input_label: str,
    map_mod_fn: Callable[..., list[model.Contribution]],
) -> tuple[list[model.Contribution], list[dict[str, Any]]]:
    """遍历 ``calc.mods`` 生成贡献；description 行收集为说明文字，不进入伤害账本。"""
    items = item_lookup(calc)
    contributions: list[model.Contribution] = []
    descriptions: list[dict[str, Any]] = []
    for mod in calc.get("mods") or []:
        if mod.get("kind") == "description":
            descriptions.append(
                {
                    "mod_id": mod.get("mod_id"),
                    "item_instance_id": mod.get("item_instance_id"),
                    "source_path": (mod.get("provenance") or {}).get("source_path"),
                    "text": mod.get("readable_text"),
                    "classification": "unique-tooltip description line (informational only)",
                }
            )
            continue
        item = items.get(str(mod.get("item_instance_id")))
        contributions.extend(
            map_mod_fn(
                mod,
                item=item,
                dataset_id=dataset_id,
                states=states,
                calc_input_label=calc_input_label,
            )
        )
    return contributions, descriptions


def passive_tree_id(calc: dict[str, Any]) -> str:
    tree = (calc.get("passive_tree") or {}).get("tree") or {}
    entity_id = str(tree.get("entity_id") or "")
    return entity_id.rsplit(":", 1)[-1] if entity_id else ""


def tree_by_source_id(catalog: Any, source_tree_id: str) -> dict[str, Any]:
    rows = [tree for tree in catalog.trees() if tree.get("source_tree_id") == source_tree_id]
    if len(rows) != 1:
        raise MappingError(
            f"expected exactly one catalog tree {source_tree_id!r}, found {len(rows)}"
        )
    return rows[0]


def nodes_by_index(catalog: Any, tree_entity_id: str) -> dict[str, dict[str, Any]]:
    return {str(node["node_index"]): node for node in catalog.nodes_for_tree(tree_entity_id)}


def skill_node_effect_values(catalog: Any, node_entity_id: str) -> dict[int, list[dict[str, Any]]]:
    """把一个技能树节点解析出的 ``stat_mod`` 效果按 stat 下标分组。"""
    grouped: dict[int, list[dict[str, Any]]] = {}
    for effect in catalog.effects_for_owner(node_entity_id):
        if effect.get("effect_category") != "stat_mod":
            continue
        for value in effect.get("values") or []:
            raw = _json_or_none(value.get("raw")) or {}
            stat_name_key = str(raw.get("statNameKey") or "")
            match = _SKILL_INDEX_RE.search(stat_name_key)
            if match is None:
                continue
            grouped.setdefault(int(match.group(1)), []).append(
                {
                    "effect_id": effect.get("effect_id"),
                    "value": value.get("value_numeric"),
                    "kind": value.get("value_kind"),
                    "unit": value.get("unit"),
                    "stat_name_key": stat_name_key,
                    "raw": raw,
                }
            )
    return grouped


def passive_contributions(
    catalog: Any,
    calc: dict[str, Any],
    *,
    dataset_id: str,
    condition_evaluator: Callable[[dict[str, Any]], tuple[Optional[bool], Optional[str]]],
    extended: bool = True,
) -> tuple[list[model.Contribution], list[tuple[model.Contribution, dict[str, Any]]]]:
    """planner 被动天赋行 → 贡献。

    返回 ``(rows, deferred)``：带 ``per`` 缩放的行必须等属性汇总后再求值，
    由调用方在属性池就绪后处理（保持属性、转换与节点效果的求值顺序）。
    """
    tree_id = passive_tree_id(calc)
    if not tree_id:
        raise MappingError("calculation input passive_tree has no tree entity id")
    payload = catalog.passive_bonus_payload(tree_id)
    stats_by_node = payload.get("stats") or {}
    tree = tree_by_source_id(catalog, tree_id)
    nodes = nodes_by_index(catalog, tree["entity_id"])
    locator = catalog_locator(
        dataset_id,
        f"source_records entity_kind=passive_bonus "
        f"source_scope=planner.passiveBonuses source_id={tree_id}",
    )
    rows: list[model.Contribution] = []
    deferred: list[tuple[model.Contribution, dict[str, Any]]] = []
    for entry in (calc.get("passive_tree") or {}).get("nodes") or []:
        node_id = str(entry.get("node_id"))
        points = int(entry.get("points") or 0)
        if points <= 0:
            continue
        node = nodes.get(node_id) or {}
        node_name = (
            entry.get("name")
            or (node.get("extensions") or {}).get("nodeNameKey")
            or f"passive node {node_id}"
        )
        for index, stat in enumerate(stats_by_node.get(node_id) or []):
            if not isinstance(stat, dict):
                continue
            property_id = stat.get("property")
            tags = stat.get("tags")
            stat_type = int(stat.get("type") or 0)
            value = _num(stat.get("value"))
            stat_key, tags_list, target = map_passive_stat(
                property_id,
                stat_type,
                tags,
                extended=extended,
                special_tag=stat.get("specialTag"),
            )
            operation = {0: "added", 1: "increased", 2: "more"}.get(stat_type, "flag")
            unit = "percent" if stat_type in (1, 2) else "flat"
            conditions = stat.get("conditions") or []
            results: list[bool] = []
            codes: list[str] = []
            unknown_condition = False
            for condition in conditions:
                result, code = condition_evaluator(condition)
                codes.append(code or json.dumps(condition, sort_keys=True))
                if result is None:
                    unknown_condition = True
                else:
                    results.append(bool(result))
            supported = stat_key != model.STAT_UNSUPPORTED
            if supported:
                condition_code = "+".join(codes) if codes else None
                if unknown_condition:
                    condition_status_value = model.CONDITION_UNKNOWN
                    included = False
                    exclusion_reason = f"条件无法判定：{condition_code}"
                    status = model.STATUS_UNRESOLVED
                elif codes and not all(results):
                    condition_status_value = model.CONDITION_NOT_SATISFIED
                    included = False
                    exclusion_reason = f"条件未满足：{condition_code}"
                    status = model.STATUS_DERIVED
                else:
                    condition_status_value = (
                        model.CONDITION_SATISFIED if codes else model.CONDITION_ALWAYS
                    )
                    included = True
                    exclusion_reason = None
                    status = model.STATUS_DERIVED
                total = no_scaling_total(value, points, stat)
                row = model.Contribution(
                    contribution_id=f"passive:{node_id}:{index}",
                    source_kind="passive_node",
                    source_label=node_name,
                    source_path=f"passive_tree.nodes.{node_id}",
                    stat_name=str(property_id),
                    stat_key=stat_key,
                    operation=operation,
                    value=float(total),
                    unit=unit,
                    damage_tags=list(tags_list),
                    target=target,
                    condition_code=condition_code,
                    condition_status=condition_status_value,
                    group=f"passive:{node_id}:{index}",
                    included=included,
                    exclusion_reason=exclusion_reason,
                    value_source="derived",
                    status=status,
                    evidence=[
                        evidence(
                            "catalog",
                            f"planner.passiveBonuses stats[{node_id!r}][{index}]",
                            locator,
                        )
                    ],
                    notes=(
                        "planner.passiveBonuses normalized stat; value = per-point value "
                        f"{value:g} x {points} point(s)"
                        + (" (noScaling threshold applied)" if stat.get("noScaling") else "")
                    ),
                    raw={
                        "node_id": node_id,
                        "index": index,
                        "points": points,
                        "stat": dict(stat),
                    },
                )
            else:
                classification = classify_unmapped(
                    str(property_id),
                    property_id=property_id,
                    stat_type=stat_type,
                    tags=tags,
                    policy=GENERIC_POLICY,
                )
                row = model.Contribution(
                    contribution_id=f"passive:{node_id}:{index}",
                    source_kind="passive_node",
                    source_label=node_name,
                    source_path=f"passive_tree.nodes.{node_id}",
                    stat_name=str(property_id),
                    stat_key=model.STAT_UNSUPPORTED,
                    operation=operation,
                    value=float(no_scaling_total(value, points, stat)),
                    unit=unit,
                    damage_tags=list(tags_list),
                    target=target,
                    condition_code="+".join(codes) if codes else None,
                    condition_status=(
                        model.CONDITION_UNKNOWN
                        if unknown_condition
                        else (
                            model.CONDITION_SATISFIED
                            if codes and all(results)
                            else (
                                model.CONDITION_NOT_SATISFIED
                                if codes and not all(results)
                                else model.CONDITION_ALWAYS
                            )
                        )
                    ),
                    group=f"passive:{node_id}:{index}",
                    included=False,
                    exclusion_reason=NEUTRAL_REASONS.get(
                        classification, NEUTRAL_REASONS[COVERAGE_OUT_OF_SCOPE]
                    ),
                    value_source="derived",
                    status=model.STATUS_UNRESOLVED,
                    evidence=[
                        evidence(
                            "catalog",
                            f"planner.passiveBonuses stats[{node_id!r}][{index}]",
                            locator,
                        )
                    ],
                    raw={
                        "node_id": node_id,
                        "index": index,
                        "points": points,
                        "stat": dict(stat),
                        "coverage_class": classification,
                    },
                )
            if stat.get("per"):
                deferred.append((row, stat))
                continue
            rows.append(row)
    return rows, deferred


def _json_or_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ATTRIBUTE_PROPERTIES",
    "COVERAGE_DAMAGE_RELEVANT",
    "COVERAGE_DECLARED",
    "COVERAGE_DEFENSIVE",
    "COVERAGE_GAP_CLASSES",
    "COVERAGE_IDOL_REFRACTION",
    "COVERAGE_OUT_OF_SCOPE",
    "COVERAGE_SKILL_LEVEL",
    "COVERAGE_STATE_MARKER",
    "COVERAGE_UTILITY",
    "DAMAGE_TYPE_TAGS",
    "GENERIC_POLICY",
    "MappingPolicy",
    "NEUTRAL_REASONS",
    "PROP_AILMENT_CHANCE",
    "PROP_DAMAGE",
    "PROP_DAMAGE_TAKEN",
    "PROP_EFFECT_OF",
    "PROP_MANA_COST",
    "PROP_MOVEMENT_SPEED",
    "PROP_SKILL_LEVEL",
    "RESISTANCE_BY_NAME",
    "STAT_DAMAGE_TAKEN",
    "STAT_EFFECT_OF",
    "STAT_MOVEMENT_SPEED",
    "STAT_SKILL_LEVEL",
    "STAT_STATE_MARKER",
    "attribute_detail",
    "base_contributions",
    "class_base",
    "classify_unmapped",
    "catalog_locator",
    "decode_tag_names",
    "evidence",
    "item_label",
    "item_lookup",
    "make_contribution",
    "map_mod",
    "map_passive_stat",
    "mod_contributions",
    "mod_evidence",
    "no_scaling_total",
    "nodes_by_index",
    "passive_tree_id",
    "quest_attribute_points",
    "skill_node_effect_values",
    "source_kind_for_instance",
    "tree_by_source_id",
    "unsupported",
]
