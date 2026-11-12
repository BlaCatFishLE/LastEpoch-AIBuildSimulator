"""组件装配（统一 DPS 第一步 · S2/S4/S5）。

把已经归一化的贡献池装配成**显式组件**：

* 命中组件：标签、暴击能力、附加伤害策略由 profile 组件声明决定，不再把
  所有命中硬编码成 Melee；
* 持续伤害组件：按异常条目声明伤害类型/持续时间/施加规则；
* 每个组件记录“哪些贡献参与、以什么角色参与、为什么没有参与”，
  调用方据此生成组件级账本；这里不修改共享 ``included`` 字段。

纯函数模块：不读文件、不访问目录、不做输入指纹校验。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from . import model
from .mechanics.crit_expected import CritCapability, CritPools, expected_crit
from .mechanics.increased_aggregate import aggregate
from .mechanics.tag_match import TagPolicy, UNKNOWN_RESTRICT, tags_match
from .mechanics.target_side import hit_component_target

#: 组件标签闭包策略：``Elemental`` 视为展开（等价跳过），其余标签必须出现在
#: 组件闭包内。与历史 hit 口径相同，但作为 dps 的显式策略声明，不再经 hit 模块。
DPS_TAG_POLICY = TagPolicy(
    ignored_tags=frozenset({"Elemental"}),
    known_tags=frozenset(),
    unknown_tag_policy=UNKNOWN_RESTRICT,
    name="dps-component-closure",
)

#: 应用记录状态
APPLIED = "applied"
NOT_APPLICABLE = "not_applicable"
CONDITION_NOT_SATISFIED = "condition_not_satisfied"
SOURCE_ABSENT = "source_absent"
UNRESOLVED = "unresolved"
DUPLICATE = "duplicate"

APPLICATION_STATUSES = (
    APPLIED, NOT_APPLICABLE, CONDITION_NOT_SATISFIED, SOURCE_ABSENT, UNRESOLVED, DUPLICATE,
)

#: 会进入命中/持续伤害组件的 stat_key（用于组件级应用记录）
HIT_RELEVANT_STATS = frozenset(
    {
        model.STAT_DAMAGE,
        model.STAT_ADDED_DAMAGE,
        model.STAT_CRIT_CHANCE,
        model.STAT_CRIT_MULTIPLIER,
        model.STAT_PENETRATION,
        model.STAT_ATTACK_SPEED,
    }
)
DOT_RELEVANT_STATS = frozenset(
    {
        model.STAT_DAMAGE,
        model.STAT_PENETRATION,
        model.STAT_AILMENT_CHANCE,
        model.STAT_AILMENT_DURATION,
    }
)

DAMAGE_TYPES = ("Physical", "Fire", "Cold", "Lightning", "Void", "Necrotic", "Poison")


def _groups(pool_rows: Iterable[model.Contribution]) -> dict[str, float]:
    grouped: dict[str, float] = {}
    for row in pool_rows:
        key = row.group or row.contribution_id
        grouped[key] = grouped.get(key, 0.0) + float(row.value)
    return grouped


def _more_product(pool_rows: Iterable[model.Contribution]) -> tuple[float, dict[str, float]]:
    grouped = _groups(pool_rows)
    product = 1.0
    for value in grouped.values():
        product *= 1.0 + value
    return product, grouped


def more_product(pool_rows: Iterable[model.Contribution]) -> tuple[float, dict[str, float]]:
    """公开 more 乘区算子（计划执行期 handler 使用）。"""
    return _more_product(pool_rows)


def component_tags(
    component: dict[str, Any],
    damage_type: str,
    *,
    isolate_damage_channels: bool = False,
) -> list[str]:
    """构造一个伤害通道的标签闭包。

    默认（``isolate_damage_channels=False``）沿用历史口径：把当前
    ``damage_type`` 与组件声明的全部 tags 合并，因此公共 tags 里的其它
    伤害类型（如 Fire）也会进入 Physical/Cold/... 通道的闭包。

    开启类型隔离后，组件 tags 中**其它**伤害类型不再进入闭包，只保留当前
    通道的 ``damage_type`` 与行为标签（Bow/Minion/Hit/...）。这样一条
    ``[Bow, Fire]`` 的附加伤害行只可能匹配 Fire 通道，不会串入其它六种
    伤害组件。``damage_type`` 本身始终在闭包内，因此组件 tags 里声明的
    当前类型会保留（与历史口径一致）。
    """
    tags = [damage_type]
    for tag in component.get("tags") or []:
        if isolate_damage_channels and tag in DAMAGE_TYPES:
            # 当前 damage_type 已由上面第一项保留；其它伤害类型被隔离。
            continue
        if tag not in tags:
            tags.append(str(tag))
    return tags


def _tag_ok(
    row: model.Contribution,
    component: dict[str, Any],
    damage_type: str,
    *,
    isolate_damage_channels: bool = False,
) -> bool:
    return tags_match(
        row.damage_tags,
        component_tags(component, damage_type, isolate_damage_channels=isolate_damage_channels),
        DPS_TAG_POLICY,
    )


def _application_status(row: model.Contribution) -> tuple[str, Optional[str]]:
    """由贡献自身状态推断“为什么未参与”（映射阶段已确定的部分）。"""
    if not row.included:
        if row.status == model.STATUS_UNRESOLVED and row.stat_key == model.STAT_UNSUPPORTED:
            return UNRESOLVED, row.exclusion_reason
        return CONDITION_NOT_SATISFIED, row.exclusion_reason
    if row.condition_status not in model.ACTIVE_CONDITIONS:
        return CONDITION_NOT_SATISFIED, row.exclusion_reason or f"条件状态={row.condition_status}"
    return APPLIED, None


def _role_for(stat_key: str, operation: str) -> str:
    if stat_key == model.STAT_ADDED_DAMAGE:
        return "added_damage"
    if stat_key == model.STAT_DAMAGE:
        return "increased_pool" if operation == "increased" else (
            "more_pool" if operation == "more" else "damage_row"
        )
    return stat_key


def _record_applications(
    *,
    contributions: list[model.Contribution],
    component_id: str,
    relevant_stats: frozenset[str],
    applicable: set[str],
    scope_excluded: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in contributions:
        if row.stat_key not in relevant_stats:
            continue
        base_status, reason = _application_status(row)
        if base_status == APPLIED:
            if scope_excluded and row.contribution_id in scope_excluded:
                status, reason = NOT_APPLICABLE, "scope 不匹配该输出单元（owner/actor 隔离）"
            elif row.contribution_id in applicable:
                status = APPLIED
            else:
                status, reason = NOT_APPLICABLE, "标签不匹配该组件"
        else:
            status = base_status
        records.append(
            {
                "contribution_id": row.contribution_id,
                "component_id": component_id,
                "stat_key": row.stat_key,
                "operation": row.operation,
                "role": _role_for(row.stat_key, row.operation),
                "status": status,
                "reason": reason,
            }
        )
    return records


# ---------------------------------------------------------------------------
# 命中组件
# ---------------------------------------------------------------------------


def build_hit_components(
    *,
    contributions: list[model.Contribution],
    skill: dict[str, Any],
    component: dict[str, Any],
    crit_rules: dict[str, Any],
    target: dict[str, Any],
    states: dict[str, Any],
    target_info: dict[str, Any],
    damage_types: Optional[Iterable[str]] = None,
    typed_added_policy: str = "legacy",
    isolate_damage_channels: bool = False,
    unit_scope: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """装配所有命中伤害类型组件，返回 ``(components, applications)``。

    ``damage_types`` 缺省遍历全部伤害类型（v1 既有行为）；plan-v2 只传入声明
    组件类型。``typed_added_policy="by_type"`` 时，显式带伤害类型的 added 行只
    进入对应类型组件，无显式伤害类型的 added 行进入自适应类型组件——与迁移前
    hit 模型的组件口径一致。

    ``isolate_damage_channels``（默认 ``False``，保持既有案例逐字节不变）：
    开启后每个伤害通道的标签闭包只保留当前 ``damage_type`` 和行为标签，
    其它伤害类型标签被移除（见 :func:`component_tags`）；因此显式带类型的
    added 行只进入对应类型通道，无类型 added 行只进入
    ``adaptive_damage_type`` 通道，increased/more/crit/penetration 行也按
    同一个隔离闭包匹配。未被任何通道选用的 eligible 行仍由
    :func:`_record_applications` 记为 ``not_applicable``（reason=标签不匹配
    该组件）。

    参数保持独立：``isolate_damage_channels=True`` 与
    ``typed_added_policy="legacy"`` 同时出现时，added 行按 ``by_type`` 语义
    结算（legacy 的“任何带类型 added 进入所有类型通道”与类型隔离直接冲突；
    在隔离闭包下两者对 eligible 行的选择实际等价，这里显式归一为 by_type，
    不修改调用方传入的参数值，也不抛错中断历史调用）。
    """
    active_rows = [row for row in contributions if row.active]
    scope_excluded: set[str] = set()
    if unit_scope:
        wanted_scope = str(unit_scope)
        kept_rows: list[model.Contribution] = []
        for row in active_rows:
            row_scope = str((row.raw or {}).get("scope") or "all")
            if row_scope in (wanted_scope, "all"):
                kept_rows.append(row)
            else:
                scope_excluded.add(row.contribution_id)
        active_rows = kept_rows
    base_damage = {str(key): float(value) for key, value in (skill.get("base_damage") or {}).items()}
    adaptive = component.get("adaptive_damage_type")
    scaling = float(skill.get("added_damage_scaling") or 0.0)
    base_chance = float(skill.get("crit_chance") or 0.0)
    base_multiplier = float(skill.get("crit_multiplier") or 0.0)
    crit_capable = bool(component.get("crit_capable"))
    if crit_capable and base_multiplier <= 0:
        raise ValueError("命中组件声明可暴击，但 skill.crit_multiplier 未给出正数")
    if typed_added_policy not in ("legacy", "by_type"):
        raise ValueError(f"不支持的 typed_added_policy：{typed_added_policy!r}")
    effective_added_policy = typed_added_policy
    if isolate_damage_channels and typed_added_policy == "legacy":
        effective_added_policy = "by_type"
    target_stack = (crit_rules.get("target_stack_chance") or {})
    deadly_rule = (crit_rules.get("deadly") or {})
    components: list[dict[str, Any]] = []
    applications: list[dict[str, Any]] = []
    for damage_type in damage_types or DAMAGE_TYPES:
        tags = component_tags(
            component,
            damage_type,
            isolate_damage_channels=isolate_damage_channels,
        )
        eligible = [row for row in active_rows if tags_match(row.damage_tags, tags, DPS_TAG_POLICY)]
        added_pool = [
            row
            for row in eligible
            if row.stat_key == model.STAT_ADDED_DAMAGE and row.operation == "added"
        ]
        if effective_added_policy == "by_type":
            added_rows = []
            for row in added_pool:
                typed = set(row.damage_tags) & set(DAMAGE_TYPES)
                if typed:
                    if damage_type in typed:
                        added_rows.append(row)
                elif damage_type == adaptive:
                    added_rows.append(row)
        else:
            added_rows = [
                row
                for row in added_pool
                if set(row.damage_tags) & set(DAMAGE_TYPES) or damage_type == adaptive
            ]
        added = sum(float(row.value) for row in added_rows)
        base = base_damage.get(damage_type, 0.0)
        if not base and not added:
            continue
        increased_rows = [
            row
            for row in eligible
            if row.stat_key == model.STAT_DAMAGE and row.operation == "increased"
        ]
        more_rows = [
            row for row in eligible if row.stat_key == model.STAT_DAMAGE and row.operation == "more"
        ]
        inc = aggregate([float(row.value) for row in increased_rows]).factor
        more, more_groups = _more_product(more_rows)
        # actor crit 行在编译期已聚合进组件 base 字段（raw.crit_absorbed），
        # 这里不再重复计入 crit 池；但仍在应用账本中记为 applied。
        crit_absorbed_rows = [
            row
            for row in eligible
            if isinstance(row.raw, dict) and row.raw.get("crit_absorbed")
        ]
        crit_absorbed_ids = {row.contribution_id for row in crit_absorbed_rows}
        crit_chance_rows = [
            row
            for row in eligible
            if row.stat_key == model.STAT_CRIT_CHANCE
            and row.contribution_id not in crit_absorbed_ids
        ]
        crit_multiplier_rows = [
            row
            for row in eligible
            if row.stat_key == model.STAT_CRIT_MULTIPLIER
            and row.contribution_id not in crit_absorbed_ids
        ]
        if crit_capable:
            crit = expected_crit(
                capability=CritCapability(True, "dps"),
                pools=CritPools(
                    base_chance=base_chance,
                    base_multiplier=base_multiplier,
                    chance_added=sum(
                        float(row.value)
                        for row in crit_chance_rows
                        if row.operation == "added"
                    ),
                    chance_increased=sum(
                        float(row.value)
                        for row in crit_chance_rows
                        if row.operation == "increased"
                    ),
                    chance_more_product=(
                        _more_product([row for row in crit_chance_rows if row.operation == "more"])[0]
                    ),
                    multiplier_added=sum(
                        float(row.value)
                        for row in crit_multiplier_rows
                        if row.operation == "added"
                    ),
                    multiplier_increased=sum(
                        float(row.value)
                        for row in crit_multiplier_rows
                        if row.operation == "increased"
                    ),
                    multiplier_more_product=(
                        _more_product(
                            [row for row in crit_multiplier_rows if row.operation == "more"]
                        )[0]
                    ),
                ),
            )
            stack_count = 0
            if target_stack:
                stack_count = int(states.get(str(target_stack.get("control"))) or 0)
            chance = min(
                1.0,
                crit.crit_chance
                + stack_count * float(target_stack.get("per_stack") or 0.0),
            )
            deadly_chance = 0.0
            if deadly_rule and bool(states.get(str(deadly_rule.get("control")))):
                deadly_chance = float(deadly_rule.get("chance") or 0.0)
            deadly_added = float(deadly_rule.get("added_multiplier") or 0.0)
            crit_multiplier = crit.crit_multiplier
            average_crit = 1.0 + chance * (
                crit_multiplier - 1.0 + deadly_chance * deadly_added
            )
        else:
            chance = 0.0
            crit_multiplier = 1.0
            deadly_chance = 0.0
            average_crit = 1.0
        penetration_rows = [
            row for row in eligible if row.stat_key == model.STAT_PENETRATION
        ]
        penetration = sum(
            float(row.value)
            for row in penetration_rows
            if row.operation in ("added", "increased")
        )
        target_side = hit_component_target(
            resistance=float(target.get("resistance") or 0.0),
            penetration_total=penetration,
            monster_damage_reduction=float(target_info["monster_damage_reduction"]),
        )
        damage = (base + added * scaling) * inc * more * average_crit * target_side.multiplier
        requested_crit_chance = skill.get("crit_chance_unclamped")
        component_id = f"hit:{damage_type}"
        source_ids = [
            row.contribution_id
            for row in eligible
            if row.stat_key in (model.STAT_DAMAGE, model.STAT_CRIT_CHANCE, model.STAT_CRIT_MULTIPLIER, model.STAT_PENETRATION)
        ] + [row.contribution_id for row in added_rows]
        crit_absorbed_ids: set[str] = set()
        applicable = {row.contribution_id for row in eligible} | {
            row.contribution_id for row in added_rows
        } | crit_absorbed_ids
        components.append(
            {
                "id": component_id,
                "component_id": component_id,
                "kind": "hit",
                "damage_type": damage_type,
                "tags": tags,
                "crit_capable": crit_capable,
                "base": base,
                "added": added,
                "added_effectiveness": scaling,
                "subtotal": base + added * scaling,
                "increased_multiplier": inc,
                "more_multiplier": more,
                "more_groups": more_groups,
                "crit_chance": chance,
                "crit_chance_unclamped": requested_crit_chance,
                "crit_multiplier": crit_multiplier,
                "average_crit": average_crit,
                "deadly_chance_on_crit": deadly_chance,
                "penetration": penetration,
                "target_multiplier": target_side.multiplier,
                "damage": damage,
                "source_ids": source_ids,
                "target_side": target_side.to_dict(),
            }
        )
        applications.extend(
            _record_applications(
                contributions=contributions,
                component_id=component_id,
                relevant_stats=HIT_RELEVANT_STATS,
                applicable=applicable,
                scope_excluded=scope_excluded,
            )
        )
    return components, applications


# ---------------------------------------------------------------------------
# 持续伤害组件
# ---------------------------------------------------------------------------


def _action_chance_applicable(
    row: model.Contribution,
    *,
    source_kinds: Iterable[str],
    excluded_tags: Iterable[str],
) -> bool:
    if row.source_kind not in set(source_kinds):
        return False
    excluded = set(excluded_tags)
    return not (set(row.damage_tags) & excluded)


def build_dot_components(
    *,
    contributions: list[model.Contribution],
    ailments: list[dict[str, Any]],
    dot_component: Optional[dict[str, Any]] = None,
    penetration_rule: Optional[dict[str, Any]],
    target: dict[str, Any],
    target_info: dict[str, Any],
    additional_chance: Optional[dict[str, float]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """装配持续伤害组件。

    ``additional_chance`` 是已按来源验证过的额外施加概率（如技能节点），
    键为异常名；不在这里读取技能/状态专用键。
    返回 ``(ailments, applications, chance_notes)``。
    """
    active_rows = [row for row in contributions if row.active]
    additional_chance = additional_chance or {}
    dot_component = dot_component or {"tags": ["DoT"]}
    results: list[dict[str, Any]] = []
    applications: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    for declared in ailments:
        name = str(declared.get("name") or "")
        if not name:
            raise ValueError("ailment 缺少 name")
        damage_type = str(declared.get("damage_type") or "")
        if damage_type not in DAMAGE_TYPES:
            raise ValueError(f"ailment {name!r} 的 damage_type 不合法：{damage_type!r}")
        component_id = f"dot:{name}"
        tags = component_tags(dot_component, damage_type)
        eligible = [row for row in active_rows if tags_match(row.damage_tags, tags, DPS_TAG_POLICY)]
        increased_rows = [
            row for row in eligible if row.stat_key == model.STAT_DAMAGE and row.operation == "increased"
        ]
        more_rows = [row for row in eligible if row.stat_key == model.STAT_DAMAGE and row.operation == "more"]
        inc = aggregate([float(row.value) for row in increased_rows]).factor
        more, more_groups = _more_product(more_rows)
        penetration_rows = [row for row in eligible if row.stat_key == model.STAT_PENETRATION]
        penetration = sum(
            float(row.value) for row in penetration_rows if row.operation in ("added", "increased")
        )
        chance_rule = declared.get("chance") or {}
        source_kinds = chance_rule.get("source_kinds") or ["equipment", "idol", "blessing", "idol_altar"]
        excluded_tags = chance_rule.get("excluded_tags") or ["Spell", "Bow", "Throwing", "Minion"]
        target_key = str(declared.get("ailment_id")) if declared.get("ailment_id") is not None else name
        chance_rows = [
            row
            for row in active_rows
            if row.stat_key == model.STAT_AILMENT_CHANCE and str(row.target) == target_key
        ]
        chance_from_rows = 0.0
        for row in chance_rows:
            if _action_chance_applicable(row, source_kinds=source_kinds, excluded_tags=excluded_tags):
                chance_from_rows += float(row.value)
            else:
                applications.append(
                    {
                        "contribution_id": row.contribution_id,
                        "component_id": component_id,
                        "stat_key": row.stat_key,
                        "operation": row.operation,
                        "role": "ailment_chance",
                        "status": UNRESOLVED,
                        "reason": chance_rule.get("unmodeled_reason")
                        or "该施加概率来源与当前技能/目标条件不匹配，未纳入概率模型",
                    }
                )
        chance = chance_from_rows + float(additional_chance.get(name) or 0.0)
        duration_inc_rows = [
            row
            for row in active_rows
            if row.stat_key == model.STAT_AILMENT_DURATION
            and (str(row.target) == name or name in str(row.stat_name))
        ]
        duration_increased = sum(float(row.value) for row in duration_inc_rows)
        base_duration = float(declared.get("duration_s") or 0.0)
        if base_duration <= 0:
            raise ValueError(f"ailment {name!r} 的 duration_s 必须 > 0")
        duration = base_duration * (1.0 + duration_increased)
        penetration_extra = 0.0
        if penetration_rule and str(penetration_rule.get("target")) == name:
            rule = penetration_rule
            ratio = float(rule.get("ratio") or 0.0)
            input_tags = list((rule.get("input") or {}).get("tags") or [])
            input_stat = str((rule.get("input") or {}).get("stat") or model.STAT_DAMAGE)
            input_operation = str((rule.get("input") or {}).get("operation") or "increased")
            input_rows = [
                row
                for row in active_rows
                if row.stat_key == input_stat
                and row.operation == input_operation
                and tags_match(row.damage_tags, input_tags, DPS_TAG_POLICY)
            ]
            input_total = sum(float(row.value) for row in input_rows)
            penetration_extra = max(0.0, input_total) * ratio
            penetration += penetration_extra
        target_side = hit_component_target(
            resistance=float(target.get("resistance") or 0.0),
            penetration_total=penetration,
            monster_damage_reduction=float(target_info["monster_damage_reduction"]),
        )
        base_damage = float(declared.get("base_damage") or 0.0)
        damage = base_damage * (1.0 + duration_increased) * inc * more * target_side.multiplier
        if chance > 1.0:
            notes.append(
                {
                    "ailment": name,
                    "chance": chance,
                    "issue": "施加概率 > 100%；当前按线性期望计入，超过 100% 是否产生额外层数未确认（假设）",
                }
            )
        applicable = {row.contribution_id for row in eligible}
        results.append(
            {
                "id": component_id,
                "component_id": component_id,
                "kind": "dot",
                "name": name,
                "damage_type": damage_type,
                "tags": tags,
                "crit_capable": False,
                "chance": chance,
                "chance_from_sources": chance_from_rows,
                "chance_from_rules": float(additional_chance.get(name) or 0.0),
                "duration_s": duration,
                "duration_increased": duration_increased,
                "base_damage": base_damage,
                "stack_damage": damage,
                "per_stack_dps": damage / duration if duration > 0 else 0.0,
                "damage_per_cast": damage * chance,
                "increased_multiplier": inc,
                "more_multiplier": more,
                "more_groups": more_groups,
                "penetration": penetration,
                "penetration_from_rule": penetration_extra,
                "target_multiplier": target_side.multiplier,
                "source_ids": [row.contribution_id for row in eligible],
            }
        )
        applications.extend(
            _record_applications(
                contributions=contributions,
                component_id=component_id,
                relevant_stats=DOT_RELEVANT_STATS,
                applicable=applicable,
            )
        )
    return results, applications, notes


# ---------------------------------------------------------------------------
# 频率与施放规则
# ---------------------------------------------------------------------------


def attack_speed_increased(
    *,
    contributions: list[model.Contribution],
    component_tags: list[str],
) -> float:
    """攻击速度加算池：按命中组件声明标签筛选（不假定近战）。"""
    total = 0.0
    for row in contributions:
        if not row.active:
            continue
        if row.stat_key != model.STAT_ATTACK_SPEED or row.operation not in ("added", "increased"):
            continue
        if tags_match(row.damage_tags, component_tags, DPS_TAG_POLICY):
            total += float(row.value)
    return total


def cooldown_recovery_increased(contributions: list[model.Contribution]) -> float:
    total = 0.0
    for row in contributions:
        if not row.active:
            continue
        if row.stat_key != model.STAT_COOLDOWN_RECOVERY:
            continue
        if row.operation not in ("added", "increased"):
            continue
        if row.target or row.condition_code:
            continue
        total += float(row.value)
    return total


def cast_interval(
    *,
    cooldown_s: float,
    action_s: float,
    cooldown_reduction: float,
    cooldown_recovery: float,
    attack_speed: float,
) -> dict[str, float]:
    """施放节奏：冷却与动作时长取较大者；非法输入明确拒绝。"""
    for label, value in (
        ("cooldown_s", cooldown_s),
        ("action_s", action_s),
        ("cooldown_reduction", cooldown_reduction),
        ("cooldown_recovery", cooldown_recovery),
        ("attack_speed", attack_speed),
    ):
        if not math.isfinite(float(value)):
            raise ValueError(f"{label} 必须是有限数值")
    if cooldown_s < 0 or action_s < 0:
        raise ValueError("冷却与动作时长不能为负")
    cooldown = cooldown_s * (1.0 - cooldown_reduction) / (1.0 + max(0.0, cooldown_recovery))
    action = action_s / (1.0 + max(0.0, attack_speed))
    interval = max(cooldown, action)
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("施放间隔无效（冷却与动作时长都为 0）")
    return {"cooldown_s": cooldown, "action_s": action, "interval_s": interval}


def hit_windows(
    *,
    single_hit: float,
    ailments: list[dict[str, Any]],
    interval: float,
    every_nth: Optional[dict[str, Any]],
    windows_s: tuple[float, ...] = (5, 30, 120),
    first_cast_s: float = 0.0,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """窗口与稳定态 DPS；每第 N 次命中只作用于命中组件。

    ``first_cast_s`` 为首次施放时刻（默认 0，与第一步一致）；窗口不含右端点。
    """
    if not math.isfinite(single_hit) or not math.isfinite(interval) or interval <= 0:
        raise ValueError("命中伤害或施放间隔无效")
    if not math.isfinite(first_cast_s) or first_cast_s < 0:
        raise ValueError("首次施放时刻必须是非负有限数值")
    n = int((every_nth or {}).get("n") or 0)
    extra = float((every_nth or {}).get("extra_more") or 0.0)
    if (every_nth or {}) and n < 1:
        raise ValueError("每第 N 次命中的 N 必须 >= 1")
    windows: list[dict[str, Any]] = []
    for raw_seconds in windows_s:
        seconds = float(raw_seconds)
        if seconds <= 0:
            raise ValueError("观察窗口必须是正数")
        if first_cast_s >= seconds:
            count = 0
        else:
            count = math.ceil((seconds - first_cast_s) / interval - 1e-12)
        hit_damage = single_hit * (count + math.floor(count / n) * extra if n else count)
        dot_damage = sum(
            float(ailment["damage_per_cast"])
            * sum(
                min(
                    1.0,
                    max(0.0, seconds - (first_cast_s + index * interval))
                    / float(ailment["duration_s"]),
                )
                for index in range(count)
            )
            for ailment in ailments
        )
        windows.append(
            {
                "seconds": seconds,
                "casts": count,
                "hit_damage": hit_damage,
                "dot_damage": dot_damage,
                "damage": hit_damage + dot_damage,
                "dps": (hit_damage + dot_damage) / seconds,
            }
        )
    steady_hit = single_hit / interval * (1.0 + (extra / n if n else 0.0))
    steady_dot = sum(float(ailment["damage_per_cast"]) for ailment in ailments) / interval
    return windows, {"hit_dps": steady_hit, "dot_dps": steady_dot, "sustained_dps": steady_hit + steady_dot}


__all__ = [
    "APPLIED",
    "APPLICATION_STATUSES",
    "CONDITION_NOT_SATISFIED",
    "DAMAGE_TYPES",
    "DOT_RELEVANT_STATS",
    "DPS_TAG_POLICY",
    "DUPLICATE",
    "HIT_RELEVANT_STATS",
    "NOT_APPLICABLE",
    "SOURCE_ABSENT",
    "UNRESOLVED",
    "attack_speed_increased",
    "build_dot_components",
    "build_hit_components",
    "cast_interval",
    "cooldown_recovery_increased",
    "component_tags",
    "hit_windows",
    "more_product",
]
