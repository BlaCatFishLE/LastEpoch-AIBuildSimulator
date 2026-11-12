"""伤害构成页：每条分支的乘区链、当前乘区与来源。

对应 ``view["composition"]``（见 ``作者本地历史记录（不随公开仓库分发）``
第 3.1 节与第 4 节）。页面把火焰本体、冰霜本体、点燃单层拆成三条独立的公式链：

    基础伤害与点伤 → 加算增伤 inc → 独立增伤 more → 抗性与穿透 → 怪物减伤 → 每次结算

每一步都是 ``formula-step`` 按钮，对应一个 ``step-panel``；面板内容全部来自
``present.build_view`` 已经算好的字段，本模块只做展示组合，不重新实现任何伤害规则。
玩家版只保留一条主公式、当前乘区数值与真实来源，工程说明与重复公式不再渲染。
"""

from __future__ import annotations

import re

from .. import components as c
from .. import units

__all__ = ["render_composition"]

_TAB_ORDER = ("fire", "cold", "ignite")
_TAB_LABELS = {"fire": "火焰本体", "cold": "冰霜本体", "ignite": "点燃单层"}

#: applies[].scope -> 玩家可读的分支名（与 present._BRANCH_LABELS 一致）
_BRANCH_LABELS = {
    "body-fire": "火焰本体",
    "body-cold": "冰霜本体",
    "body": "本体",
    "ignite": "点燃",
}

_DAMAGE_TYPE_LABELS = {
    "Fire": "火焰",
    "Cold": "冰霜",
    "Lightning": "闪电",
    "Physical": "物理",
    "Void": "虚空",
    "Poison": "毒素",
    "Necrotic": "腐化",
}

#: short penetration / resistance labels used by the pen tables
_PEN_LABELS = {
    "火焰": "火穿",
    "冰霜": "冰穿",
    "闪电": "闪电穿",
    "物理": "物理穿",
    "虚空": "虚空穿",
    "毒素": "毒素穿",
    "腐化": "腐化穿",
}
_RES_LABELS = {
    "火焰": "目标火抗",
    "冰霜": "目标冰抗",
    "闪电": "目标闪电抗",
    "物理": "目标物抗",
    "虚空": "目标虚空抗",
    "毒素": "目标毒抗",
    "腐化": "目标腐化抗",
}

#: skill-node ledger rows expose an exact dot path; more-groups carry node:N.
#: The explicit path mapping keeps the page from guessing by string suffix.
_SKILL_NODE_PATH = re.compile(r"^data\.specializations\.bh2\.nodes\.(\d+)$")
_MORE_NODE_GROUP = re.compile(r"^node:bh2:(\d+)$")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _source(view: dict, source_id) -> dict:
    return (view.get("sources") or {}).get(source_id) or {}


def _chip(view: dict, source_id, *, text: str | None = None, role: str = "") -> str:
    """Clickable source chip; never prints a raw internal id as fallback text."""

    source = _source(view, source_id)
    if not source:
        return '<span class="muted">来源未登记</span>'
    return c.source_chip_by_id(view, source_id, text=text or source.get("display_name"), role=role)


def _branches_text(source: dict) -> str:
    names: list[str] = []
    for item in source.get("applies") or []:
        if not isinstance(item, dict):
            continue
        scope = str(item.get("scope") or "")
        label = _BRANCH_LABELS.get(scope) or str(item.get("label") or scope)
        if label and label not in names:
            names.append(label)
    return "、".join(names)


def _damage_label(damage_type) -> str:
    text = str(damage_type or "")
    return _DAMAGE_TYPE_LABELS.get(text, text or "伤害")


def _pen_label(damage_label: str) -> str:
    return _PEN_LABELS.get(damage_label, damage_label + "穿")


def _res_label(damage_label: str) -> str:
    return _RES_LABELS.get(damage_label, "目标抗性")


# --------------------------------------------------------------------------- #
# dynamic target scenario bindings (units.num ``sc``)
# --------------------------------------------------------------------------- #

#: Values tagged below are exactly the ones that already contain the target-side
#: multipliers (penetration / monster DR), so the client may re-scale them when
#: the reader applies another monster level or target fire resistance.
_SC_DR = "stat:dr"
_SC_DR_FACTOR = "stat:drFactor"
_SC_LEVEL = "stat:level"


def _branch_bindings(branch_key: str) -> dict[str, str | None]:
    """Scenario group + target-stat names for one damage branch."""

    if branch_key == "cold":
        return {
            "group": "group:cold",
            # the scenario input covers fire resistance only; cold resistance
            # stays at the report baseline, so it has no ``targetRes`` binding.
            "target_res": None,
            "effective": "stat:effCold",
            "res_mult": "stat:multCold",
        }
    if branch_key == "ignite":
        return {
            "group": "group:ignite",
            "target_res": "stat:targetRes",
            "effective": "stat:effIgnite",
            "res_mult": "stat:multIgnite",
        }
    return {
        "group": "group:fire",
        "target_res": "stat:targetRes",
        "effective": "stat:effFire",
        "res_mult": "stat:multFire",
    }


def _skill_node_index(view: dict) -> dict[str, dict]:
    """node id -> skill-node source carrying the allocated points.

    ``present._more_table`` receives engine-local ``ruleset:node:<n>`` ids, so a
    more entry does not carry ``points`` for skill nodes.  The source catalogue
    does: each skill ledger row exposes its exact ``source_path``.  The mapping
    below is explicit path equality (never truncation / trailing digits); it can
    move into ``present.py`` once the display layer exposes per-node points.
    """

    index: dict[str, tuple[int, dict]] = {}
    for source in (view.get("sources") or {}).values():
        if source.get("category") != "skill_node":
            continue
        advanced = source.get("advanced") or {}
        match = _SKILL_NODE_PATH.match(str(advanced.get("source_path") or ""))
        if not match:
            continue
        node = match.group(1)
        priority = 0 if str(advanced.get("stat_key") or "") == "damage" else 1
        current = index.get(node)
        if current is None or priority < current[0]:
            index[node] = (priority, source)
    return {node: payload for node, (_priority, payload) in index.items()}


def _more_source(entry: dict, node_index: dict[str, dict]) -> dict:
    match = _MORE_NODE_GROUP.match(str(entry.get("group") or ""))
    if not match:
        return {}
    return node_index.get(match.group(1)) or {}


def _pen_scope(source: dict) -> str:
    """Where a penetration row applies, derived from its real consumption sites."""

    applies = [item for item in (source.get("applies") or []) if isinstance(item, dict)]
    scopes = {str(item.get("scope") or "") for item in applies}
    if not scopes:
        return "适用分支待确认"
    if any("仅点燃" in str(item.get("role") or "") for item in applies):
        return "仅点燃"
    if "ignite" in scopes and ({"body-fire", "body-cold"} & scopes):
        return "本体与点燃共用"
    if "ignite" in scopes:
        return "仅点燃"
    if "body-fire" in scopes:
        return "火焰本体"
    if "body-cold" in scopes:
        return "冰霜本体"
    return "、".join(_BRANCH_LABELS.get(scope, scope) for scope in sorted(scopes))


def _panel(tab_key: str, step_key: str, body: str, *, first: bool = False) -> str:
    panel_id = f"cmp-{tab_key}-{step_key}"
    opened = " is-open" if first else ""
    return f'<div class="step-panel{opened}" data-step-panel="{panel_id}" id="{panel_id}">{body}</div>'


def _chain(tab_label: str, tab_key: str, steps: list[tuple[str, str, str]], result_label: str, result_value: str) -> str:
    parts = []
    for index, (step_key, label, value_html) in enumerate(steps):
        panel_id = f"cmp-{tab_key}-{step_key}"
        active = " is-active" if index == 0 else ""
        parts.append(
            f'<button type="button" class="formula-step{active}" data-step-target="{panel_id}" '
            f'aria-controls="{panel_id}">'
            f'<span class="formula-step-label">{c.esc(label)}</span>'
            f'<span class="formula-step-value">{value_html}</span></button>'
        )
    parts.append(
        '<div class="formula-result"><span class="formula-step-label">'
        + c.esc(result_label)
        + '</span><span class="formula-step-value">'
        + result_value
        + "</span></div>"
    )
    return (
        f'<div class="formula-chain" role="group" aria-label="{c.esc(tab_label + "公式链")}">'
        + "".join(parts)
        + "</div>"
    )


# --------------------------------------------------------------------------- #
# 基础伤害与点伤
# --------------------------------------------------------------------------- #


def _base_body_panel(view: dict, component: dict, damage_label: str) -> str:
    steps = [step for step in (component.get("steps") or []) if isinstance(step, dict)]
    step = steps[0] if steps else {}
    parts = [part for part in (step.get("parts") or []) if isinstance(part, dict)]
    base = parts[0].get("value") if len(parts) > 0 else None
    added = parts[1].get("value") if len(parts) > 1 else None
    scale = parts[2].get("value") if len(parts) > 2 else None
    detail = step.get("detail") or {}

    facts = c.fact_list(
        [
            ("技能基础值", c.num(base, "damage")),
            ("装备/被动附加点伤", c.num(added, "number")),
            ("点伤效用", c.num(scale, "factor")),
            ("目标伤害类型", c.esc(damage_label + "伤害")),
        ]
    )
    formula = (
        '<p class="step-kicker">基础值 + 附加点伤 × 点伤效用</p>'
        '<p class="formula-block">'
        + c.num(base, "damage")
        + " + "
        + c.num(added, "number")
        + " × "
        + c.num(scale, "number")
        + " = "
        + c.num(component.get("per_tick_base"), "damage")
        + "</p>"
    )
    blocks = [facts, formula]

    sources = [item for item in (detail.get("sources") or []) if isinstance(item, dict)]
    source_items = []
    for item in sources:
        source_items.append(
            '<span class="part-item">'
            + _chip(view, item.get("source_id"))
            + " <b>+"
            + c.num(item.get("value"), "number")
            + " 点</b></span>"
        )
    if source_items:
        blocks.append(
            '<p class="step-kicker">附加点伤来源 · 合计 '
            + c.num(added, "number")
            + ' 点</p><div class="part-list">'
            + "".join(source_items)
            + "</div>"
        )
    return "".join(blocks)


def _base_ignite_panel(ignite: dict) -> str:
    base = ignite.get("base_damage")
    duration = ignite.get("base_duration_s")
    dps = ignite.get("base_dps")
    facts = c.fact_list(
        [
            ("基础总伤害", c.num(base, "damage")),
            ("基础持续", c.num(duration, "seconds", unit=True)),
        ]
    )
    formula = (
        '<p class="step-kicker">基础总伤害 ÷ 基础持续 = 基础 DPS</p>'
        '<p class="formula-block">'
        + c.num(base, "damage")
        + " ÷ "
        + c.num(duration, "seconds", unit=True)
        + " = "
        + c.num(dps, "damage")
        + " / 秒</p>"
    )
    return facts + formula


# --------------------------------------------------------------------------- #
# 加算增伤 inc
# --------------------------------------------------------------------------- #


def _inc_block(view: dict, inc: dict) -> str:
    total = inc.get("total")
    factor = inc.get("factor")
    count = inc.get("count")
    attribute = inc.get("attribute_entry") if isinstance(inc.get("attribute_entry"), dict) else None
    attribute_id = attribute.get("source_id") if attribute else None

    headline = (
        '<header class="card-head"><h4>加算增伤 '
        + c.signed_pct(total)
        + " → "
        + c.num(factor, "factor")
        + ' <span class="muted small">（'
        + c.num(count, "integer")
        + " 条来源）</span></h4></header>"
    )

    rows: list[list[str]] = []
    row_classes: list[str] = []
    for group in inc.get("groups") or []:
        group_rows = [
            entry
            for entry in (group.get("rows") or [])
            if isinstance(entry, dict) and entry.get("source_id") != attribute_id
        ]
        if not group_rows:
            continue
        rows.append(
            [
                "<b>" + c.esc(group.get("category_label")) + '</b> <span class="count-pill">' + str(len(group_rows)) + " 条</span>",
                "",
                c.num(group.get("subtotal"), "fraction"),
                "",
            ]
        )
        row_classes.append("is-subtotal")
        for entry in group_rows:
            source = _source(view, entry.get("source_id"))
            effect = str(source.get("stat_label") or "、".join(entry.get("tags") or []) or "来源增伤")
            branches = _branches_text(source)
            rows.append(
                [
                    _chip(view, entry.get("source_id")),
                    c.esc(effect),
                    c.signed_pct(entry.get("value")),
                    c.esc(branches) if branches else units.missing(),
                ]
            )
            row_classes.append("")

    if attribute:
        source = _source(view, attribute_id)
        branches = _branches_text(source)
        rows.append(
            [
                _chip(view, attribute_id, text=attribute.get("name")),
                '<span class="badge badge-info">属性</span> '
                + c.esc(source.get("effect_label") or "智力属性缩放"),
                c.signed_pct(attribute.get("value")),
                c.esc(branches) if branches else units.missing(),
            ]
        )
        row_classes.append("")

    table_html = c.table(
        ["来源", "效果", "本次计入", "适用分支"],
        rows,
        row_classes=row_classes,
        numeric_cols=(2,),
    )
    return headline + table_html


# --------------------------------------------------------------------------- #
# 独立增伤 more
# --------------------------------------------------------------------------- #


def _more_block(view: dict, more: dict, node_index: dict[str, dict]) -> str:
    entries = [entry for entry in (more.get("entries") or []) if isinstance(entry, dict)]
    headline = (
        '<header class="card-head"><h4>独立增伤 '
        + c.num(more.get("product"), "factor")
        + ' <span class="muted small">（'
        + c.num(more.get("count"), "integer")
        + " 个因子相乘）</span></h4></header>"
    )
    if not entries:
        return headline + c.empty("当前结果没有独立增伤因子。")

    rows: list[list[str]] = []
    for entry in entries:
        name = str(entry.get("name") or "")
        name_html = c.esc(name) if name else units.missing()
        points = entry.get("points")
        source = _more_source(entry, node_index)
        if source:
            name_html = _chip(view, source.get("id"), text=name or source.get("name"))
            if points is None:
                points = source.get("points")
        points_html = c.num(points, "points", unit=True) if points is not None else units.unknown()

        per_point_text = entry.get("per_point_text")
        if per_point_text:
            per_point_html = c.esc(per_point_text)
        elif points and entry.get("value") is not None:
            per_point_html = (
                '<span class="muted" title="由本次因子与已分配点数折算">≈</span> '
                + c.signed_pct(entry.get("value") / points)
            )
        else:
            per_point_html = units.unknown()

        rows.append(
            [
                name_html,
                points_html,
                per_point_html,
                c.num(entry.get("factor"), "factor"),
                c.esc(entry.get("condition") or "已分配点数下生效"),
            ]
        )

    table_html = c.table(
        ["节点/被动", "已分配点数", "每点效果", "本次因子", "条件"],
        rows,
        numeric_cols=(1, 3),
    )
    return headline + table_html


# --------------------------------------------------------------------------- #
# 抗性与穿透 / 怪物减伤
# --------------------------------------------------------------------------- #


def _pen_block(
    view: dict,
    pen: dict,
    *,
    branch_key: str,
    branch_label: str,
    damage_label: str,
    cross_pen: dict | None = None,
) -> str:
    entries = [entry for entry in (pen.get("entries") or []) if isinstance(entry, dict)]
    pen_label = _pen_label(damage_label)
    bindings = _branch_bindings(branch_key)
    rows: list[list[str]] = []
    for entry in entries:
        source = _source(view, entry.get("source_id"))
        rows.append(
            [
                _chip(view, entry.get("source_id")),
                c.signed_pct(entry.get("value")),
                c.esc(_pen_scope(source)),
            ]
        )

    total = pen.get("total")
    blocks: list[str] = []
    if rows:
        blocks.append(
            c.table(
                ["来源", pen_label, "生效范围"],
                rows,
                numeric_cols=(1,),
            )
        )
    else:
        blocks.append(c.note(branch_label + "当前没有" + pen_label + "来源。", cls="small"))

    settle_lines = []
    if pen.get("target_resistance") is not None:
        settle_lines.append(
            _res_label(damage_label)
            + " "
            + c.num(pen.get("target_resistance"), "fraction", sc=bindings["target_res"])
            + " − 穿透 "
            + c.num(total, "fraction")
            + " = 有效抗性 "
            + c.num(pen.get("effective_resistance"), "fraction", sc=bindings["effective"])
            + " → 伤害系数 "
            + c.num(pen.get("resistance_multiplier"), "factor", sc=bindings["res_mult"])
        )
    elif total:
        settle_lines.append(
            "穿透 "
            + c.num(total, "fraction")
            + " → 伤害系数 "
            + c.num(pen.get("resistance_multiplier"), "factor", sc=bindings["res_mult"])
        )
    if pen.get("monster_dr") is not None or pen.get("dr_factor") is not None:
        settle_lines.append(
            "怪物 DR "
            + c.num(pen.get("monster_dr"), "fraction", sc=_SC_DR)
            + " → 伤害系数 "
            + c.num(pen.get("dr_factor"), "factor", sc=_SC_DR_FACTOR)
        )
    if settle_lines:
        blocks.append('<p class="formula-block">' + "<br>".join(settle_lines) + "</p>")

    if branch_key == "fire" and cross_pen:
        own_ids = {entry.get("source_id") for entry in entries}
        extra = [
            entry
            for entry in (cross_pen.get("entries") or [])
            if isinstance(entry, dict) and entry.get("source_id") not in own_ids
        ]
        if extra:
            extra_total = sum(entry.get("value") or 0.0 for entry in extra)
            blocks.append(
                c.note(
                    "点燃另有仅点燃"
                    + pen_label
                    + " "
                    + c.signed_pct(extra_total)
                    + "（点燃合计 "
                    + c.num(cross_pen.get("total"), "fraction")
                    + "），不作用于"
                    + branch_label
                    + "。",
                    cls="small",
                )
            )
    if branch_key == "ignite" and cross_pen:
        blocks.append(
            c.note(
                "本体火焰只使用通用"
                + pen_label
                + " "
                + c.num(cross_pen.get("total"), "fraction")
                + "。",
                cls="small",
            )
        )
    return "".join(blocks)


def _dr_panel(view: dict, pen: dict) -> str:
    scenario = (view.get("context") or {}).get("scenario") or {}
    return c.fact_list(
        [
            ("目标等级", c.num(scenario.get("level"), "integer", sc=_SC_LEVEL)),
            ("怪物减伤 DR", c.num(pen.get("monster_dr"), "fraction", sc=_SC_DR)),
            ("伤害系数", c.num(pen.get("dr_factor"), "factor", sc=_SC_DR_FACTOR)),
        ]
    )


# --------------------------------------------------------------------------- #
# 结果行
# --------------------------------------------------------------------------- #


def _formula_expression(
    *,
    base_value,
    inc_factor,
    more_product,
    res_multiplier,
    res_mult_sc: str,
    dr_factor,
    result_value,
    result_sc: str,
    result_suffix: str,
) -> str:
    """One formula line rebuilt from ``.num`` spans.

    Only the target-side factors (penetration multiplier / monster DR factor)
    and the damage result are scenario-bound; base damage, ``inc`` and ``more``
    are offensive constants and stay untagged.  The visible formula block uses
    this helper, so no plain-text copy of a target-side number can go stale after
    the reader applies a scenario.
    """

    return (
        " ".join(
            [
                c.num(base_value, "damage"),
                c.num(inc_factor, "factor"),
                c.num(more_product, "factor"),
                c.num(res_multiplier, "factor", sc=res_mult_sc),
                c.num(dr_factor, "factor", sc=_SC_DR_FACTOR),
            ]
        )
        + " = "
        + c.num(result_value, "damage", sc=result_sc)
        + " "
        + result_suffix
    )


def _body_summary(component: dict, *, branch_key: str) -> str:
    bindings = _branch_bindings(branch_key)
    group_sc = bindings["group"]
    inc = component.get("inc") or {}
    more = component.get("more") or {}
    pen = component.get("pen") or {}
    formula = _formula_expression(
        base_value=component.get("per_tick_base"),
        inc_factor=inc.get("factor"),
        more_product=more.get("product"),
        res_multiplier=pen.get("resistance_multiplier"),
        res_mult_sc=bindings["res_mult"],
        dr_factor=pen.get("dr_factor"),
        result_value=component.get("per_tick"),
        result_sc=group_sc,
        result_suffix="/ 每次结算",
    )
    total_line = (
        c.num(component.get("per_tick"), "damage", sc=group_sc)
        + " × "
        + c.num(component.get("tick_count"), "count", unit=True)
        + " = "
        + c.num(component.get("total"), "damage", sc=group_sc)
    )
    return '<p class="formula-block">' + formula + "<br>" + total_line + "</p>"


def _ignite_summary(ignite: dict) -> str:
    formula = _formula_expression(
        base_value=ignite.get("base_dps"),
        inc_factor=ignite.get("inc_factor"),
        more_product=ignite.get("more_product"),
        res_multiplier=ignite.get("resistance_multiplier"),
        res_mult_sc="stat:multIgnite",
        dr_factor=ignite.get("dr_factor"),
        result_value=ignite.get("per_stack_dps"),
        result_sc="group:ignite",
        result_suffix="/ 秒 / 层",
    )
    return '<p class="formula-block">' + formula + "</p>"


# --------------------------------------------------------------------------- #
# 三条分支
# --------------------------------------------------------------------------- #


def _body_branch(view: dict, component: dict, node_index: dict[str, dict], *, cross_pen: dict | None = None) -> str:
    if not component:
        return c.empty("当前结果没有该本体分量。")
    key = str(component.get("key") or "fire")
    title = str(component.get("label") or _TAB_LABELS.get(key, "本体"))
    damage_label = _damage_label(component.get("damage_type"))
    inc = component.get("inc") or {}
    more = component.get("more") or {}
    pen = component.get("pen") or {}
    bindings = _branch_bindings(key)

    steps = [
        ("base", "基础伤害与点伤", c.num(component.get("per_tick_base"), "damage")),
        ("inc", "加算增伤 inc", c.num(inc.get("factor"), "factor")),
        ("more", "独立增伤 more", c.num(more.get("product"), "factor")),
        ("pen", "抗性与穿透", c.num(pen.get("resistance_multiplier"), "factor", sc=bindings["res_mult"])),
        ("dr", "怪物减伤", c.num(pen.get("dr_factor"), "factor", sc=_SC_DR_FACTOR)),
    ]
    panels = "".join(
        [
            _panel(key, "base", _base_body_panel(view, component, damage_label), first=True),
            _panel(key, "inc", _inc_block(view, inc)),
            _panel(key, "more", _more_block(view, more, node_index)),
            _panel(
                key,
                "pen",
                _pen_block(
                    view,
                    pen,
                    branch_key=key,
                    branch_label=title,
                    damage_label=damage_label,
                    cross_pen=cross_pen,
                ),
            ),
            _panel(key, "dr", _dr_panel(view, pen)),
        ]
    )
    return (
        f'<section class="block" aria-label="{c.esc(title + "乘区")}">'
        + _chain(
            title,
            key,
            steps,
            "每次结算",
            c.num(component.get("per_tick"), "damage", sc=bindings["group"]),
        )
        + _body_summary(component, branch_key=key)
        + panels
        + "</section>"
    )


def _ignite_branch(view: dict, ignite: dict, node_index: dict[str, dict], *, cross_pen: dict | None = None) -> str:
    if not ignite:
        return c.empty("当前结果没有点燃单层数据。")
    damage_label = _damage_label(ignite.get("damage_type") or "Fire")
    pen = ignite.get("pen") or {}
    steps = [
        ("base", "基础 DPS", c.num(ignite.get("base_dps"), "damage")),
        ("inc", "加算增伤 inc", c.num(ignite.get("inc_factor"), "factor")),
        ("more", "独立增伤 more", c.num(ignite.get("more_product"), "factor")),
        ("pen", "抗性与穿透", c.num(ignite.get("resistance_multiplier"), "factor", sc="stat:multIgnite")),
        ("dr", "怪物减伤", c.num(ignite.get("dr_factor"), "factor", sc=_SC_DR_FACTOR)),
    ]
    panels = "".join(
        [
            _panel("ignite", "base", _base_ignite_panel(ignite), first=True),
            _panel("ignite", "inc", _inc_block(view, ignite.get("inc") or {})),
            _panel("ignite", "more", _more_block(view, ignite.get("more") or {}, node_index)),
            _panel(
                "ignite",
                "pen",
                _pen_block(
                    view,
                    pen,
                    branch_key="ignite",
                    branch_label="点燃",
                    damage_label=damage_label,
                    cross_pen=cross_pen,
                ),
            ),
            _panel("ignite", "dr", _dr_panel(view, pen)),
        ]
    )
    return (
        f'<section class="block" aria-label="{c.esc("点燃单层乘区")}">'
        + _chain(
            "点燃单层",
            "ignite",
            steps,
            "单层每秒",
            c.num(ignite.get("per_stack_dps"), "damage", sc="group:ignite"),
        )
        + _ignite_summary(ignite)
        + panels
        + '<div class="card-actions">'
        + '<button type="button" class="btn btn-primary" data-goto-page="ignite">查看点燃四步 →</button>'
        + "</div></section>"
    )


# --------------------------------------------------------------------------- #
# page
# --------------------------------------------------------------------------- #


def render_composition(view: dict) -> str:
    composition = view.get("composition") or {}
    components = [item for item in (composition.get("components") or []) if isinstance(item, dict)]
    by_key = {str(item.get("key")): item for item in components}
    ignite = composition.get("ignite") or {}
    if not components and not ignite:
        return c.page(
            "composition",
            "伤害构成",
            "",
            c.empty("当前结果没有可展示的伤害构成。"),
        )

    node_index = _skill_node_index(view)
    fire_component = by_key.get("fire") or {}
    ignite_pen = ignite.get("pen") or {}
    fire_pen = (fire_component.get("pen") or {}) if fire_component else {}

    branch_html = {
        "fire": _body_branch(view, fire_component, node_index, cross_pen=ignite_pen),
        "cold": _body_branch(view, by_key.get("cold") or {}, node_index),
        "ignite": _ignite_branch(view, ignite, node_index, cross_pen=fire_pen),
    }

    tabs = []
    panels = []
    for index, key in enumerate(_TAB_ORDER):
        active = index == 0
        panel_id = f"cmp-{key}"
        tab_id = f"cmp-tab-{key}"
        tabs.append(
            f'<button type="button" class="tab{" is-active" if active else ""}" id="{tab_id}" '
            f'role="tab" aria-selected="{"true" if active else "false"}" '
            f'aria-controls="{panel_id}" data-tab-target="{panel_id}">'
            f"{c.esc(_TAB_LABELS[key])}</button>"
        )
        panels.append(
            f'<div class="tab-panel{" is-active" if active else ""}" id="{panel_id}" '
            f'data-tab-panel="{panel_id}" role="tabpanel" aria-labelledby="{tab_id}">'
            f"{branch_html[key]}</div>"
        )

    body = (
        '<div class="tabs" role="tablist" aria-label="伤害构成分支">'
        + "".join(tabs)
        + "</div>"
        + "".join(panels)
    )
    return c.page("composition", "伤害构成", "", body)
