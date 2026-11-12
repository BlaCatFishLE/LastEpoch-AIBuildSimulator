"""总览页：这次释放造成多少伤害？"""

from __future__ import annotations

from .. import components as c
from .. import labels, units


def _rule_pending(view: dict) -> list[str]:
    rules = view.get("validation", {}).get("rules") or []
    pending = [rule for rule in rules if rule.get("status") in ("assumed", "unresolved")]
    return pending


def _single_kpis(view: dict) -> str:
    context = view.get("context") or {}
    single = context.get("single") or {}
    parts = single.get("body_parts") or []
    body_hint = " + ".join(
        f"{part.get('label')} {c.num(part.get('total'), 'damage', sc='group:' + str(part.get('key') or 'body'))}"
        for part in parts
        if part.get("total") is not None
    )
    observation_end = single.get("observation_end_s")
    cards = [
        c.kpi(
            "预计单次伤害",
            c.num(single.get("total"), "damage", sc="group:total"),
            hint="本体与点燃，含黑洞消失后的点燃尾部",
            tone="primary",
        ),
        c.kpi(
            "本体伤害",
            c.num(single.get("body_total"), "damage", sc="group:body"),
            hint=body_hint,
            tone="fire",
        ),
        c.kpi(
            "点燃伤害",
            c.num(single.get("ignite_total"), "damage", sc="group:ignite"),
            hint="尾部 " + c.num(single.get("ignite_tail"), "damage", sc="group:ignite"),
            tone="ignite",
        ),
        c.kpi(
            "持续时间",
            f"黑洞 {units.seconds(single.get('bh_duration_s'))}",
            hint="点燃结束 " + units.seconds(observation_end),
            tone="state",
        ),
    ]
    return f'<div class="kpi-row kpi-row-4">{"".join(cards)}</div>'


def _loop_controls(view: dict) -> str:
    context = view.get("context") or {}
    loops = context.get("loops") or []
    policy_buttons = []
    for index, loop in enumerate(loops):
        active = " is-active" if index == 0 else ""
        policy_buttons.append(
            f'<button type="button" class="seg-btn{active}" data-policy-btn="{c.esc(loop.get("id"))}" '
            f'title="{c.esc(loop.get("description"))}">{c.esc(loop.get("label"))}</button>'
        )
    window_buttons = []
    for seconds in (5, 30, 120):
        active = " is-active" if seconds == 30 else ""
        window_buttons.append(
            f'<button type="button" class="seg-btn{active}" data-window-btn="{seconds}">{seconds} 秒</button>'
        )
    return (
        '<div class="loop-controls print-hide">'
        '<div class="seg-group" role="group" aria-label="施放策略">'
        '<span class="seg-label">施放策略</span>'
        f'{"".join(policy_buttons)}</div>'
        '<div class="seg-group" role="group" aria-label="观察窗口">'
        '<span class="seg-label">观察窗口</span>'
        f'{"".join(window_buttons)}</div>'
        "</div>"
    )


def _loop_kpi_blocks(view: dict) -> str:
    context = view.get("context") or {}
    loops = context.get("loops") or []
    blocks = []
    for loop_index, loop in enumerate(loops):
        policy_id = loop.get("id")
        for window_index, window in enumerate(loop.get("windows") or []):
            active = " is-active" if (loop_index == 0 and window.get("key") == "30") else ""
            cards = [
                c.kpi(
                    "窗口平均 DPS",
                    c.num(window.get("dps"), "dps", sc="group:loop"),
                    unit="",
                    hint=f"{loop.get('label')} · {window.get('seconds')} 秒窗口；分母为窗口长度",
                    tone="primary",
                ),
                c.kpi(
                    "窗口内伤害",
                    c.num(window.get("total"), "damage", sc="group:loop"),
                    hint=f"本体 {c.num(window.get('body'), 'damage', sc='group:body')} + 点燃 "
                    f"{c.num(window.get('ignite'), 'damage', sc='group:ignite')}",
                    tone="ignite",
                ),
                c.kpi(
                    "窗口内施放",
                    c.num(window.get("casts"), "count"),
                    unit="次",
                    hint=f"整段 {window.get('seconds')} 秒内被接受的施放次数",
                    tone="state",
                ),
                c.kpi(
                    "窗口外尾部",
                    c.num(window.get("tail"), "damage", sc="group:ignite"),
                    hint="窗口结束时仍在生效、尚未结算的点燃伤害；不进入窗口 DPS",
                    tone="plain",
                ),
            ]
            blocks.append(
                f'<div class="loop-kpi{active}" data-loop-kpi data-policy="{c.esc(policy_id)}" '
                f'data-window="{c.esc(window.get("key"))}">'
                f'<div class="kpi-row kpi-row-4">{"".join(cards)}</div></div>'
            )
    loop_summary = []
    for loop in loops:
        loop_summary.append(
            f'<div class="loop-summary-line" data-loop-policy="{c.esc(loop.get("id"))}" data-loop-window="120">'
            f'<span class="loop-summary-name">{c.esc(loop.get("label"))}</span>'
            f'<span>循环间隔 {c.num(loop.get("cycle_s"), "seconds")}</span>'
            f'<span>窗口内施放 {c.num(loop.get("casts"), "count")}</span>'
            f'<span>最大同时存续 {c.num(loop.get("max_concurrent"), "count")} 个黑洞</span>'
            f'<span>稳态交叉验证 {c.num(loop.get("steady_state_dps"), "dps", sc="group:loop")} DPS</span>'
            "</div>"
        )
    return "".join(blocks) + f'<div class="loop-summary">{"".join(loop_summary)}</div>'


def _composition_card(view: dict) -> str:
    composition = view.get("composition") or {}
    bar = composition.get("bar") or []
    meter = c.meter(bar)
    return c.card(meter, title="伤害组成", cls="card-composition")


def _phase_bar(view: dict) -> str:
    timeline = view.get("timeline") or {}
    single = timeline.get("single") or {}
    spawn = single.get("spawn_s") or 0.0
    bh_end = single.get("bh_end_s") or 0.0
    end = single.get("end_s") or 0.0
    if not end:
        return c.empty("时间数据未提供")
    segments = [
        {"key": "cast", "label": "施法", "start": 0.0, "end": spawn, "tone": "cast"},
        {"key": "bh", "label": "黑洞存续", "start": spawn, "end": bh_end, "tone": "state"},
        {"key": "tail", "label": "仅点燃尾部", "start": bh_end, "end": end, "tone": "ignite"},
    ]
    bars = []
    for segment in segments:
        width = max(0.0, (segment["end"] - segment["start"]) / end * 100.0)
        if width <= 0:
            continue
        label = segment["label"] if width >= 12 else ""
        bars.append(
            f'<span class="phase-seg phase-{segment["tone"]}" style="width:{width:.3f}%" '
            f'title="{c.esc(segment["label"])} {units.fmt_compact(segment["start"], "seconds")} – '
            f'{units.fmt_compact(segment["end"], "seconds")}">{c.esc(label)}</span>'
        )
    milestones = single.get("milestones") or []
    milestone_items = []
    for milestone in milestones:
        milestone_items.append(
            f'<li class="milestone"><span class="milestone-dot"></span>'
            f'<span class="milestone-time">{c.num(milestone.get("t"), "seconds")}</span>'
            f'<span class="milestone-label">{c.esc(milestone.get("label"))}</span></li>'
        )
    return (
        f'<div class="phase-bar">{"".join(bars)}</div>'
        f'<ol class="milestone-list">{"".join(milestone_items)}</ol>'
        ""
    )


def _pending_card(view: dict) -> str:
    validation = view.get("validation") or {}
    rules = validation.get("rules") or []
    pending = [rule for rule in rules if rule.get("status") in ("assumed", "unresolved")]
    issues = validation.get("issues") or []
    warnings = [issue for issue in issues if issue.get("severity") in ("warning", "error", "critical", "high")]
    items = []
    for rule in pending:
        items.append(
            f'<li class="pending-item">{c.rule_badge(rule.get("status"))}'
            f'<span class="pending-title">{c.esc(rule.get("title"))}</span>'
            f'<span class="muted small">{c.esc((rule.get("statement") or "")[:150])}…</span></li>'
        )
    for issue in warnings[:3]:
        items.append(
            f'<li class="pending-item">{c.severity_badge(issue.get("severity"))}'
            f'<span class="pending-title">{c.esc(issue.get("title"))}</span>'
            f'<span class="muted small">{c.esc((issue.get("detail") or "")[:150])}…</span></li>'
        )
    summary = (
        f"假设规则 {len([r for r in pending if r.get('status') == 'assumed'])} 条 · "
        f"未解决 {len([r for r in pending if r.get('status') == 'unresolved'])} 条 · "
        f"需处理 {len(warnings)} 条"
    )
    body = (
        f'<p class="pending-summary">{c.esc(summary)}</p>'
        f'<ul class="pending-list">{"".join(items)}</ul>'
        '<div class="card-actions"><button type="button" class="btn" data-goto-page="validation">'
        "查看全部规则与证据</button></div>"
    )
    return c.card(body, title="影响结果的待确认项", cls="card-pending")


def _scenario_card(view: dict) -> str:
    context = view.get("context") or {}
    scenario = context.get("scenario") or {}
    body = (
        f'<p class="small muted">目标：等级 <b>{c.num(scenario.get("level"), "integer", sc="stat:level")}</b> · '
        f'火抗 <b>{c.num(scenario.get("target_resistance"), "fraction", sc="stat:targetRes")}</b></p>'
    )
    return c.card(body, title="目标状态", cls="card-scenario")


def _conditions_card(view: dict) -> str:
    conditions = view.get("conditions") or {}
    if not conditions.get("available"):
        return ""
    counts = conditions.get("counts") or {}
    chips = "".join(
        [
            c.chip(f"候选 {counts.get('candidates', 0)}", tone="info"),
            c.chip(f"已配置 {counts.get('configured', 0)}", tone="muted"),
            c.chip(f"相关 {counts.get('relevant', 0)}", tone="ok"),
            c.chip(f"待确认 {counts.get('unconfirmed', 0)}", tone="warn"),
        ]
    )
    applied = (view.get("conditional") or {}).get("applied_count") or 0
    status_line = (
        f'<p class="small muted">条件场景已应用：{c.num(applied, "count")} 项条件 Buff 生效。</p>'
        if (view.get("context") or {}).get("scenario", {}).get("conditional_applied")
        else '<p class="small muted">可设置开关、层数与目标条件，并生成可复核的条件场景。</p>'
    )
    body = (
        f'<div class="chip-row">{chips}</div>'
        f"{status_line}"
        '<div class="card-actions"><button type="button" class="btn btn-primary" data-goto-page="conditions">'
        "配置条件 Buff</button></div>"
    )
    return c.card(body, title="条件 Buff 配置", cls="card-conditions")


def render_overview(view: dict) -> str:
    context = view.get("context") or {}
    status = context.get("status")
    body = []
    display_baseline = (view.get("conditions") or {}).get("display_baseline") or {}
    if display_baseline.get("label"):
        body.append(
            '<p class="section-note val-display-baseline">'
            f'当前页面主口径：<b>{c.esc(display_baseline.get("label"))}</b>。'
            f'{c.esc(display_baseline.get("note") or "")}'
            "</p>"
        )
    body.append(
        '<div class="overview-top">'
        + '<div class="overview-kpis" data-mode-panel="single">'
        + _single_kpis(view)
        + "</div>"
        + '<div class="overview-kpis" data-mode-panel="loop">'
        + _loop_kpi_blocks(view)
        + "</div>"
        + "</div>"
    )
    body.append(_composition_card(view))
    body.append(c.card(_phase_bar(view), title="关键时间", cls="card-timeline-strip"))
    return c.page(
        "overview",
        "总览",
        "",
        "".join(body),
        extra=f' data-status="{c.esc(status or "")}"',
    )
