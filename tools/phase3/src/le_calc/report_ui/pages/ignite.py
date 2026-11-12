"""点燃页：施加层数 → 单层伤害 → 持续时间 → 点燃总伤害。

页面按四个环节组织（契约 3.2 / 改版方案 §6）：

1. 施加层数：未封顶火抗与 Heat Wave 如何换算成每秒新增与累计施加层数；
2. 单层伤害：基础 DPS × inc × more × 抗性/穿透 × 怪物 DR；
3. 持续时间：基础持续 × 持续时间乘区，得到单层完整伤害；
4. 点燃总伤害：逐批结算结果，尾部作为总量的组成展示。

所有数值来自 ``present.build_view`` 的 ``view["ignite"]``，页面只做展示组合，
不重新实现伤害规则；持续时间只在第三步计入一次，尾部包含在点燃总伤害内。
玩家版每个环节先给结果，再把实际乘区与装备／节点来源放进可展开详情。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .. import components as c
from .. import units

__all__ = ["render_ignite"]

_PAGE_ID = "ignite"
_PAGE_TITLE = "点燃"

#: 神像折射行的词缀原文里取「原词缀 21%」这样的原始百分比。
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?\s*%)")


# --------------------------------------------------------------------------- #
# small coercion helpers (view data is plain JSON)
# --------------------------------------------------------------------------- #


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> list:
    return value if isinstance(value, list) else []


def _s(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def _number(value: Any) -> float | None:
    """Return a finite float or ``None`` (display helpers handle the rest)."""

    if not units.is_number(value):
        return None
    return units.clean_number(value)


def _share(part: Any, whole: Any) -> float | None:
    part_value = _number(part)
    whole_value = _number(whole)
    if part_value is None or not whole_value:
        return None
    return part_value / whole_value


def _damage_exact(value: Any, sc: str | None = None) -> str:
    """Damage with the full grouped value visible (formula lines stay exact).

    ``sc`` forwards the dynamic-scenario binding; formula result damage on this
    page always carries target-side multipliers, so callers pass
    ``sc="group:ignite"``.
    """

    number = _number(value)
    if number is None:
        return units.unknown()
    return c.num(
        number,
        "damage",
        compact_text=units.fmt_full(number, "damage"),
        sc=sc,
    )


def _seconds3(value: Any) -> str:
    """Duration with three decimals (点燃持续时间的关键结果按 7.648 秒展示)."""

    number = _number(value)
    if number is None:
        return units.unknown()
    text = units.fmt_full(number, "seconds", digits=3)
    return c.num(number, "seconds", unit=True, compact_text=f"{text}")


def _source(view: dict, source_id: Any) -> dict:
    if not source_id:
        return {}
    return _d(_d(view.get("sources")).get(str(source_id)))


def _source_cell(view: dict, source_id: Any, name: Any = None) -> str:
    """Source name as a clickable chip (falls back to plain escaped text)."""

    source = _source(view, source_id)
    if source:
        return c.source_chip_by_id(view, str(source_id), text=_s(name) or None)
    label = _s(name) or _s(source_id) or "来源"
    return f'<span class="muted small">{c.esc(label)}</span>'


def _source_effect(source: dict, tags: Iterable[Any] = ()) -> str:
    label = _s(source.get("stat_label")) or _s(source.get("effect_label"))
    if label:
        return c.esc(label)
    tag_text = " / ".join(str(tag) for tag in tags if tag)
    return c.esc(tag_text) if tag_text else "—"


def _stat(label: str, value_html: str) -> str:
    return (
        f'<div class="stat-row"><span class="stat-label">{c.esc(label)}</span>'
        f'<span class="stat-value">{value_html}</span></div>'
    )


def _stat_list(rows: Iterable[tuple[str, str]]) -> str:
    return '<div class="stat-list">' + "".join(_stat(label, value) for label, value in rows) + "</div>"


def _step(number: int, kicker: str, title: str, body: str) -> str:
    return (
        '<article class="step-card step-card-ignite">'
        f'<span class="step-index">{number}</span>'
        '<div class="step-body">'
        f'<p class="step-kicker">{c.esc(kicker)}</p>'
        f"<h3>{c.esc(title)}</h3>"
        f"{body}"
        "</div></article>"
    )


# --------------------------------------------------------------------------- #
# step 1 - application / stacks
# --------------------------------------------------------------------------- #


def _fire_resistance_pool_detail(view: dict, pool: dict) -> str:
    """Expandable fire-resistance pool: one row per real source plus subtotals."""

    groups = [group for group in _l(pool.get("groups")) if isinstance(group, dict)]
    rows: list[list[str]] = []
    row_classes: list[str] = []
    source_count = 0
    for group in groups:
        for row in _l(group.get("rows")):
            if not isinstance(row, dict):
                continue
            source_count += 1
            rows.append(
                [
                    _source_cell(view, row.get("source_id"), row.get("name")),
                    c.num(row.get("value"), "fraction"),
                ]
            )
            row_classes.append("")
        rows.append(
            [
                f'<b>{c.esc(group.get("label"))} 小计</b>',
                c.num(group.get("subtotal"), "pp"),
            ]
        )
        row_classes.append("is-subtotal")

    total = pool.get("total")
    if not rows:
        return c.detail("未封顶火抗的来源", c.empty("火抗来源未提供"))
    summary = f"未封顶火抗 {c.num(total, 'pp')} 的来源（{source_count} 条）"
    table = c.table(
        ["来源", "计入未封顶火抗"],
        rows,
        numeric_cols=(1,),
        row_classes=row_classes,
    )
    cap_note = (
        '<p class="table-note">抗性上限 '
        + c.num(pool.get("cap"), "pp")
        + "；Heat Wave 判定使用未封顶火抗。</p>"
    )
    return c.detail(summary, table + cap_note)


def _step_application(view: dict, application: dict) -> str:
    points = application.get("points")
    uncapped = application.get("uncapped_fire_resistance")
    per_second = application.get("per_second")
    per_roll = application.get("per_roll")
    interval = application.get("roll_interval_s")
    rolls = application.get("roll_count")
    total_stacks = application.get("total_stacks")
    peak_stacks = application.get("peak_stacks")
    peak_time = application.get("peak_time_s")
    per_point = application.get("per_point_percent")
    pool = _d(application.get("pool"))

    heat_wave_id = application.get("heat_wave_source_id")
    heat_wave_name = _s(application.get("heat_wave_name"))
    heat_wave_chip = c.source_chip_by_id(
        view,
        heat_wave_id,
        text=heat_wave_name or None,
    )

    gauges = (
        '<div class="grid-3">'
        + c.kpi(
            "累计施加层数",
            c.num(total_stacks, "stacks"),
            unit="层",
            hint=f"{c.num(rolls, 'count', unit=True)} × {c.num(per_roll, 'stacks', unit=True)}",
            tone="ignite",
        )
        + c.kpi(
            "峰值活跃层数",
            c.num(peak_stacks, "stacks"),
            unit="层",
            hint=f"t≈{_seconds3(peak_time)}",
            tone="state",
        )
        + c.kpi(
            "每秒预期新增",
            c.num(per_second, "stacks_rate"),
            unit="层/秒",
            tone="plain",
        )
        + "</div>"
    )

    mechanics = c.note(
        heat_wave_chip
        + " "
        + c.num(points, "points", unit=True)
        + "：每点使点燃几率按未封顶火抗 "
        + c.num(uncapped, "pp")
        + " 增加 "
        + c.num(per_point, "fraction")
        + "；黑洞生成瞬间首判，之后每 "
        + c.num(interval, "seconds", unit=True)
        + " 判定一次，共 "
        + c.num(rolls, "count")
        + " 次。双星共用同一光环，层数不翻倍。",
        cls="small",
    )

    body = gauges + mechanics + _fire_resistance_pool_detail(view, pool)
    return _step(1, "第一步", "施加层数", body)


# --------------------------------------------------------------------------- #
# step 2 - per-stack DPS
# --------------------------------------------------------------------------- #


def _inc_detail(view: dict, inc: dict) -> str:
    entries_total = _l(inc.get("entries"))
    groups = [group for group in _l(inc.get("groups")) if isinstance(group, dict)]
    factor = inc.get("factor")
    total = inc.get("total")
    summary = (
        f"加算增伤 {c.signed_pct(total)} → {c.num(factor, 'factor')}"
        f"（{len(entries_total)} 条来源）"
    )
    rows: list[list[str]] = []
    row_classes: list[str] = []
    for group in groups:
        for entry in _l(group.get("rows")):
            if not isinstance(entry, dict):
                continue
            source = _source(view, entry.get("source_id"))
            rows.append(
                [
                    _source_cell(view, entry.get("source_id"), entry.get("name")),
                    _source_effect(source, _l(entry.get("tags"))),
                    c.num(entry.get("value"), "fraction"),
                ]
            )
            row_classes.append("")
        rows.append(
            [
                f'<b>{c.esc(group.get("category_label"))} 小计</b>',
                "",
                c.num(group.get("subtotal"), "fraction"),
            ]
        )
        row_classes.append("is-subtotal")
    if not rows:
        return c.detail("加算增伤 inc", c.empty("inc 来源未提供"))
    table = c.table(
        ["来源", "效果", "本次计入"],
        rows,
        numeric_cols=(2,),
        row_classes=row_classes,
    )
    return c.detail(summary, table)


def _more_detail(view: dict, more: dict) -> str:
    entries = [entry for entry in _l(more.get("entries")) if isinstance(entry, dict)]
    product = more.get("product")
    summary = f"独立增伤 {len(entries)} 个因子 → {c.num(product, 'factor')}"
    rows: list[list[str]] = []
    for entry in entries:
        source_ids = [_s(source_id) for source_id in _l(entry.get("source_ids")) if source_id]
        name = _s(entry.get("name")) or "独立增伤"
        if source_ids and _source(view, source_ids[0]):
            name_cell = c.source_chip_by_id(view, source_ids[0], text=name)
        else:
            name_cell = f"<span>{c.esc(name)}</span>"
        points_html = (
            c.num(entry.get("points"), "points", unit=True)
            if units.is_number(entry.get("points"))
            else units.missing()
        )
        rows.append(
            [
                name_cell,
                points_html,
                c.num(entry.get("factor"), "factor"),
                f'<span class="small">{c.esc(_s(entry.get("condition")))}</span>',
            ]
        )
    if not rows:
        return c.detail("独立增伤 more", c.empty("more 来源未提供"))
    table = c.table(
        ["节点/被动", "已分配点数", "本次因子", "条件"],
        rows,
        numeric_cols=(1, 2),
    )
    return c.detail(summary, table)


def _pen_detail(view: dict, per_stack: dict) -> str:
    pen = _d(per_stack.get("pen"))
    entries = [entry for entry in _l(pen.get("entries")) if isinstance(entry, dict)]
    total = pen.get("total")
    summary = f"火焰穿透 {c.num(total, 'fraction')}（{len(entries)} 条来源）"
    rows: list[list[str]] = []
    for entry in entries:
        source_id = entry.get("source_id")
        source = _source(view, source_id)
        advanced = _d(source.get("advanced"))
        scope = "仅点燃" if advanced.get("stat_key") == "penetration_with_ignite" else "通用火穿"
        rows.append(
            [
                _source_cell(view, source_id, entry.get("name")),
                c.num(entry.get("value"), "fraction"),
                c.esc(scope),
            ]
        )
    if not rows:
        return c.detail("火焰穿透", c.empty("穿透来源未提供"))
    table = c.table(
        ["来源", "火穿", "生效范围"],
        rows,
        numeric_cols=(1,),
    )
    formula = (
        '<p class="formula-block">'
        f"目标火抗 {c.num(pen.get('target_resistance'), 'fraction', sc='stat:targetRes')}"
        f" − 穿透 {c.num(total, 'fraction')}"
        f" = 有效抗性 {c.num(pen.get('effective_resistance'), 'fraction', sc='stat:effIgnite')}，"
        f"伤害系数 {c.num(pen.get('resistance_multiplier'), 'factor', sc='stat:multIgnite')}"
        f"<br>怪物 DR {c.num(pen.get('monster_dr'), 'fraction', sc='stat:dr')}"
        f" → 系数 {c.num(pen.get('dr_factor'), 'factor', sc='stat:drFactor')}"
        "</p>"
    )
    return c.detail(summary, table + formula)


def _step_per_stack(view: dict, per_stack: dict) -> str:
    base_dps = per_stack.get("base_dps")
    inc_factor = per_stack.get("inc_factor")
    more_product = per_stack.get("more_product")
    resistance_multiplier = per_stack.get("resistance_multiplier")
    dr_factor = per_stack.get("dr_factor")
    per_stack_dps = per_stack.get("per_stack_dps")

    stats = _stat_list(
        [
            (
                "单层每秒伤害",
                _damage_exact(per_stack_dps, sc="group:ignite")
                + ' <span class="muted">/ 秒 / 层</span>',
            ),
            ("基础 DPS", c.num(base_dps, "dps")),
        ]
    )
    formula = (
        '<p class="step-formula">'
        f"基础 DPS {c.num(base_dps, 'dps')} "
        f"{c.num(inc_factor, 'factor')} "
        f"{c.num(more_product, 'factor')} "
        f"{c.num(resistance_multiplier, 'factor', sc='stat:multIgnite')} "
        f"{c.num(dr_factor, 'factor', sc='stat:drFactor')} "
        f"= {_damage_exact(per_stack_dps, sc='group:ignite')} / 秒 / 层"
        "</p>"
    )
    body = (
        stats
        + formula
        + _inc_detail(view, _d(per_stack.get("inc")))
        + _more_detail(view, _d(per_stack.get("more")))
        + _pen_detail(view, per_stack)
    )
    return _step(2, "第二步", "单层伤害", body)


# --------------------------------------------------------------------------- #
# step 3 - duration
# --------------------------------------------------------------------------- #


def _duration_value_text(row: dict) -> str:
    """Render the affix → imported → included value path for one duration row."""

    imported = _s(row.get("imported_display"))
    raw = _s(row.get("raw_text"))
    if bool(row.get("refracted")):
        match = _PERCENT_RE.search(raw)
        if match:
            raw_text = match.group(1).replace(" ", "")
            if imported:
                return f"原词缀 {c.esc(raw_text)} → 导入后 {c.esc(imported)}"
            return f"原词缀 {c.esc(raw_text)}"
        if imported:
            return f"导入后 {c.esc(imported)}"
        return c.esc(raw) if raw else "按导入快照计入"
    if imported:
        return f"导入后 {c.esc(imported)}"
    return "按导入快照计入"


def _step_duration(view: dict, duration: dict, per_stack: dict) -> str:
    base_s = duration.get("base_s")
    increased_total = duration.get("increased_total")
    multiplier = duration.get("multiplier")
    duration_s = duration.get("duration_s")
    per_stack_total = duration.get("per_stack_total")
    per_stack_dps = per_stack.get("per_stack_dps")
    rows: list[list[str]] = []
    for row in _l(duration.get("rows")):
        if not isinstance(row, dict):
            continue
        rows.append(
            [
                _source_cell(view, row.get("source_id"), row.get("name")),
                _duration_value_text(row),
                c.num(row.get("value"), "fraction"),
            ]
        )
    stats = _stat_list(
        [
            ("持续时间", _seconds3(duration_s)),
            ("单层完整伤害", _damage_exact(per_stack_total, sc="group:ignite")),
        ]
    )
    formula = (
        '<p class="step-formula">'
        f"基础持续 {c.num(base_s, 'seconds', unit=True)}"
        f" ×（1 + 持续时间加成 {c.num(increased_total, 'fraction')}）"
        f" = {_seconds3(duration_s)}（系数 {c.num(multiplier, 'factor')}）"
        "<br>"
        f"单层完整伤害 = {_damage_exact(per_stack_dps, sc='group:ignite')} × {_seconds3(duration_s)}"
        f" = {_damage_exact(per_stack_total, sc='group:ignite')}"
        "</p>"
    )
    if rows:
        table = c.table(
            ["来源", "词缀取值过程", "本次计入"],
            rows,
            numeric_cols=(2,),
        )
        sources = c.detail(f"持续时间来源（{len(rows)} 条）", table)
    else:
        sources = c.detail("持续时间来源", c.empty("持续时间来源未提供"))
    body = stats + formula + sources
    return _step(3, "第三步", "持续时间", body)


# --------------------------------------------------------------------------- #
# step 4 - total damage
# --------------------------------------------------------------------------- #


def _cohorts_table(cohorts: list[dict]) -> str:
    rows: list[list[str]] = []
    for cohort in cohorts:
        time_s = _number(cohort.get("time_s"))
        goto = f"{time_s:.3f}" if time_s is not None else ""
        index = _s(cohort.get("index")) or "?"
        rows.append(
            [
                f'<button type="button" class="link-btn" data-goto-time="{c.esc(goto)}" '
                f'aria-label="跳到时间轴 {c.esc(goto)} 秒">第 {c.esc(index)} 批</button>',
                c.num(cohort.get("time_s"), "seconds", unit=True),
                c.num(cohort.get("stacks"), "stacks", unit=True),
                c.num(cohort.get("expiry_s"), "seconds", unit=True),
                c.num(cohort.get("before_bh_end"), "damage", sc="group:ignite"),
                c.num(cohort.get("tail"), "damage", sc="group:ignite"),
            ]
        )
    return c.table(
        ["批次", "施加时刻", "层数", "到期时刻", "本窗口伤害", "窗口外尾部"],
        rows,
        numeric_cols=(2, 3, 4, 5),
    )


def _step_totals(totals: dict, single: dict, application: dict) -> str:
    cohorts = [cohort for cohort in _l(totals.get("cohorts")) if isinstance(cohort, dict)]
    damage = totals.get("damage")
    per_stack_total = totals.get("per_stack_total")
    before = totals.get("before_bh_end")
    tail = totals.get("tail")
    roll_count = application.get("roll_count")
    bh_end = single.get("bh_end_s")
    observation_end = single.get("observation_end_s")

    first_cohort = cohorts[0] if cohorts else {}
    batch_stacks = first_cohort.get("stacks") if cohorts else application.get("per_roll")
    batch_damage = first_cohort.get("damage")
    batch_count = len(cohorts) if cohorts else roll_count

    result = _stat_list([("点燃总伤害", c.num(damage, "damage", sc="group:ignite"))])

    segments = [
        {
            "key": "before",
            "label": "黑洞存续期间",
            "value": before,
            "share": _share(before, damage),
            "color": "ignite",
            "group": "ignite",
        },
        {
            "key": "tail",
            "label": "尾部",
            "value": tail,
            "share": _share(tail, damage),
            "color": "body",
            "group": "ignite",
        },
    ]
    composition = (
        '<p class="step-kicker">组成</p>'
        + c.meter(segments)
        + c.note(
            f"尾部在黑洞消失后继续结算至 t≈{_seconds3(observation_end)}；"
            f"黑洞在 t≈{_seconds3(bh_end)} 结束。",
            cls="small",
        )
    )

    formula = (
        '<p class="step-formula">'
        f"每批 {c.num(batch_stacks, 'stacks', unit=True)} × "
        f"单层完整伤害 {_damage_exact(per_stack_total, sc='group:ignite')}"
        f" = {_damage_exact(batch_damage, sc='group:ignite')}"
        "<br>"
        f"{c.num(batch_count, 'count')} 批逐批相加 = {_damage_exact(damage, sc='group:ignite')}"
        "</p>"
    )

    if cohorts:
        cohort_detail = c.detail(
            f"查看每批点燃（{len(cohorts)} 批）",
            _cohorts_table(cohorts),
        )
    else:
        cohort_detail = c.detail("查看每批点燃", c.empty("逐批数据未提供"))

    action = (
        '<div class="card-actions">'
        '<button type="button" class="btn" data-goto-page="timeline">'
        "在时间轴查看层数曲线与逐批区间</button>"
        "</div>"
    )
    body = result + composition + formula + cohort_detail + action
    return _step(4, "第四步", "点燃总伤害", body)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def render_ignite(view: dict) -> str:
    """Render the ignite page section for one display view."""

    view = view if isinstance(view, dict) else {}
    ignite = _d(view.get("ignite"))
    if not ignite:
        return c.page(_PAGE_ID, _PAGE_TITLE, "", c.empty("点燃数据未提供"))

    application = _d(ignite.get("application"))
    per_stack = _d(ignite.get("per_stack"))
    duration = _d(ignite.get("duration"))
    totals = _d(ignite.get("totals"))
    single = _d(_d(view.get("context")).get("single"))

    steps = (
        '<div class="step-list">'
        + _step_application(view, application)
        + _step_per_stack(view, per_stack)
        + _step_duration(view, duration, per_stack)
        + _step_totals(totals, single, application)
        + "</div>"
    )
    return c.page(_PAGE_ID, _PAGE_TITLE, "", steps)
