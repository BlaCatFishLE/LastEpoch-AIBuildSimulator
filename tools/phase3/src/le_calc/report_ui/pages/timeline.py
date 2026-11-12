"""时间轴页：伤害什么时候发生、什么时候只剩点燃。

图表、单位、图例与关键事件是主阅读内容；文件末尾的详细数据按需展开。
"""

from __future__ import annotations

from .. import components as c
from .. import units

#: view key_events/detail_events kinds -> player-facing short labels
_EVENT_LABELS = {
    "cast_start": "施法开始",
    "black_hole_spawn": "黑洞生成",
    "black_hole_destroyed": "黑洞结束",
    "observation_end": "点燃结束",
}

_DETAIL_LABELS = {
    "cast_start": "施法开始",
    "black_hole_spawn": "黑洞生成",
    "black_hole_destroyed": "黑洞结束",
    "observation_end": "点燃结束",
    "ignite_roll": "点燃判定",
    "ignite_tick": "点燃结算",
    "ignite_cohort_expiry": "点燃批次到期",
}


def _track_block(title: str, note: str, track: str, legend: str, cls: str = "") -> str:
    note_html = f'<p class="chart-note">{note}</p>' if note else ""
    return (
        f'<section class="chart-block {c.esc(cls)}" data-chart-block="{c.esc(track)}">'
        f'<header class="chart-head"><h3>{c.esc(title)}</h3>'
        f"{note_html}{legend}</header>"
        f'<div class="chart" data-track="{c.esc(track)}" role="img" '
        f'aria-label="{c.esc(title)}"></div></section>'
    )


def _legend(items: list[tuple[str, str]]) -> str:
    keys = "".join(
        f'<span class="legend-item"><i class="legend-dot legend-{c.esc(tone)}"></i>{c.esc(label)}</span>'
        for label, tone in items
    )
    return f'<div class="chart-legend">{keys}</div>'


def _event_label(event: dict) -> str:
    kind = str(event.get("kind") or "")
    if kind in _EVENT_LABELS:
        return _EVENT_LABELS[kind]
    return str(event.get("label") or kind)


def _detail_label(event: dict) -> str:
    kind = str(event.get("kind") or "")
    if kind == "body_tick":
        return ("火焰" if str(event.get("damage_type")) == "Fire" else "冰霜") + "本体结算"
    if kind in _DETAIL_LABELS:
        return _DETAIL_LABELS[kind]
    return str(event.get("label") or kind)


def _single_events(view: dict) -> str:
    single = (view.get("timeline") or {}).get("single") or {}
    key_events = single.get("key_events") or []
    buttons = "".join(
        f'<button type="button" class="event-btn" data-set-time="{units.fmt_compact(event.get("t"), "seconds")}">'
        f'<span class="event-time">{c.num(event.get("t"), "seconds")}</span>'
        f'<span class="event-name">{c.esc(_event_label(event))}</span></button>'
        for event in key_events
    )
    detail_rows = []
    for event in single.get("detail_events") or []:
        amount = event.get("amount")
        if event.get("kind") == "ignite_tick":
            group = "ignite"
        elif event.get("kind") == "body_tick":
            group = "fire" if event.get("damage_type") == "Fire" else "cold"
        else:
            group = None
        detail_rows.append(
            [
                c.num(event.get("t"), "seconds"),
                c.esc(_detail_label(event)),
                c.num(amount, "damage", sc=f"group:{group}") if amount and group else (
                    c.num(amount, "damage") if amount else '<span class="muted">—</span>'
                ),
            ]
        )
    detail_table = c.table(
        ["时刻", "事件", "金额"],
        detail_rows,
        cls="event-table",
        numeric_cols=(0, 2),
    )
    return (
        '<div class="event-toolbar print-hide">'
        '<div class="event-buttons" id="event-list">'
        f"{buttons}</div>"
        "</div>"
        + c.detail("逐跳结算明细", detail_table)
    )


def _bins_table(view: dict) -> str:
    single = (view.get("timeline") or {}).get("single") or {}
    bins = single.get("bins") or []
    rows = []
    row_attributes = []
    for item in bins:
        body = item.get("body")
        ignite = item.get("ignite")
        total = item.get("total")
        width = item.get("width")
        rows.append(
            [
                f'{units.fmt_compact(item.get("start"), "seconds")} – '
                f'{units.fmt_compact(item.get("end"), "seconds")}',
                f'<span data-bin-cell="body">{c.num(body, "damage")}</span>',
                f'<span data-bin-cell="ignite">{c.num(ignite, "damage")}</span>',
                f'<span data-bin-cell="total">{c.num(total, "damage")}</span>',
                f'<span data-bin-cell="dps">{c.num(item.get("dps"), "dps")}</span>',
            ]
        )
        row_attributes.append(
            {
                "data-sc-bin": "1",
                "data-body": f"{float(body or 0):.15g}",
                "data-ignite": f"{float(ignite or 0):.15g}",
                "data-width": f"{float(width or 0):.15g}",
            }
        )
    return c.table(
        ["区间（秒）", "本体伤害", "点燃伤害", "区间合计", "区间平均 DPS"],
        rows,
        cls="bins-table",
        numeric_cols=(0, 1, 2, 3, 4),
        row_attributes=row_attributes,
    )


def _loop_tables(view: dict) -> str:
    context = view.get("context") or {}
    loops = context.get("loops") or []
    blocks = []
    for loop in loops:
        rows = []
        row_attributes = []
        for window in loop.get("windows") or []:
            rows.append(
                [
                    f'{units.fmt_compact(window.get("seconds"), "seconds")}',
                    c.num(window.get("total"), "damage", sc="group:loop"),
                    c.num(window.get("dps"), "dps", sc="group:loop"),
                    c.num(window.get("casts"), "count"),
                    c.num(window.get("tail"), "damage", sc="group:ignite"),
                ]
            )
            row_attributes.append(
                {
                    "data-loop-policy": loop.get("id"),
                    "data-loop-window": window.get("seconds"),
                }
            )
        table = c.table(
            ["窗口", "窗口内伤害", "窗口平均 DPS", "窗口内施放", "窗口外尾部"],
            rows,
            numeric_cols=(0, 1, 2, 3, 4),
            caption="",
            row_attributes=row_attributes,
        )
        blocks.append(
            c.detail(f"{c.esc(loop.get('label'))} · 窗口汇总", table, cls="print-show")
        )
    return "".join(blocks) if blocks else c.empty("循环数据未提供")


def render_timeline(view: dict) -> str:
    timeline = view.get("timeline") or {}
    single = timeline.get("single") or {}
    peak = (single.get("peak_stacks") or 0.0)
    bin_width = single.get("bin_width") or 0.5

    legend_cumulative = _legend(
        [("本体累计", "fire"), ("点燃累计", "ignite"), ("总伤害", "total")]
    )
    legend_dps = _legend([("本体", "fire"), ("点燃", "ignite")])
    legend_stacks = _legend([("活跃点燃层数", "ignite"), ("峰值", "peak")])
    legend_state = _legend([("施法", "cast"), ("黑洞存续", "state"), ("仅点燃尾部", "tail")])

    toolbar = (
        '<div class="timeline-toolbar print-hide">'
        '<div class="timeline-toolbar-left">'
        '<span class="basis-chip">t = 0 为开始施法</span>'
        "</div>"
        '<div class="timeline-toolbar-right">'
        '<button type="button" class="btn btn-small" data-cursor-step="-1" title="上一事件">← 上一事件</button>'
        '<button type="button" class="btn btn-small" data-cursor-step="1" title="下一事件">下一事件 →</button>'
        '<button type="button" class="btn btn-small" data-cursor-reset>回到起点</button>'
        '<span class="window-readout">当前窗口：<b data-window-label>单次释放全程</b></span>'
        "</div></div>"
    )

    chart_stack = (
        '<div class="chart-stack" id="chart-stack" tabindex="0" '
        'aria-label="时间轴图表：使用左右方向键移动游标，Home/End 跳到起点或终点">'
        + _track_block(
            "累计伤害",
            "本体 + 点燃；黑洞结束后仍显示点燃尾部",
            "cumulative",
            legend_cumulative,
        )
        + _track_block(
            "每 0.5 秒平均 DPS",
            "每 0.5 秒平均 DPS",
            "dps",
            legend_dps,
            cls="chart-dps",
        )
        + _track_block(
            "点燃层数",
            "",
            "stacks",
            legend_stacks,
        )
        + _track_block(
            "技能持续",
            "",
            "state",
            legend_state,
        )
        + '<div class="chart-tooltip" id="chart-tooltip" hidden></div>'
        + "</div>"
    )

    readout = (
        '<div class="readout" id="timeline-readout" aria-live="polite">'
        '<div class="readout-item"><span class="readout-label">时刻</span>'
        '<span class="readout-value" data-readout="t">—</span></div>'
        '<div class="readout-item"><span class="readout-label">累计本体</span>'
        '<span class="readout-value" data-readout="body">—</span></div>'
        '<div class="readout-item"><span class="readout-label">累计点燃</span>'
        '<span class="readout-value" data-readout="ignite">—</span></div>'
        '<div class="readout-item"><span class="readout-label">累计总伤害</span>'
        '<span class="readout-value" data-readout="total">—</span></div>'
        '<div class="readout-item"><span class="readout-label">区间平均 DPS</span>'
        '<span class="readout-value" data-readout="dps">—</span></div>'
        '<div class="readout-item"><span class="readout-label">活跃点燃层数</span>'
        '<span class="readout-value" data-readout="stacks">—</span></div>'
        '<div class="readout-item"><span class="readout-label">存续黑洞数</span>'
        '<span class="readout-value" data-readout="bh">—</span></div>'
        '<div class="readout-item readout-loop"><span class="readout-label">充能层数</span>'
        '<span class="readout-value" data-readout="charges">—</span></div>'
        "</div>"
    )

    single_panel = (
        '<div data-mode-panel="single">'
        + c.card(
            _single_events(view),
            title="关键事件",
            cls="card-events",
        )
        + "</div>"
    )
    loop_panel = (
        '<div data-mode-panel="loop">'
        + c.card(
            _loop_tables(view),
            title="循环窗口汇总",
            cls="card-events",
        )
        + "</div>"
    )

    tables = c.detail(
        "详细数据",
        c.table(
            ["说明", "值"],
            [
                ["单次观察结束", c.num(single.get("end_s"), "seconds")],
                ["黑洞生成", c.num(single.get("spawn_s"), "seconds")],
                ["黑洞结束", c.num(single.get("bh_end_s"), "seconds")],
            ],
            numeric_cols=(1,),
        )
        + c.detail("详细数据", _bins_table(view)),
        cls="print-show",
    )

    body = "".join(
        [
            toolbar,
            chart_stack,
            readout,
            single_panel,
            loop_panel,
            tables,
        ]
    )
    return c.page(
        "timeline",
        "时间轴",
        "",
        body,
        extra=f' data-horizon="{units.fmt_compact(single.get("end_s"), "seconds")}"',
    )
