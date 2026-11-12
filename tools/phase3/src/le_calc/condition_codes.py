"""场景状态 / 条件代码求值（共享）。

原本只存在于 ``hit_damage`` 的条件求值逻辑抽到这里，供命中文档与统一 DPS 路径
共同调用；两边的差异只在于各自传入的状态字典内容，不在求值语义。

条件代码是**数据词汇**（来自 calculation-input / ruleset 的条件字符串），
例如 ``mana_ge_300``、``dual_wield``、``passive_condition:...``。未知代码返回
``None``，调用方必须记录为 unknown，不能默认当成 false。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

#: 基线场景状态（命中模型文档的 task-2 契约：所有条件 Buff 默认关闭）。
DEFAULT_HIT_STATES: dict[str, Any] = {
    "ignited": False,
    "enchant_weapon_active": False,
    "frenzy_active": False,
    "rhythm_of_fire_stacks": 0,
    "consume_rhythm_of_fire": False,
    "firebrand_stacks": 0,
    "consume_firebrand": False,
    "blade_weaving_stacks": 0,
    "consume_blade_weaving": False,
    "ambition_stacks": 0,
    "enemy_high_health": False,
    "mana_ge_300": False,
    "boss_or_rare": True,
    "target_in_melee_range": True,
}

#: 场景文档中的状态别名（同一语义的多个键名）。
STATE_ALIASES = {
    "frenzy": ("frenzy", "frenzy_active", "frenzy_active_flag"),
    "enchant_weapon_active": ("enchant_weapon_active",),
    "ignited": ("ignited",),
    "mana_ge_300": ("mana_ge_300",),
    "boss_or_rare": ("boss_or_rare",),
    "dual_wield": ("dual_wield",),
    "melee_ability_mana_cost": ("melee_ability_mana_cost",),
    "rhythm_consume": (
        "consume_rhythm_of_fire",
        "consume_rhythm",
        "rhythm_consume",
    ),
    "firebrand_stacks": ("firebrand_stacks", "consume_firebrand"),
    "blade_weaving_stacks": ("blade_weaving_stacks", "consume_blade_weaving"),
    "rhythm_stacks": ("rhythm_of_fire_stacks", "rhythm_stacks"),
}


def first_state(states: dict[str, Any], names: Iterable[str]) -> Any:
    """返回第一个存在状态的取值；都缺失时返回 ``None``。"""
    for name in names:
        if name in states:
            return states[name]
    return None


def default_states() -> dict[str, Any]:
    """命中模型/统一 DPS 共用的默认状态字典副本。"""
    return dict(DEFAULT_HIT_STATES)


def evaluate_condition(code: Optional[str], states: dict[str, Any]) -> Optional[bool]:
    """按 ``+`` 拆分条件代码逐项求值；任一项未知返回 ``None``。"""
    if not code:
        return True
    states = states or {}
    parts = [part.strip() for part in str(code).split("+") if part.strip()]
    results: list[Optional[bool]] = []
    for part in parts:
        results.append(evaluate_atom(part, states))
    if any(result is None for result in results):
        return None
    return all(results)


def evaluate_atom(atom: str, states: dict[str, Any]) -> Optional[bool]:
    """单个条件原子的求值；未知原子返回 ``None``（fail closed）。"""
    if atom.startswith("passive_condition:"):
        return None  # 未映射的 planner 条件：未知，不默认成立
    if atom in (
        "ignited",
        "enchant_weapon_active",
        "mana_ge_300",
        "boss_or_rare",
        "dual_wield",
        "melee_ability_mana_cost",
        "frenzy",
        "rhythm_consume",
    ):
        value = first_state(states, STATE_ALIASES.get(atom, (atom,)))
        return None if value is None else bool(value)
    if atom == "firebrand_stacks":
        value = first_state(states, STATE_ALIASES["firebrand_stacks"])
        if isinstance(value, bool):
            return value
        try:
            return float(value) > 0
        except (TypeError, ValueError):
            return None
    if atom == "blade_weaving_stacks":
        value = first_state(states, STATE_ALIASES["blade_weaving_stacks"])
        if isinstance(value, bool):
            return value
        try:
            return float(value) > 0
        except (TypeError, ValueError):
            return None
    if atom == "rhythm_consume":
        explicit = states.get("consume_rhythm_of_fire")
        if explicit is not None:
            return bool(explicit)
        stacks = first_state(states, STATE_ALIASES["rhythm_stacks"])
        try:
            return float(stacks or 0) >= 12.0
        except (TypeError, ValueError):
            return None
    if atom in ("chilled", "frozen"):
        value = states.get(atom)
        return None if value is None else bool(value)
    return None


__all__ = [
    "DEFAULT_HIT_STATES",
    "STATE_ALIASES",
    "default_states",
    "evaluate_atom",
    "evaluate_condition",
    "first_state",
]
