"""Buff 设置页（玩家版）：勾选需要的 Buff、保存/载入配置、计算 DPS。

对应 ``view["conditions"]``（条件 Buff 配置数据）。页面按
``作者本地历史记录（不随公开仓库分发）`` §3/§4/§8/
§9.6/§10 收敛为玩家操作界面：

1. 首屏是预设、保存/载入、重置、搜索/来源筛选与 Buff 卡片；
2. 卡片只显示玩家需要的信息：名称、来源技能、属性/数值、实际生效条件、
   一个主状态（生效 / 仅本次施法 / 暂不可用 / 未启用）和控件；
3. 计算按钮 + 简短状态（“设置已更改”等）放在紧凑操作栏里；
4. 页尾只有一条乘区公式和紧凑说明，不再渲染 11 个开发者模块。

工程统计、支持矩阵、数据缺口、检查分母、证据/机制/拟议贡献、JSON/schema 说明
等不在此页渲染（保留在导出数据与工程文档中）。

页面保留 ``app.js`` 依赖的 DOM 钩子（其余隐藏空节点用于状态/条件同步）：
``[data-cond-toggle]``、``[data-cond-stacks]``、``[data-cond-cast]``、
``[data-cond-preset]``、``[data-cond-preset-apply]``、``[data-cond-export]``、
``[data-cond-import]``、``[data-cond-reset]``、``[data-cond-live-recompute]``、
``[data-cond-live-status]``、``[data-cond-changed]``、``[data-cond-search]``、
``[data-cond-category]``、``[data-cond-count]``、``[data-cond-visible]``、
``[data-cond-empty]``、``[data-cond-row]``、``[data-effect-id]``、
``[data-cond-status-for]``、``[data-cond-proposal-for]``、
``[data-cond-status-reason-for]``、``[data-cond-check-for]``。
目标条件（Boss/稀有、中心 1 米、双星、冰缓/冻结）不再由本页编辑。
"""

from __future__ import annotations

import re

from .. import components as c
from .. import shared_ui
from .. import labels

__all__ = ["render_conditions"]

_TITLE = "Buff 设置"

#: 状态色调沿用报告既有 badge 语义；页面只给玩家看一个主状态。
_TONE_OK = "ok"
_TONE_INFO = "info"
_TONE_MUTED = "muted"

#: 玩家主状态标签（只保留一个）。工程状态在 app.js 中仅写入隐藏钩子。
_STATUS_ACTIVE = "生效"
_STATUS_SINGLE_CAST = "仅本次施法"
_STATUS_UNAVAILABLE = "暂不可用"
_STATUS_OFF = "未启用"

_CONFIGURABLE_CONTROLS = ("toggle", "stacks", "cast_state")

#: §10 乘区说明（紧凑表，放在卡片之后）。
_SCALING_ROWS = [
    ("基础伤害", "技能基础值与附加点伤"),
    ("加算增伤 inc", "适用的 increased 相加"),
    ("独立增伤 more", "按当前规则组合的独立倍率"),
    ("抗性与穿透", "目标抗性、减抗与穿透"),
    ("目标减伤", "当前目标的伤害减免"),
    ("数量与时间", "次数、层数、持续时间与施放间隔；不是额外伤害乘区"),
]

_SCALING_FORMULA = "基础伤害 → 加算增伤 inc → 独立增伤 more → 抗性与穿透 → 目标减伤"

_PAREN_RE = re.compile(r"[（(][^（）()]*[）)]")
_MULTI_SPACE_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _s(value, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _d(value) -> dict:
    return value if isinstance(value, dict) else {}


def _l(value) -> list:
    return value if isinstance(value, list) else []


def _strip_parentheses(text: str) -> str:
    return _MULTI_SPACE_RE.sub(" ", _PAREN_RE.sub("", text)).strip()


def _category_label(effect: dict) -> str:
    category = _s(effect.get("category"))
    return _s(effect.get("category_label")) or (
        labels.CATEGORY_LABELS.get(category) if category else ""
    ) or "其他"


def _baseline_scenario(conditions: dict) -> dict:
    return _d(_d(conditions.get("client_rules")).get("baseline_scenario"))


def _initial_scenario(conditions: dict) -> dict:
    """The scenario the generated page represents (applied scenario wins)."""

    scenario = conditions.get("scenario")
    if isinstance(scenario, dict) and scenario:
        return scenario
    baseline = _baseline_scenario(conditions)
    return baseline if isinstance(baseline, dict) else {}


def _control_kind(effect: dict) -> str:
    return _s(_d(effect.get("control")).get("kind"))


def _is_configurable(effect: dict) -> bool:
    return _control_kind(effect) in _CONFIGURABLE_CONTROLS


def _selected(scenario: dict, effect: dict) -> bool:
    """Whether the given scenario selects this effect (toggle/stacks/cast)."""

    effect_id = _s(effect.get("effect_id"))
    control = _d(effect.get("control"))
    kind = _s(control.get("kind"))
    entry = _d(_d(scenario.get("effects")).get(effect_id))
    if kind == "toggle":
        return entry.get("enabled") is True
    if kind == "stacks":
        stacks = entry.get("stacks")
        if stacks is None:
            stacks = _d(scenario.get("states")).get(_s(control.get("stack_state_id")))
        try:
            return int(stacks) > 0
        except (TypeError, ValueError):
            return False
    if kind == "cast_state":
        key = _s(control.get("cast_state_id")) or _s(control.get("state_id")) or effect_id
        return _d(scenario.get("single_cast")).get(key) is True
    return False


def _configured_count(conditions: dict) -> int:
    scenario = _initial_scenario(conditions)
    return sum(
        1
        for effect in _l(conditions.get("effects"))
        if isinstance(effect, dict) and _is_configurable(effect) and _selected(scenario, effect)
    )


def _application_state(conditions: dict, effect_id: str) -> dict:
    """Per-effect application state of a recomputed (scenario-applied) report."""

    if conditions.get("scenario_applied") is not True:
        return {}
    recompute = _d(conditions.get("recompute"))
    states = _d(recompute.get("application_states"))
    if isinstance(states.get(effect_id), dict):
        return dict(states[effect_id])
    application = _d(recompute.get("condition_application"))
    for item in _l(application.get("applied")):
        if isinstance(item, dict) and _s(item.get("effect_id")) == effect_id:
            scope = _s(item.get("scope"))
            return {
                "state": "applied_single" if scope == "single_cast" else "applied_both",
                "reason": "",
            }
    for item in _l(application.get("skipped")):
        if isinstance(item, dict) and _s(item.get("effect_id")) == effect_id:
            return {"state": "skipped", "reason": _s(item.get("detail")) or _s(item.get("reason"))}
    return {"state": "not_selected", "reason": ""}


def _channel_usable(conditions: dict, effect: dict) -> bool:
    """Whether the conditional recompute channel can apply this effect.

    ``recompute.channel_support`` is authoritative when the report was generated
    with a scenario; otherwise fall back to the support level (rule mapping is
    usable by the cross-skill channel, action/time simulation is not).
    """

    recompute = _d(conditions.get("recompute"))
    support = _d(recompute.get("channel_support")).get(_s(effect.get("effect_id")))
    if isinstance(support, dict):
        state = _s(support.get("state"))
        if state == "supported":
            return True
        if state in ("skipped", "unsupported"):
            return False
    level = _s(_d(effect.get("support")).get("level"))
    return level in ("engine_supported", "needs_rule_mapping")


def _channel_state(conditions: dict, effect: dict) -> str:
    """Channel-support state for a static (non-recomputed) report."""

    recompute = _d(conditions.get("recompute"))
    support = _d(recompute.get("channel_support")).get(_s(effect.get("effect_id")))
    return _s(support.get("state")) if isinstance(support, dict) else ""


def _is_baseline_effect(effect: dict) -> bool:
    initial = _s(_d(effect.get("status")).get("initial"))
    relation = _s(_d(effect.get("relevance")).get("baseline_relation"))
    return relation == "already_in_baseline" or initial in (
        "already_in_baseline",
        "active_supported",
        "auto_active",
    )


def _player_status(conditions: dict, effect: dict) -> tuple[str, str]:
    """One player-facing status: 生效 / 仅本次施法 / 暂不可用 / 未启用."""

    application = _application_state(conditions, _s(effect.get("effect_id")))
    if application:
        state = _s(application.get("state"))
        if state in ("applied_both", "applied_loop", "baseline"):
            return _STATUS_ACTIVE, _TONE_OK
        if state == "applied_single":
            return _STATUS_SINGLE_CAST, _TONE_INFO
        if state == "skipped":
            return _STATUS_UNAVAILABLE, _TONE_MUTED
        if state == "not_selected":
            # Non-configurable baseline/auto effects stay active even though the
            # scenario did not "select" them.
            if not _is_configurable(effect) and _is_baseline_effect(effect):
                return _STATUS_ACTIVE, _TONE_OK
            return _STATUS_OFF, _TONE_MUTED
    support_level = _s(_d(effect.get("support")).get("level"))
    if not _is_configurable(effect):
        channel = _channel_state(conditions, effect)
        if channel == "baseline" or _is_baseline_effect(effect):
            return _STATUS_ACTIVE, _TONE_OK
        if (
            channel == "skipped"
            or _s(_d(effect.get("status")).get("initial")) in ("unsupported_action", "auto_unknown")
            or support_level in ("needs_action_or_time_simulation", "mechanism_unresolved")
        ):
            return _STATUS_UNAVAILABLE, _TONE_MUTED
        return _STATUS_OFF, _TONE_MUTED
    if not _channel_usable(conditions, effect):
        return _STATUS_UNAVAILABLE, _TONE_MUTED
    if _selected(_initial_scenario(conditions), effect):
        if _control_kind(effect) == "cast_state":
            return _STATUS_SINGLE_CAST, _TONE_INFO
        return _STATUS_ACTIVE, _TONE_OK
    return _STATUS_OFF, _TONE_MUTED


# --------------------------------------------------------------------------- #
# display name / source / values / condition
# --------------------------------------------------------------------------- #


def _display_name(effect: dict) -> tuple[str, str]:
    """Return ``(title, hint)``; move trailing parenthetical hints to the subtitle."""

    name = _s(effect.get("name")) or _s(effect.get("effect_id"))
    hint = ""
    match = re.search(r"[（(]([^（）()]+)[）)]\s*$", name)
    if match:
        hint = match.group(1).strip()
        name = name[: match.start()].strip()
    source_detail = _d(effect.get("source_detail"))
    item = _s(source_detail.get("item"))
    slot = _s(source_detail.get("slot"))
    # Parentheticals carrying internal ids ("idols.2") are not player copy.
    if hint and (hint == slot or re.fullmatch(r"[a-z0-9_.]+", hint)):
        hint = ""
    if "·" in name and item:
        prefix, rest = name.split("·", 1)
        if prefix.strip() == item:
            name = rest.strip()
    return name or _s(effect.get("effect_id")), hint


def _slot_text(slot: str) -> str:
    if not slot:
        return ""
    if slot.startswith("idols."):
        return "神像 " + slot.split(".")[-1]
    if slot.startswith("equipment."):
        return labels.SLOT_LABELS.get(slot.split(".", 1)[-1], "")
    return labels.SLOT_LABELS.get(slot, slot)


def _source_secondary(effect: dict, group_heading: str = "") -> str:
    """Second line: source skill/item (Meteor, Enchant Weapon, ring …)."""

    source_detail = _d(effect.get("source_detail"))
    kind = _s(source_detail.get("kind"))
    title, hint = _display_name(effect)
    parts: list[str] = []
    if kind in ("skill_node", "main_skill_node", "passive_node"):
        tree = _s(source_detail.get("tree"))
        if tree:
            parts.append(tree)
    else:
        item = _s(source_detail.get("item"))
        if item and item != title:
            parts.append(item)
        slot_text = _slot_text(_s(source_detail.get("slot")))
        if slot_text:
            parts.append(slot_text)
    if hint:
        parts.append(hint)
    text = " · ".join(parts) if parts else _category_label(effect)
    if group_heading and text.startswith(group_heading):
        trimmed = text[len(group_heading) :].lstrip(" ·")
        text = trimmed or group_heading
    return text


def _clean_attr_label(label: str) -> str:
    """Drop defensive parentheticals but keep meaningful qualifiers."""

    keep = "（非伤害）" if "非伤害" in label else ""
    cleaned = _strip_parentheses(label)
    if keep and keep not in cleaned:
        cleaned = cleaned + keep
    return cleaned or label


def _value_pairs(effect: dict) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in _l(effect.get("value_lines")):
        if not isinstance(line, dict):
            continue
        value_text = _s(line.get("value_text"))
        if not value_text:
            continue
        label_text = _clean_attr_label(_s(line.get("label"))) or "数值"
        pairs.append((label_text, f'<b class="cond-value-text">{c.esc(value_text)}</b>'))
    return pairs


def _short_condition_text(text: str) -> str:
    text = _strip_parentheses(text)
    for separator in ("；", "。"):
        if separator in text:
            text = text.split(separator, 1)[0]
    text = text.strip(" 　")
    if len(text) > 42:
        text = text[:40].rstrip() + "…"
    return text


def _condition_line(conditions: dict, effect: dict) -> str:
    effect_id = _s(effect.get("effect_id"))
    control = _d(effect.get("control"))
    self_states = {
        state_id
        for state_id in (_s(control.get("state_id")), _s(control.get("stack_state_id")))
        if state_id
    }
    check_labels = {
        _s(check.get("state_id")): _s(check.get("label"))
        for check in _l(effect.get("condition_checks"))
        if isinstance(check, dict) and _s(check.get("state_id"))
    }
    rules_effect = next(
        (
            item
            for item in _l(_d(conditions.get("client_rules")).get("effects"))
            if isinstance(item, dict) and _s(item.get("effect_id")) == effect_id
        ),
        {},
    )
    condition = _d(rules_effect.get("condition"))

    def _group_labels(entries) -> list[str]:
        found: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            state_id = _s(entry.get("state"))
            if state_id in self_states:
                # A toggle/stacks control already declares its own state.
                continue
            label_text = _s(entry.get("label")) or check_labels.get(state_id, "")
            cleaned = re.sub(r"^假设\s*", "", label_text).strip()
            cleaned = _strip_parentheses(cleaned)
            if cleaned and cleaned not in found:
                found.append(cleaned)
        return found

    all_labels = _group_labels(_l(condition.get("all")))
    any_labels = _group_labels(_l(condition.get("any")))
    clauses: list[str] = []
    if all_labels:
        clauses.append("、".join(all_labels))
    if any_labels:
        clauses.append("或".join(any_labels))
    if clauses:
        text = "需要：" + "；".join(clauses)
    else:
        fallback_labels: list[str] = []
        for state_id, label_text in check_labels.items():
            if state_id in self_states:
                continue
            cleaned = _strip_parentheses(re.sub(r"^假设\s*", "", label_text).strip())
            if cleaned and cleaned not in fallback_labels:
                fallback_labels.append(cleaned)
        if fallback_labels:
            text = "需要：" + "、".join(fallback_labels)
        elif not self_states:
            text = _short_condition_text(_s(effect.get("condition_text")))
        else:
            text = ""
    if "无需" in text:
        text = ""
    if not text:
        return ""
    return f'<p class="cond-condition"><span class="cond-field-label">条件</span>{c.esc(text)}</p>'


# --------------------------------------------------------------------------- #
# controls + hidden hooks
# --------------------------------------------------------------------------- #


def _control_html(conditions: dict, effect: dict) -> str:
    effect_id = _s(effect.get("effect_id"))
    control = _d(effect.get("control"))
    kind = _s(control.get("kind"))
    scenario = _initial_scenario(conditions)
    title, _hint = _display_name(effect)
    if kind == "toggle":
        checked = " checked" if _selected(scenario, effect) else ""
        return (
            '<label class="cond-toggle">'
            f'<input type="checkbox" data-cond-toggle="{c.esc(effect_id)}" '
            f'aria-label="启用 {c.esc(title)}"{checked}> <span class="cond-control-label">启用</span>'
            "</label>"
        )
    if kind == "stacks":
        bounds = _d(control.get("bounds"))
        minimum = bounds.get("min")
        maximum = bounds.get("max")
        entry = _d(_d(scenario.get("effects")).get(effect_id))
        stacks = entry.get("stacks")
        if stacks is None:
            stacks = _d(scenario.get("states")).get(_s(control.get("stack_state_id")))
        try:
            value = max(int(minimum or 0), int(stacks or 0))
        except (TypeError, ValueError):
            value = 0
        if isinstance(maximum, (int, float)):
            value = min(int(maximum), value)
        min_attr = f' min="{int(minimum)}"' if isinstance(minimum, (int, float)) else ""
        max_attr = f' max="{int(maximum)}"' if isinstance(maximum, (int, float)) else ""
        return (
            '<label class="cond-stacks">'
            f'<input type="number" inputmode="numeric" step="1"{min_attr}{max_attr} '
            f'data-cond-stacks="{c.esc(effect_id)}" value="{value}" '
            f'aria-label="{c.esc(title)} 层数"> <span class="cond-control-label">层</span>'
            "</label>"
        )
    if kind == "cast_state":
        key = _s(control.get("cast_state_id")) or _s(control.get("state_id")) or effect_id
        checked = " checked" if _d(scenario.get("single_cast")).get(key) is True else ""
        return (
            '<label class="cond-toggle">'
            f'<input type="checkbox" data-cond-cast="{c.esc(key)}" '
            f'aria-label="{c.esc(title)} 仅在本次施法生效"{checked}> '
            '<span class="cond-control-label">本次施法</span></label>'
        )
    return ""


def _hidden_hooks(effect: dict) -> str:
    """Hidden nodes consumed by app.js (status/condition sync). Never visible."""

    effect_id = _s(effect.get("effect_id"))
    parts = [
        f'<span hidden data-cond-status-for="{c.esc(effect_id)}"></span>',
        f'<span hidden data-cond-proposal-for="{c.esc(effect_id)}"></span>',
        f'<span hidden data-cond-status-reason-for="{c.esc(effect_id)}"></span>',
    ]
    for check in _l(effect.get("condition_checks")):
        if not isinstance(check, dict):
            continue
        state_id = _s(check.get("state_id"))
        if not state_id:
            continue
        parts.append(
            f'<span hidden data-cond-check-for="{c.esc(effect_id)}" '
            f'data-cond-check-state="{c.esc(state_id)}"></span>'
        )
    return "".join(parts)


# --------------------------------------------------------------------------- #
# effect card
# --------------------------------------------------------------------------- #


def _search_text(effect: dict) -> str:
    title, hint = _display_name(effect)
    pieces = [
        title,
        hint,
        _s(effect.get("name_en")),
        _s(effect.get("source_label")),
        _source_secondary(effect),
        _category_label(effect),
        _s(effect.get("condition_text")),
    ]
    for line in _l(effect.get("value_lines")):
        if isinstance(line, dict):
            pieces.append(_s(line.get("label")))
            pieces.append(_s(line.get("value_text")))
    for check in _l(effect.get("condition_checks")):
        if isinstance(check, dict):
            pieces.append(_s(check.get("label")))
    return " ".join(piece.strip() for piece in pieces if piece.strip()).lower()


def _effect_card(conditions: dict, effect: dict, group_heading: str = "") -> str:
    effect_id = _s(effect.get("effect_id"))
    title, _hint = _display_name(effect)
    subtitle = _source_secondary(effect, group_heading)
    status_label, status_tone = _player_status(conditions, effect)
    control_html = _control_html(conditions, effect)
    head = (
        '<header class="cond-effect-head">'
        '<div class="cond-effect-title">'
        f'<h4 class="cond-effect-name">{c.esc(title)}</h4>'
        + (
            f'<p class="cond-effect-en muted small">{c.esc(subtitle)}</p>'
            if subtitle
            else ""
        )
        + "</div>"
        '<div class="cond-effect-badges">'
        f'<span class="badge badge-{c.esc(status_tone)} cond-status">{c.esc(status_label)}</span>'
        f"{control_html}</div></header>"
    )
    value_pairs = _value_pairs(effect)
    values_html = c.fact_list(value_pairs, cls="cond-values") if value_pairs else ""
    body = head + values_html + _condition_line(conditions, effect) + _hidden_hooks(effect)

    relevance = _d(effect.get("relevance"))
    support = _d(effect.get("support"))
    status = _d(effect.get("status"))
    attrs = (
        " data-cond-row"
        f' data-effect-id="{c.esc(effect_id)}"'
        f' data-category="{c.esc(_s(effect.get("category")))}"'
        f' data-relevance="{c.esc(_s(relevance.get("status")))}"'
        f' data-support="{c.esc(_s(support.get("level")))}"'
        f' data-status="{c.esc(_s(status.get("initial")) or "not_selected")}"'
        f' data-search="{c.esc(_search_text(effect))}"'
    )
    card_cls = "cond-effect" if _is_configurable(effect) else "cond-effect cond-effect-readonly"
    return shared_ui.effect_card(
        title=title,
        subtitle=subtitle,
        status_label=status_label,
        status_tone=status_tone,
        control_html=control_html,
        value_html=values_html,
        extra_body=_condition_line(conditions, effect) + _hidden_hooks(effect),
        attrs=attrs,
        card_class=card_cls,
        variant="bh",
    )


def _page_effects(conditions: dict) -> list[dict]:
    return [
        effect
        for effect in _l(conditions.get("effects"))
        if isinstance(effect, dict) and _s(effect.get("effect_id"))
    ]


def _configurable_effects(conditions: dict) -> list[dict]:
    return [effect for effect in _page_effects(conditions) if _is_configurable(effect)]


def _ordered_sections(conditions: dict) -> list[tuple[str, list[dict]]]:
    """Group effects by ``view.groups``; configurable sources come first."""

    effects = _page_effects(conditions)
    by_id = {_s(effect.get("effect_id")): effect for effect in effects}
    used: set[str] = set()
    sections: list[tuple[str, list[dict]]] = []
    for group in _l(conditions.get("groups")):
        if not isinstance(group, dict):
            continue
        ids = [
            _s(effect_id)
            for effect_id in _l(group.get("effect_ids"))
            if _s(effect_id) in by_id and _s(effect_id) not in used
        ]
        if not ids:
            continue
        used.update(ids)
        label = _strip_parentheses(_s(group.get("label"))) or "其他"
        sections.append((label, [by_id[effect_id] for effect_id in ids]))
    leftovers = [effect for effect in effects if _s(effect.get("effect_id")) not in used]
    if leftovers:
        sections.append(("", leftovers))
    # Keep the switchable Buffs first; read-only baseline/auto effects follow.
    sections.sort(key=lambda item: 0 if any(_is_configurable(effect) for effect in item[1]) else 1)
    return sections


def _group_heading(label: str, effects: list[dict]) -> str:
    """Group heading without repeating the card titles (player polish §9.6)."""

    titles = {_display_name(effect)[0] for effect in effects}
    parts = [part.strip() for part in label.split("·") if part.strip()]
    kept = [part for part in parts if part not in titles]
    return " · ".join(kept) if kept else label


def _grid_html(conditions: dict, effects: list[dict], group_heading: str = "") -> str:
    cards = "".join(_effect_card(conditions, effect, group_heading) for effect in effects)
    return (
        '<div class="cond-card-grid cond-buff-grid" data-cond-row-container>'
        f"{cards}</div>"
    )


def _cards_html(conditions: dict) -> str:
    sections = _ordered_sections(conditions)
    if not sections:
        return c.empty("当前构筑没有可展示的 Buff。")
    multi = [section for section in sections if len(section[1]) > 1]
    if not multi:
        return _grid_html(conditions, [effect for _label, sec in sections for effect in sec])
    blocks: list[str] = []
    pending: list[dict] = []

    def _flush() -> None:
        if pending:
            blocks.append(_grid_html(conditions, list(pending)))
            pending.clear()

    for label, section_effects in sections:
        if len(section_effects) > 1 and label:
            _flush()
            heading = _group_heading(label, section_effects)
            cards = "".join(
                _effect_card(conditions, effect, heading) for effect in section_effects
            )
            blocks.append(
                f'<section class="cond-group" data-cond-group="{c.esc(label)}" '
                'data-cond-row-container>'
                f'<h3 class="cond-group-title">{c.esc(heading)}</h3>'
                f'<div class="cond-card-grid cond-buff-grid">{cards}</div></section>'
            )
        else:
            pending.extend(section_effects)
    _flush()
    return "".join(blocks)


# --------------------------------------------------------------------------- #
# toolbars / action bar
# --------------------------------------------------------------------------- #


def _preset_options(conditions: dict) -> str:
    options: list[str] = []
    seen: set[str] = set()
    default_id = ""
    for preset in _l(conditions.get("presets")):
        if not isinstance(preset, dict):
            continue
        preset_id = _s(preset.get("id")) or _s(preset.get("preset_id"))
        if not preset_id or preset_id in seen:
            continue
        seen.add(preset_id)
        label_text = _s(preset.get("label")) or preset_id
        selected = " selected" if preset.get("is_default") and not default_id else ""
        if selected:
            default_id = preset_id
        options.append(f'<option value="{c.esc(preset_id)}"{selected}>{c.esc(label_text)}</option>')
    if not options:
        options.append('<option value="build_base" selected>构筑基础状态（默认）</option>')
    return "".join(options)


def _config_toolbar(conditions: dict) -> str:
    return shared_ui.action_bar(
        (
            '<label class="cond-field"><span class="cond-field-label">预设</span>'
            f'<select data-cond-preset aria-label="选择预设">{_preset_options(conditions)}</select>'
            "</label>"
            '<button type="button" class="btn btn-small btn-primary" data-cond-preset-apply>'
            "应用预设</button>"
            '<button type="button" class="btn btn-small" data-cond-export>保存配置</button>'
            '<label class="cond-file"><span class="btn btn-small">载入配置</span>'
            '<input type="file" accept=".json,application/json" data-cond-import '
            'aria-label="载入 Buff 配置"></label>'
            '<button type="button" class="btn btn-small" data-cond-reset>重置 Buff</button>'
        ),
        aria_label="预设与配置",
    )


def _category_items(conditions: dict) -> list[dict]:
    effects = _page_effects(conditions)
    available: dict[str, str] = {}
    for effect in effects:
        category = _s(effect.get("category"))
        if category and category not in available:
            available[category] = _category_label(effect)
    ordered: list[dict] = []
    seen: set[str] = set()
    for item in _l(_d(conditions.get("filters")).get("categories")):
        if not isinstance(item, dict):
            continue
        category = _s(item.get("id"))
        if category in available and category not in seen:
            ordered.append({"id": category, "label": available[category]})
            seen.add(category)
    for category, label_text in available.items():
        if category not in seen:
            ordered.append({"id": category, "label": label_text})
    return [{"id": "all", "label": "全部"}] + ordered


def _option_html(items: list[dict]) -> str:
    seen: set[str] = set()
    options: list[str] = []
    for item in items:
        option_id = _s(item.get("id"))
        if not option_id or option_id in seen:
            continue
        seen.add(option_id)
        options.append(
            f'<option value="{c.esc(option_id)}">{c.esc(_s(item.get("label")) or option_id)}</option>'
        )
    return "".join(options) or '<option value="all">全部</option>'


def _filter_toolbar(conditions: dict) -> str:
    total = len(_page_effects(conditions))
    return shared_ui.action_bar(
        (
            '<label class="cond-field"><span class="cond-field-label">搜索</span>'
            '<input type="search" data-cond-search autocomplete="off" '
            'placeholder="Buff、技能或属性…" aria-label="搜索 Buff"></label>'
            '<label class="cond-field"><span class="cond-field-label">来源</span>'
            f'<select data-cond-category aria-label="按来源筛选">{_option_html(_category_items(conditions))}</select>'
            "</label>"
            '<span class="small muted">已启用 '
            f'<b data-cond-count="configured">{_configured_count(conditions)}</b> 项</span>'
            '<span class="small muted cond-visible-count" data-cond-visible>'
            f"共 {total} 项</span>"
        ),
        aria_label="搜索与筛选",
    )


def _compute_bar(conditions: dict) -> str:
    applied_note = (
        '<span class="small muted">当前结果已按所选 Buff 计算</span>'
        if conditions.get("scenario_applied") is True
        else ""
    )
    buttons = (
        '<button type="button" class="btn btn-primary" data-cond-live-recompute>计算 DPS</button>'
        f"{applied_note}"
        + (
            ""
            if conditions.get("scenario_applied") is True
            else '<span class="small muted">勾选 Buff 后点击“计算 DPS”更新结果</span>'
        )
        + '<span class="small muted" data-cond-changed role="status" hidden>'
        "设置已更改，点击“计算 DPS”更新结果。</span>"
    )
    return (
        shared_ui.action_bar(buttons, aria_label="计算 DPS", extra_class="cond-compute-bar")
        + '<p class="cond-live-status small print-hide" data-cond-live-status '
        'data-status-state="applied" role="status">已应用（生成时场景）。</p>'
        '<p class="small muted print-hide" data-cond-import-status role="status" hidden></p>'
        '<p class="small cond-scenario-error print-hide" data-cond-import-error role="alert" hidden></p>'
    )


# --------------------------------------------------------------------------- #
# footer: 伤害乘区 short explanation (§10)
# --------------------------------------------------------------------------- #


def _scaling_block() -> str:
    rows = [[c.esc(part), c.esc(description)] for part, description in _SCALING_ROWS]
    body = (
        f'<p class="small"><code class="mono cond-formula">{c.esc(_SCALING_FORMULA)}</code></p>'
        + c.table(["部分", "说明"], rows, cls="cond-scaling-table")
        + '<p class="small muted">数量、层数、持续时间与施放间隔影响“结算多少次/多久”，'
        "不再乘一次伤害。</p>"
    )
    return c.detail("伤害乘区说明", body, cls="cond-scaling")


# --------------------------------------------------------------------------- #
# unavailable / legacy payloads
# --------------------------------------------------------------------------- #


def _unavailable_page(conditions: dict) -> str:
    reason = _s(conditions.get("unavailable_reason")) or "缺少条件 Buff 配置数据。"
    body = (
        c.empty(f"本报告未包含条件 Buff 配置数据：{reason}")
        + '<div class="card-actions">'
        '<button type="button" class="btn" data-goto-page="overview">返回总览</button>'
        '<button type="button" class="btn" data-goto-page="validation">查看增伤来源</button>'
        "</div>"
    )
    return c.page("conditions", _TITLE, "", body)


def _missing_page() -> str:
    body = (
        c.empty("本报告未包含条件 Buff 配置数据；请使用最新报告。")
        + '<div class="card-actions">'
        '<button type="button" class="btn" data-goto-page="overview">返回总览</button>'
        '<button type="button" class="btn" data-goto-page="validation">查看增伤来源</button>'
        "</div>"
    )
    return c.page("conditions", _TITLE, "", body)


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #


def render_conditions(view: dict) -> str:
    """Render the complete player-facing ``Buff 设置`` page as one ``<section>``."""

    if not isinstance(view, dict):
        view = {}
    conditions = view.get("conditions")
    if not isinstance(conditions, dict) or not conditions:
        return _missing_page()
    if conditions.get("available") is False:
        return _unavailable_page(conditions)
    body = (
        _config_toolbar(conditions)
        + _filter_toolbar(conditions)
        + _compute_bar(conditions)
        + _cards_html(conditions)
        + '<p class="empty-state cond-empty" data-cond-empty hidden>'
        "没有匹配的 Buff；换个关键词或来源再试。</p>"
        + _scaling_block()
    )
    return c.page("conditions", _TITLE, "", body)
