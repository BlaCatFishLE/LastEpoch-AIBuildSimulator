"""D10 shared player-report components.

Both damage models render their source rows, effect cards and action bars through
this module; model-specific data is converted by a tiny adapter in the model
renderer, but the markup, classes and DOM data attributes live here once.
"""

from __future__ import annotations

from html import escape as _escape
from typing import Iterable, Mapping, Sequence

SHARED_TAG = "data-shared-component"


def _esc(value) -> str:
    return "" if value is None else _escape(str(value), quote=True)


def source_row(
    *,
    name: str,
    caption: str = "",
    source_id: str = "",
    effect_html: str = "",
    status_html: str = "",
    row_class: str = "",
    cell_classes: Sequence[str] = ("source-name", "source-effect", "source-status"),
    row_attrs: str = "",
    button: bool = False,
) -> str:
    """One shared source row (HTML strings are already escaped by the caller)."""

    cells = list(cell_classes) + [""] * 3
    name_html = f"<b>{_esc(name)}</b>"
    if button and source_id:
        name_html = (
            f'<button type="button" class="link-btn source-name-link" '
            f'data-source-id="{_esc(source_id)}">{_esc(name)}</button>'
        )
    caption_html = f'<div class="small muted source-caption">{_esc(caption)}</div>' if caption else ""
    return (
        f'<tr class="{_esc(row_class) if row_class else 'source-row'}" data-source-row {row_attrs}>'
        f'<td class="{_esc(cells[0])}" data-source-id="{_esc(source_id)}">'
        f'<div class="source-title">{name_html}</div>{caption_html}</td>'
        f'<td class="{_esc(cells[1])}">{effect_html}</td>'
        f'<td class="{_esc(cells[2])}">{status_html}</td>'
        "</tr>"
    )


def source_main_cell(
    *, name: str, caption: str = "", source_id: str = "", rowspan: int = 1
) -> str:
    """Shared first-column source cell (grouped rows use a rowspan)."""

    span = f' rowspan="{max(1, int(rowspan))}"'
    caption_html = f'<span class="source-caption">{_esc(caption)}</span>' if caption else ""
    return (
        f'<td{span} class="source-main" data-source-id="{_esc(source_id)}">'
        f'<span class="source-name">{_esc(name)}</span>{caption_html}</td>'
    )


def source_group(
    *,
    name: str,
    caption: str = "",
    source_id: str = "",
    group_attrs: str = "",
    effects: Sequence[tuple[str, str]] = (),
    group_class: str = "source-group",
) -> str:
    """Shared grouped tbody: one source instance with its effect/status rows."""

    count = max(1, len(effects))
    rows: list[str] = []
    for index, (effect_html, status_html) in enumerate(effects or (("", ""),)):
        if index == 0:
            cells = source_main_cell(
                name=name, caption=caption, source_id=source_id, rowspan=count
            )
            rows.append(
                f'<tr class="source-row" data-source-row>{cells}<td>{effect_html}</td>'
                f'<td>{status_html}</td></tr>'
            )
        else:
            rows.append(f'<tr class="source-row" data-source-row><td>{effect_html}</td><td>{status_html}</td></tr>')
    return (
        f'<tbody class="{_esc(group_class)}" data-source-group {group_attrs}>'
        f'{"".join(rows)}</tbody>'
    )


def source_table(
    *,
    rows: Iterable[str] = (),
    body_html: str = "",
    table_class: str = "source-table",
    caption: str = "增伤来源：来源、效果与当前状态；可搜索和筛选。",
    headers: Sequence[str] = ("来源", "效果", "状态"),
) -> str:
    """Shared source-table shell used by both damage models."""

    head = "".join(f'<th scope="col">{_esc(title)}</th>' for title in headers)
    body = body_html if body_html else "<tbody>" + "".join(rows) + "</tbody>"
    return (
        f'<div class="table-wrap">'
        f'<table class="data-table {_esc(table_class)}" {SHARED_TAG}="source-table">'
        f'<caption class="sr-only">{_esc(caption)}</caption>'
        f"<thead><tr>{head}</tr></thead>{body}</table></div>"
    )


def effect_card(
    *,
    title: str,
    subtitle: str = "",
    status_label: str = "",
    status_tone: str = "muted",
    control_html: str = "",
    value_html: str = "",
    extra_body: str = "",
    attrs: str = "",
    card_class: str = "cond-effect",
    variant: str = "bh",
) -> str:
    """Shared effect card (BH conditions page and FR Buff page)."""

    status_html = (
        f'<span class="badge badge-{_esc(status_tone)} cond-status">{_esc(status_label)}</span>'
        if status_label
        else ""
    )
    if variant == "hit":
        head = (
            '<div class="cond-head cond-effect-head">'
            f'<div class="cond-effect-title"><h4 class="cond-effect-name">{_esc(title)}</h4>'
            + (f'<p class="cond-sub cond-effect-en muted small">{_esc(subtitle)}</p>' if subtitle else "")
            + "</div>"
            f'<div class="cond-effect-badges">{status_html}{control_html}</div></div>'
        )
        return (
            f'<div class="card {_esc(card_class)}" {SHARED_TAG}="effect-card" {attrs}>{head}'
            f"{value_html}{extra_body}</div>"
        )
    head = (
        '<header class="cond-effect-head">'
        '<div class="cond-effect-title">'
        f'<h4 class="cond-effect-name">{_esc(title)}</h4>'
        + (f'<p class="cond-effect-en muted small">{_esc(subtitle)}</p>' if subtitle else "")
        + "</div>"
        f'<div class="cond-effect-badges">{status_html}{control_html}</div></header>'
    )
    return (
        f'<div class="card {_esc(card_class)}" {SHARED_TAG}="effect-card" {attrs}>'
        f"{head}{value_html}{extra_body}</div>"
    )


def action_bar(buttons_html: str, *, aria_label: str, extra_class: str = "", status_html: str = "") -> str:
    """Shared toolbar/action-bar shell (buttons stay model-adapter HTML)."""

    return (
        f'<div class="toolbar cond-toolbar print-hide {_esc(extra_class)}" '
        f'role="group" aria-label="{_esc(aria_label)}" {SHARED_TAG}="action-bar">'
        f"{buttons_html}{status_html}</div>"
    )
