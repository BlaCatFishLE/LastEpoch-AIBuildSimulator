"""Document shell for the offline single-file damage report.

``render(result)`` builds the display view and glues the page modules into
one self-contained UTF-8 document: inline CSS, inline JS, two embedded JSON
payloads (the full engine ``result`` and the derived display ``view``), the
sticky header with mode/loop controls, the page tabs, the source-detail sidebar
and the scenario / report-info drawers.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from html import escape as _escape
from pathlib import Path

from . import components as c
from . import document, labels, present, units

__all__ = ["render", "write_html", "main", "TITLE"]

TITLE = "Black Hole 伤害分析"
_ASSETS_DIR = Path(__file__).with_name("assets")
#: display-only payloads rebuilt by the page modules; the client JS does not
#: consume them, so they are not duplicated into the embedded view JSON
_EMBED_VIEW_EXCLUDED = ("source_browser", "conditional", "limitations", "original_coverage")
_PAGE_MODULES = {
    "overview": ("pages.overview", "render_overview"),
    "composition": ("pages.composition", "render_composition"),
    "ignite": ("pages.ignite", "render_ignite"),
    "timeline": ("pages.timeline", "render_timeline"),
    "validation": ("pages.validation", "render_validation"),
    "conditions": ("pages.conditions", "render_conditions"),
}


def _asset(name: str) -> str:
    path = _ASSETS_DIR / name
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _s(value) -> str:
    return "" if value is None else str(value)


def _embed_json(payload) -> str:
    """Serialize JSON for a ``<script type="application/json">`` block.

    ``<``, ``>``, ``&`` and the JS line separators are escaped so the payload can
    never terminate the script element or be interpreted as HTML.
    """

    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


# --------------------------------------------------------------------------- #
# header pieces
# --------------------------------------------------------------------------- #


def _mode_controls(view: dict) -> str:
    context = view.get("context") or {}
    loops = context.get("loops") or []
    mode_buttons = "".join(
        f'<button type="button" class="seg-btn{" is-active" if mode == "single" else ""}" '
        f'data-mode-btn="{mode}" role="tab" aria-selected="{"true" if mode == "single" else "false"}">'
        f"{labels.MODE_LABELS[mode]}</button>"
        for mode in ("single", "loop")
    )
    policy_buttons = []
    for index, loop in enumerate(loops):
        policy_buttons.append(
            f'<button type="button" class="seg-btn{" is-active" if index == 0 else ""}" '
            f'data-policy-btn="{c.esc(loop.get("id"))}" title="{c.esc(loop.get("description"))}">'
            f'{c.esc(loop.get("label"))}</button>'
        )
    window_buttons = "".join(
        f'<button type="button" class="seg-btn{" is-active" if seconds == 30 else ""}" '
        f'data-window-btn="{seconds}">{seconds} 秒</button>'
        for seconds in (5, 30, 120)
    )
    loop_options = (
        '<div class="loop-options" data-mode-content="loop">'
        '<div class="seg-group"><span class="seg-label">策略</span>'
        f'{"".join(policy_buttons)}</div>'
        '<div class="seg-group"><span class="seg-label">窗口</span>'
        f"{window_buttons}</div></div>"
    )
    return (
        '<div class="header-controls">'
        '<div class="mode-switch" role="tablist" aria-label="报告模式">'
        f"{mode_buttons}</div>"
        f"{loop_options}"
        "</div>"
    )


def _status_badge(view: dict) -> str:
    context = view.get("context") or {}
    status = context.get("status") or "unknown"
    return c.status_badge(status, label=context.get("status_label") or None)


def _header_summary_html(view: dict) -> str:
    context = view.get("context") or {}
    scenario = context.get("scenario") or {}
    level = scenario.get("level")
    level_text = units.fmt_compact(level, "integer") if level is not None else "100"
    fire = scenario.get("target_resistance")
    fire_text = units.pct(fire) if fire is not None else "0%"
    return (
        '目标 <b>Lv<span class="num n-integer" data-sc="stat:level" data-kind="integer" '
        f'data-value="{c.esc(level_text)}">{c.esc(level_text)}</span></b>'
        ' · 火抗 <b><span class="num n-custom" data-sc="stat:targetRes" data-kind="fraction" '
        f'data-value="{c.esc("0" if fire is None else fire)}">{c.esc(fire_text)}</span></b>'
    )


def _header_extra_html(view: dict) -> str:
    scenario = (view.get("context") or {}).get("scenario") or {}
    return (
        '<span class="header-result-kind">条件已应用</span>'
        if scenario.get("conditional_applied")
        else ""
    )


def _header(view: dict) -> str:
    """Legacy header helper kept for import compatibility; render() uses document."""
    context = view.get("context") or {}
    character = context.get("character") or {}
    subtitle_bits = [str(character.get("class") or ""), str(character.get("mastery") or "")]
    subtitle = " · ".join(bit for bit in subtitle_bits if bit)
    subtitle = (subtitle + " · 预计伤害") if subtitle else "预计伤害"
    return (
        '<header class="app-header">'
        '<div class="header-top">'
        '<div class="brand">'
        '<span class="brand-mark" aria-hidden="true">BH</span>'
        f'<div class="brand-text"><h1>{_escape(TITLE)}</h1>'
        f'<p class="brand-sub">{c.esc(subtitle)}</p></div>'
        "</div>"
        '<div class="header-actions">'
        f'<span class="header-target-summary" aria-label="当前目标">{_header_summary_html(view)}</span>'
        f'{_header_extra_html(view)}'
        '<button type="button" class="btn btn-primary btn-small" data-open-drawer="scenario">目标设置</button>'
        '<details class="more-menu">'
        '<summary class="btn btn-small more-summary">更多</summary>'
        '<div class="more-popover">'
        '<label class="toggle"><input type="checkbox" data-toggle-numbers> 显示完整数字</label>'
        '<button type="button" class="more-item" data-print>打印 / 导出 PDF</button>'
        '<button type="button" class="more-item" data-export-json>导出结果 JSON</button>'
        "</div></details>"
        "</div></div>"
        + _mode_controls(view)
        + _nav(view)
        + "</header>"
    )


def _scenario_banner(view: dict) -> str:
    """Hidden until the reader applies a non-baseline target scenario."""

    return (
        '<div class="scenario-banner" id="scenario-banner" role="status" hidden>'
        '<span class="badge badge-info">自定义场景</span>'
        '<p>当前显示 <b data-sc-banner-summary>—</b>。'
        "数值按目标侧乘区统一变换（等级用引擎预计算系数表，目标火抗用抗性乘性公式），"
        "不重算伤害规则；与生成基线不同。规则账与证据文本保持生成基线口径。</p>"
        '<span class="scenario-check" data-sc-banner-check hidden></span>'
        '<button type="button" class="btn btn-small" data-reset-scenario>恢复生成基线</button>'
        "</div>"
    )


def _nav(view: dict) -> str:
    buttons = []
    for index, page in enumerate(view.get("pages") or []):
        page_id = page.get("id")
        active = " is-active" if index == 0 else ""
        selected = "true" if index == 0 else "false"
        label = page.get("label") or page_id
        buttons.append(
            f'<button type="button" class="page-tab{active}" id="tab-{c.esc(page_id)}" '
            f'role="tab" aria-selected="{selected}" aria-controls="page-{c.esc(page_id)}" '
            f'data-page-btn="{c.esc(page_id)}">'
            f'<span class="page-tab-label">{c.esc(label)}</span></button>'
        )
    return f'<nav class="page-tabs" role="tablist" aria-label="报告页面">{"".join(buttons)}</nav>'


def _global_status_banner(view: dict) -> str:
    context = view.get("context") or {}
    status = context.get("status") or "unknown"
    if status == "complete":
        return ""
    reason = context.get("status_reason") or ""
    friendly = {
        "assumption": "结果包含显式假设或未解决规则，不能视为精确值。",
        "incomplete": "结果尚未完整，缺失的输入或规则会影响数值。",
    }.get(status, "结果状态需要确认。")
    detail = (
        f'<details class="status-detail"><summary>技术说明</summary>'
        f'<p class="small">{c.esc(reason)}</p></details>'
        if reason
        else ""
    )
    return (
        '<div class="status-banner" role="status">'
        + _status_badge(view)
        + '<div class="status-banner-text">'
        + f'<p><strong>结果状态：{c.esc(context.get("status_label"))}</strong> — {friendly}'
        + "未解决项与依据见「来源与校验」。</p>"
        + detail
        + "</div></div>"
    )


# --------------------------------------------------------------------------- #
# source sidebar + drawers
# --------------------------------------------------------------------------- #


def _source_panel() -> str:
    return (
        '<div class="source-backdrop" id="source-backdrop" hidden></div>'
        '<aside class="source-panel" id="source-panel" aria-hidden="true" '
        'aria-labelledby="source-panel-title" role="dialog">'
        '<header class="source-panel-head">'
        '<h2 id="source-panel-title">来源详情</h2>'
        '<button type="button" class="icon-btn" data-close-source aria-label="关闭来源详情">关闭 ×</button>'
        "</header>"
        '<div class="source-panel-body" id="source-panel-body">'
        '<p class="muted">点击页面中的来源名称查看详情。</p>'
        "</div></aside>"
    )


def _scenario_result_summary(view: dict) -> str:
    """Current-result condition summary (values come from the displayed result)."""

    context = view.get("context") or {}
    scenario = context.get("scenario") or {}
    conditional = context.get("conditional") or {}
    summary = scenario.get("condition_summary") or {}
    level = scenario.get("level")
    fire_res = scenario.get("target_resistance")
    conditional_applied = bool(scenario.get("conditional_applied"))
    applied_count = int(_num_or_zero(conditional.get("applied_count")))
    baseline_count = int(_num_or_zero(conditional.get("baseline_count")))
    selected_not_applied = [item for item in conditional.get("selected_not_applied") or [] if isinstance(item, dict)]
    conflicts = summary.get("conflicts") or []
    conditions = scenario.get("conditions") or []
    state_by_id = {
        _s(condition.get("id")): condition for condition in conditions if isinstance(condition, dict)
    }

    def _state(condition_id: str) -> str:
        condition = state_by_id.get(condition_id) or {}
        if condition.get("conflict"):
            return "待复核"
        if condition.get("met") is None:
            return "待确认"
        return "是" if condition.get("met") else "否"

    rows = [
        ("目标", f"等级 {units.fmt_compact(level, 'integer')} · 火抗 {units.pct(fire_res)}"),
        (
            "目标条件",
            "稀有/首领：" + _state("target_is_boss_or_rare")
            + " · 中心 1 米内：" + _state("target_within_center_1m")
            + " · 双星同时覆盖：" + _state("both_stars_cover"),
        ),
    ]
    if conditional.get("available"):
        if conditional_applied:
            buff_text = f"{units.fmt_compact(applied_count, 'count')} 项生效"
            if baseline_count:
                buff_text += f"（另 {units.fmt_compact(baseline_count, 'count')} 项已计入基线）"
        else:
            buff_text = "未计入当前结果"
            if baseline_count:
                buff_text += f"（基础来源账已含 {units.fmt_compact(baseline_count, 'count')} 项）"
        rows.append(("条件 Buff", buff_text))
    rows_html = "".join(
        f'<div class="fact"><dt>{c.esc(label)}</dt><dd>{c.esc(text)}</dd></div>' for label, text in rows
    )
    conflict_html = ""
    if conflicts:
        conflict_html = (
            '<p class="note note-warn small val-condition-conflict">'
            f'条件别名取值不一致，需要复核：{c.esc("、".join(conflicts))}</p>'
        )
    wait_html = ""
    if selected_not_applied:
        names = "、".join(_s(item.get("name")) for item in selected_not_applied[:4])
        wait_html = (
            '<p class="note note-warn small val-condition-waiting">'
            f"{units.fmt_compact(len(selected_not_applied), 'count')} 项所选条件效果未计入当前结果"
            f"（{c.esc(names)}）。条件已更改，等待条件重算。</p>"
        )
    result_kind = _s(scenario.get("result_kind_label"))
    kind_html = (
        f'<p class="small muted val-result-kind">当前显示：{c.esc(result_kind)}</p>' if result_kind else ""
    )
    edit_button = (
        '<button type="button" class="btn btn-primary btn-small" data-close-drawer '
        'data-goto-page="conditions">编辑条件</button>'
    )
    return (
        f'<h3>当前计算条件</h3>'
        f'<dl class="fact-list val-condition-summary">{rows_html}</dl>'
        f"{kind_html}{conflict_html}{wait_html}"
        f'<div class="card-actions val-condition-actions">{edit_button}</div>'
    )


def _num_or_zero(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number else 0.0


def _sensitivity_table(view: dict) -> str:
    sensitivity = view.get("sensitivity") or {}
    rows = []
    row_attributes = []
    current = sensitivity.get("current_level")
    for item in sensitivity.get("rows") or []:
        level = item.get("level")
        if level == 0:
            # The DR-off engineering probe is not a normal target level.
            continue
        is_current = level == current
        rows.append(
            [
                c.esc(f"Lv{level}"),
                c.pct(item.get("dr")) if item.get("dr") is not None else "—",
                f'<span data-sens-cell="single">{c.num(item.get("single"), "damage")}</span>',
                f'<span data-sens-cell="eager">{c.num(item.get("eager_dps"), "dps")}</span>',
                f'<span data-sens-cell="patient">{c.num(item.get("patient_dps"), "dps")}</span>',
            ]
        )
        attrs = {"data-sens-row": level}
        if is_current:
            attrs["data-sens-current"] = "1"
        row_attributes.append(attrs)
    return c.table(
        ["等级", "减伤", "预计单次", "可用即施放 DPS", "尽量保留黑洞 DPS"],
        rows,
        numeric_cols=(1, 2, 3, 4),
        cls="sensitivity-table",
        row_attributes=row_attributes,
    )


_TARGET_STATE_IDS = (
    "target.boss_or_rare",
    "target.within_center_1m",
    "target.both_stars_cover",
    "target.chilled",
    "target.frozen",
)

_TARGET_STATE_LABELS = {
    "target.boss_or_rare": "首领或稀有敌人",
    "target.within_center_1m": "位于黑洞中心 1 米内",
    "target.both_stars_cover": "双星同时覆盖目标",
    "target.chilled": "目标处于冰缓",
    "target.frozen": "目标处于冻结",
}


def _target_condition_controls(view: dict) -> str:
    conditions_view = view.get("conditions") or {}
    states = {
        _s(item.get("id")): item
        for item in conditions_view.get("states") or []
        if isinstance(item, dict)
    }
    parts = []
    for state_id in _TARGET_STATE_IDS:
        state = states.get(state_id) or {}
        checked = bool(state.get("value"))
        label = _TARGET_STATE_LABELS.get(state_id) or _s(state.get("label")) or state_id
        parts.append(
            '<label class="target-check">'
            f'<input type="checkbox" data-cond-state="{c.esc(state_id)}"{" checked" if checked else ""}>'
            f"<span>{c.esc(label)}</span></label>"
        )
    return (
        '<fieldset class="target-conditions"><legend>目标条件</legend>'
        f'<div class="target-check-grid">{"".join(parts)}</div></fieldset>'
    )


def _drawers(view: dict) -> str:
    context = view.get("context") or {}
    scenario = context.get("scenario") or {}
    contract = view.get("scenario") or {}
    baseline = contract.get("baseline") or {}
    limits = contract.get("limits") or {}
    level = scenario.get("level")
    current_fire = scenario.get("target_resistance")
    input_level = units.fmt_compact(level, "integer") if level is not None else "100"
    input_fire = units.fmt_compact(current_fire * 100.0, "number") if current_fire is not None else "0"
    level_impact = _sensitivity_table(view)
    return (
        '<div class="drawer target-drawer" id="drawer-scenario" role="dialog" aria-modal="true" '
        'aria-labelledby="drawer-scenario-title" hidden>'
        '<div class="drawer-backdrop" data-close-drawer></div>'
        '<div class="drawer-panel">'
        '<header class="drawer-head"><div><h2 id="drawer-scenario-title">目标设置</h2>'
        '<p class="small muted">修改目标后点「应用」；Buff 在「Buff 设置」页配置。</p></div>'
        '<button type="button" class="icon-btn" data-close-drawer aria-label="关闭">关闭 ×</button></header>'
        '<div class="drawer-body">'
        '<form class="target-form" data-scenario-form novalidate>'
        '<div class="target-field-grid">'
        '<label class="target-field"><span class="target-field-label">怪物等级</span>'
        f'<input type="number" inputmode="numeric" data-scenario-level '
        f'min="{int(limits.get("level_min", 1))}" max="{int(limits.get("level_max", 100))}" '
        f'step="1" value="{c.esc(input_level)}" aria-label="怪物等级"></label>'
        '<label class="target-field"><span class="target-field-label">火焰抗性</span>'
        '<span class="target-input-wrap">'
        f'<input type="number" inputmode="numeric" data-scenario-fire-res '
        f'min="{int(limits.get("resistance_input_min", -100))}" '
        f'max="{int(limits.get("resistance_input_max", 200))}" step="1" '
        f'value="{c.esc(input_fire)}" aria-label="目标火焰抗性百分比">'
        '<span class="field-unit">%</span></span></label>'
        "</div>"
        + _target_condition_controls(view)
        + '<span class="scenario-error" data-scenario-error role="alert" hidden></span>'
        '<div class="target-actions">'
        '<button type="button" class="btn" data-reset-scenario data-reset-target>恢复默认目标</button>'
        '<button type="button" class="btn btn-primary" data-apply-scenario>应用</button>'
        "</div></form>"
        '<details class="level-impact"><summary>等级影响</summary>'
        f"{level_impact}</details>"
        "</div></div></div>"
    )


# --------------------------------------------------------------------------- #
# page assembly
# --------------------------------------------------------------------------- #


def _render_page(view: dict, page_id: str) -> str:
    module_name, function_name = _PAGE_MODULES.get(page_id, ("", ""))
    if not module_name:
        return ""
    try:
        module = importlib.import_module(f".{module_name}", __package__)
        renderer = getattr(module, function_name)
    except Exception as exc:  # pragma: no cover - staged development fallback
        return c.page(
            page_id,
            present.PAGE_LABELS.get(page_id, page_id),
            present.PAGE_TITLES.get(page_id, ""),
            c.empty(f"页面模块暂不可用：{exc}"),
        )
    return renderer(view)


def _footer(view: dict) -> str:
    return (
        '<footer class="app-footer">'
        '<span>预计伤害，不是游戏内实测 DPS。</span>'
        "</footer>"
    )


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #


def render(
    result: dict,
    *,
    capabilities: dict | None = None,
    recalc_endpoint: str | None = None,
    bindings: dict | None = None,
    recalc_bindings: dict | None = None,
) -> str:
    """Render one BH engine result dict into a complete offline HTML document.

    ``capabilities`` / ``recalc_endpoint`` are supplied by the service adapter
    (P3R-B); offline reports omit them and the local-recompute endpoint stays an
    explicit placeholder (never a hardcoded port).
    """

    view = present.build_view(result)
    # P3R-B service adapter contract: the local-recompute endpoint is injected at
    # render time. Offline renders keep the placeholder so the page states
    # "本机重算未启用" instead of hardcoding a loopback port.
    conditions_view = dict(view.get("conditions") or {})
    live_recompute = dict(conditions_view.get("live_recompute") or {})
    live_recompute["url"] = recalc_endpoint or document.RECALC_PLACEHOLDER
    live_recompute["available"] = bool(recalc_endpoint)
    conditions_view["live_recompute"] = live_recompute
    view["conditions"] = conditions_view
    context = view.get("context") or {}
    character = context.get("character") or {}
    subtitle_bits = [str(character.get("class") or ""), str(character.get("mastery") or "")]
    subtitle = " · ".join(bit for bit in subtitle_bits if bit)
    subtitle = (subtitle + " · 预计伤害") if subtitle else "预计伤害"
    page_ids = list(present.PAGE_IDS)
    pages = [
        {"id": page_id, "label": present.PAGE_LABELS.get(page_id, page_id)}
        for page_id in page_ids
    ]
    page_html = {page_id: _render_page(view, page_id) for page_id in page_ids}
    spec = document.DocumentSpec(
        model="black-hole",
        title=TITLE,
        brand_mark="BH",
        heading=TITLE,
        subtitle=subtitle,
        result=result,
        view=view,
        pages=pages,
        page_html=page_html,
        first_page="overview",
        header_summary_html=_header_summary_html(view),
        header_extra_html=_header_extra_html(view),
        header_controls_html=_mode_controls(view),
        target_button=True,
        number_toggle=True,
        drawers_html=_drawers(view),
        model_scripts=("app.js",),
        capabilities=capabilities
        or {
            "model": "black-hole",
            "ui_contract_version": document.UI_CONTRACT_VERSION,
            "recalc": {"supported": True, "endpoint_injected": bool(recalc_endpoint)},
            "pages": [
                {"id": page_id, "label": present.PAGE_LABELS.get(page_id, page_id)}
                for page_id in page_ids
            ],
        },
        recalc_endpoint=recalc_endpoint,
        bindings=bindings if bindings is not None else recalc_bindings,
        status_label="",
        embed_view_excluded=_EMBED_VIEW_EXCLUDED,
        body_attrs_extra=' data-mode="single" data-policy="eager" data-window="30"',
    )
    return document.render_document(spec)


def write_html(result: dict, path: str | Path) -> None:
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(result), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="le_calc.html",
        description="Render the offline single-file Black Hole damage report.",
    )
    parser.add_argument("--result", required=True, help="path to result.json")
    parser.add_argument("--out", required=True, help="output HTML path")
    args = parser.parse_args(argv)
    result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    write_html(result, args.out)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
