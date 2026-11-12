"""增伤来源页：玩家可读的增伤来源、效果与当前状态。

The page is a searchable source list, not an audit report.  It renders only
what a player needs to answer "this damage increase — where does it come
from?": the game/item/skill name, the attribute it adds with exactly one value,
and whether it currently applies.

Removed from this page (data stays in the result/JSON artifacts):
rule ledger, evidence/confirmation levels, check records, excluded-source
audit, original-build coverage, report/run/input information, limitations and
schema/locator/raw ids.

All data comes from ``view["source_browser"]`` rows produced by
``present.build_view``; the page never re-computes a damage rule.
"""

from __future__ import annotations

from .. import components as c
from .. import labels, shared_ui, units

__all__ = ["render_validation"]

#: category reading order (mirrors the view's browser order)
_CATEGORY_ORDER = [
    "equipment",
    "idol",
    "blessing",
    "idol_altar",
    "passive_node",
    "skill_node",
    "other_skill",
    "action",
    "base_character",
    "derived",
    "quest",
    "unknown",
]


def _s(value, default: str = "") -> str:
    return default if value is None else str(value)


def _d(value) -> dict:
    return value if isinstance(value, dict) else {}


def _l(value) -> list:
    return value if isinstance(value, list) else []


def _attribute_value_html(attribute: dict) -> str:
    """One attribute value, never appended twice after its label."""

    text = _s(attribute.get("value_text"))
    value = attribute.get("value")
    kind = _s(attribute.get("kind")) or "text"
    unit = _s(attribute.get("unit"))
    if value is None or kind == "text":
        return f'<b class="val-attr-value">{c.esc(text)}</b>'
    if kind == "fraction":
        return c.num(value, "fraction", compact_text=text)
    if kind in ("seconds", "points", "stacks", "count", "integer"):
        compact = f"{text} {unit}".strip() if unit else text
        return c.num(value, kind, compact_text=compact)
    return c.num(value, kind, compact_text=f"{text} {unit}".strip() if unit else text)


def _attribute_line_html(attribute: dict, *, with_applies: bool = False) -> str:
    qualifier = _s(attribute.get("qualifier"))
    name = _s(attribute.get("name"))
    qualifier_html = (
        f'<span class="val-attr-qualifier">{c.esc(qualifier)}</span>' if qualifier else ""
    )
    state_html = ""
    if attribute.get("show_state"):
        state_label = _s(attribute.get("state_label"))
        if state_label:
            state_html = (
                '<span class="small muted val-attr-state">'
                f"（{c.esc(state_label)}）</span>"
            )
    applies_html = ""
    if with_applies:
        applies = [label_text for label_text in _l(attribute.get("applies")) if label_text]
        if applies:
            chips = "".join(
                f'<span class="chip val-branch">{c.esc(label_text)}</span>' for label_text in applies
            )
            applies_html = f'<span class="val-attr-applies">{chips}</span>'
    return (
        '<div class="val-attr">'
        f"{qualifier_html}"
        f'<span class="val-attr-name">{c.esc(name)}</span> '
        f"{_attribute_value_html(attribute)}{state_html}{applies_html}"
        "</div>"
    )


def _applies_html(applies: list[str]) -> str:
    chips = "".join(
        f'<span class="chip val-branch">{c.esc(label_text)}</span>' for label_text in applies
    )
    if not chips:
        return ""
    return (
        '<div class="val-applies-row"><span class="muted small">作用位置</span>'
        f"{chips}</div>"
    )


def _details_html(row: dict) -> str:
    """Expansion with the attributes the main row could not hold."""

    player = _d(row.get("player"))
    attributes = [item for item in _l(player.get("attributes")) if isinstance(item, dict)]
    applies = [label_text for label_text in _l(player.get("applies")) if label_text]
    extra_attributes = attributes[3:]
    parts: list[str] = []
    if extra_attributes:
        items = "".join(
            f"<li>{_attribute_line_html(attribute, with_applies=True)}</li>"
            for attribute in extra_attributes
        )
        parts.append(f'<ul class="val-attr-list">{items}</ul>')
    parts.append(_applies_html(applies))
    body = "".join(part for part in parts if part)
    if not body:
        return ""
    return c.detail("属性明细", body, cls="val-source-detail")


def _condition_html(player: dict) -> str:
    condition = _s(player.get("condition"))
    if not condition:
        return ""
    return f'<p class="small muted val-condition">{c.esc(condition)}</p>'


def _state_html(player: dict) -> str:
    status_label = _s(player.get("status_label")) or labels.PLAYER_STATUS_LABELS["unknown"]
    tone = _s(player.get("status_tone")) or "muted"
    return (
        f'<div class="val-state">{c.badge(status_label, tone)}'
        f"{_condition_html(player)}</div>"
    )


def _source_row_html(row: dict) -> str:
    player = _d(row.get("player"))
    category = _s(row.get("category")) or "unknown"
    included = "1" if player.get("enabled") else "0"
    title = _s(player.get("title")) or _s(row.get("name")) or "未命名来源"
    subtitle = _s(player.get("subtitle"))
    subtitle_html = f'<div class="small muted">{c.esc(subtitle)}</div>' if subtitle else ""
    attributes = [item for item in _l(player.get("attributes")) if isinstance(item, dict)]
    inline = "".join(_attribute_line_html(attribute) for attribute in attributes[:3])
    if len(attributes) > 3:
        inline += f'<div class="small muted">另有 {len(attributes) - 3} 项属性</div>'
    if not inline:
        inline = '<span class="muted small">未提供数值</span>'
    effect_names = [name for name in _l(player.get("inline_effect_names")) if name]
    if effect_names:
        inline += (
            '<div class="small muted val-effect-family">'
            f'{c.esc(" / ".join(effect_names))}</div>'
        )
    inline_applies = [label_text for label_text in _l(player.get("inline_applies")) if label_text]
    if inline_applies:
        inline += _applies_html(inline_applies)
    details = _details_html(row) if player.get("needs_details") else ""
    return (
        f'<tr class="val-source-row" data-source-row data-category="{c.esc(category)}" '
        f'data-included="{included}" data-search="{c.esc(_s(player.get("search")))}">'
        f'<td class="source-name val-cell-name"><div class="val-source-title">'
        f"<b>{c.esc(title)}</b></div>{subtitle_html}</td>"
        f'<td class="val-cell-effect">{inline}{details}</td>'
        f'<td class="val-cell-state">{_state_html(player)}</td>'
        "</tr>"
    )


def _source_row_html(row: dict) -> str:
    player = _d(row.get("player"))
    category = _s(row.get("category")) or "unknown"
    included = "1" if player.get("enabled") else "0"
    title = _s(player.get("title")) or _s(row.get("name")) or "未命名来源"
    subtitle = _s(player.get("subtitle"))
    attributes = [item for item in _l(player.get("attributes")) if isinstance(item, dict)]
    inline = "".join(_attribute_line_html(attribute) for attribute in attributes[:3])
    if len(attributes) > 3:
        inline += f'<div class="small muted">另有 {len(attributes) - 3} 项属性</div>'
    if not inline:
        inline = '<span class="muted small">未提供数值</span>'
    effect_names = [name for name in _l(player.get("inline_effect_names")) if name]
    if effect_names:
        inline += (
            '<div class="small muted val-effect-family">'
            f'{c.esc(" / ".join(effect_names))}</div>'
        )
    inline_applies = [label_text for label_text in _l(player.get("inline_applies")) if label_text]
    if inline_applies:
        inline += _applies_html(inline_applies)
    details = _details_html(row) if player.get("needs_details") else ""
    row_attrs = (
        f'data-category="{c.esc(category)}" data-included="{included}" '
        f'data-search="{c.esc(_s(player.get("search")))}"'
    )
    return shared_ui.source_row(
        name=title,
        caption=subtitle,
        source_id=_s(row.get("source_id")) or _s(player.get("id")),
        effect_html=inline + details,
        status_html=_state_html(player),
        row_class="val-source-row",
        cell_classes=("source-name val-cell-name", "val-cell-effect", "val-cell-state"),
        row_attrs=row_attrs,
    )


def _source_table_html(rows: list[dict]) -> str:
    body_rows = [_source_row_html(row) for row in rows]
    return shared_ui.source_table(
        rows=body_rows,
        table_class="source-table val-source-table",
        caption="增伤来源：来源、效果与当前状态；可搜索和筛选。",
        headers=("来源", "效果", "状态"),
    )


def _source_groups(rows: list[dict]) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        category = _s(row.get("category")) or "unknown"
        buckets.setdefault(category, []).append(row)
    ordered = [category for category in _CATEGORY_ORDER if category in buckets]
    ordered += sorted(category for category in buckets if category not in _CATEGORY_ORDER)
    groups: list[dict] = []
    for category in ordered:
        group_rows = buckets[category]
        if not group_rows:
            continue
        groups.append(
            {
                "category": category,
                "label": _s(group_rows[0].get("category_label"))
                or labels.CATEGORY_LABELS.get(category, "其他来源"),
                "rows": group_rows,
            }
        )
    return groups


def _source_group_html(group: dict) -> str:
    category = c.esc(_s(group["category"]))
    rows = group["rows"]
    return (
        f'<details class="source-group val-source-group" data-source-group="{category}">'
        '<summary class="val-group-summary">'
        f'<span class="val-group-name">{c.esc(_s(group["label"]))}</span>'
        f'<span class="count-pill" data-group-count="{len(rows)}">{len(rows)}</span>'
        "</summary>"
        '<div class="source-group-body val-group-body">'
        f"{_source_table_html(rows)}"
        "</div></details>"
    )


def _source_toolbar(groups: list[dict], total: int) -> str:
    options = ['<option value="all">全部类别</option>']
    for group in groups:
        options.append(
            f'<option value="{c.esc(_s(group["category"]))}">'
            f'{c.esc(_s(group["label"]))}（{len(group["rows"])}）</option>'
        )
    return (
        '<div class="toolbar val-source-toolbar print-hide" role="group" aria-label="筛选增伤来源">'
        '<label class="val-field" for="source-search">'
        '<span class="val-field-label">搜索</span>'
        '<input type="search" id="source-search" data-source-search autocomplete="off" '
        'placeholder="装备、技能、效果…" aria-label="搜索增伤来源"></label>'
        '<label class="val-field" for="source-category">'
        '<span class="val-field-label">类别</span>'
        f'<select id="source-category" data-source-category aria-label="按类别筛选">{"".join(options)}</select>'
        "</label>"
        '<label class="val-field" for="source-included">'
        '<span class="val-field-label">状态</span>'
        '<select id="source-included" data-source-included aria-label="按状态筛选">'
        '<option value="all">全部</option>'
        '<option value="1">已生效</option>'
        '<option value="0">未生效</option></select></label>'
        '<button type="button" class="link-btn val-expand-all" data-expand-all="source">展开全部</button>'
        f'<span class="muted small val-source-total" data-filter-count data-source-count>'
        f"共 {total} 条</span>"
        "</div>"
    )


def _player_rows(view: dict) -> list[dict]:
    browser = _d(view.get("source_browser"))
    rows: list[dict] = []
    for row in _l(browser.get("primary")):
        if not isinstance(row, dict):
            continue
        player = _d(row.get("player"))
        if not player.get("has_contribution"):
            continue
        rows.append(row)
    if not rows:
        # degraded/legacy results: fall back to included ledger rows without the
        # player enrichment so the page still lists the sources it has
        for row in _l(browser.get("primary")):
            player = _d(_d(row).get("player"))
            if player:
                rows.append(row)
    return rows


def render_validation(view: dict) -> str:
    """Render the player-facing ``增伤来源`` page section."""

    if not isinstance(view, dict):
        view = {}
    rows = _player_rows(view)
    groups = _source_groups(rows)
    if not groups:
        body = c.empty("结果中没有可展示的增伤来源")
    else:
        body = (
            '<div id="val-sources" class="val-sources">'
            + _source_toolbar(groups, len(rows))
            + "".join(_source_group_html(group) for group in groups)
            + '<p class="empty-state val-source-empty" data-source-empty hidden>'
            "没有匹配的来源；清空搜索或切换筛选后再试。</p>"
            + "</div>"
        )
    return c.page("validation", "增伤来源", "", body)
