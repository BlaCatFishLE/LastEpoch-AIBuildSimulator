"""Shared HTML fragments for the offline report pages.

Pages are plain Python functions returning HTML strings; every fragment that
appears on more than one page lives here so the report keeps one visual
vocabulary.  Text is HTML-escaped by the helpers - never interpolate raw data
without going through ``esc`` or a ``unit`` helper.
"""

from __future__ import annotations

from html import escape as _escape
from typing import Iterable, Sequence

from . import labels, units

__all__ = [
    "esc",
    "page",
    "section",
    "card",
    "badge",
    "chip",
    "tone_for_status",
    "kpi",
    "detail",
    "details",
    "table",
    "empty",
    "note",
    "fact_list",
    "source_chip",
    "source_chip_by_id",
    "source_link",
    "status_badge",
    "rule_badge",
    "severity_badge",
    "meter",
    "num",
    "pct",
    "factor",
    "seconds",
    "stacks",
    "signed_pct",
    "fmt",
    "fmt_compact",
    "fmt_full",
    "source_color",
    "inline_list",
]

# Re-export the unit helpers so pages only import this module.
num = units.num
pct = units.pct
factor = units.factor
seconds = units.seconds
stacks = units.stacks
signed_pct = units.signed_pct
fmt = units.fmt
fmt_compact = units.fmt_compact
fmt_full = units.fmt_full


def esc(value) -> str:
    if value is None:
        return ""
    return _escape(str(value), quote=True)


# --------------------------------------------------------------------------- #
# page + section scaffolding
# --------------------------------------------------------------------------- #


def page(page_id: str, title: str, question: str, body: str, *, extra: str = "") -> str:
    question_html = f'<p class="page-question">{esc(question)}</p>' if question else ""
    return (
        f'<section class="page" id="page-{esc(page_id)}" data-page-panel="{esc(page_id)}" '
        f'role="tabpanel" aria-labelledby="tab-{esc(page_id)}"{extra}>\n'
        f'  <header class="page-head">\n'
        f'    <div><h2>{esc(title)}</h2>{question_html}</div>\n'
        f"  </header>\n{body}\n</section>\n"
    )


def section(title: str, body: str, *, subtitle: str = "", section_id: str = "", cls: str = "", extra: str = "") -> str:
    id_attr = f' id="{esc(section_id)}"' if section_id else ""
    sub = f'<p class="section-sub">{esc(subtitle)}</p>' if subtitle else ""
    return (
        f'<section class="block {esc(cls)}"{id_attr}{extra}>\n'
        f'  <header class="block-head"><h3>{esc(title)}</h3>{sub}</header>\n'
        f"  {body}\n</section>\n"
    )


def card(body: str, *, title: str = "", cls: str = "", card_id: str = "", extra: str = "") -> str:
    id_attr = f' id="{esc(card_id)}"' if card_id else ""
    head = f'<header class="card-head"><h4>{esc(title)}</h4></header>' if title else ""
    return f'<div class="card {esc(cls)}"{id_attr}{extra}>{head}{body}</div>'


def badge(text: str, tone: str = "muted", *, title: str = "", cls: str = "") -> str:
    title_attr = f' title="{esc(title)}"' if title else ""
    return f'<span class="badge badge-{esc(tone)} {esc(cls)}"{title_attr}>{esc(text)}</span>'


def chip(text: str, *, tone: str = "", cls: str = "") -> str:
    return f'<span class="chip {esc("chip-" + tone) if tone else ""} {esc(cls)}">{esc(text)}</span>'


def tone_for_status(status: str) -> str:
    return labels.STATUS_TONES.get(status, "muted")


def status_badge(status: str, *, label: str | None = None) -> str:
    status = status or "unknown"
    return badge(label or labels.STATUS_LABELS.get(status, "状态未知"), tone_for_status(status))


def rule_badge(status: str) -> str:
    status = status or "unknown"
    return badge(
        labels.RULE_STATUS_LABELS.get(status, status),
        labels.RULE_STATUS_TONES.get(status, "muted"),
    )


def severity_badge(severity: str) -> str:
    severity = severity or "unknown"
    return badge(
        labels.SEVERITY_LABELS.get(severity, severity),
        labels.SEVERITY_TONES.get(severity, "muted"),
    )


def kpi(
    label: str,
    value_html: str,
    *,
    unit: str = "",
    hint: str = "",
    tone: str = "plain",
    value_class: str = "",
) -> str:
    unit_html = f'<span class="kpi-unit">{esc(unit)}</span>' if unit else ""
    hint_html = f'<p class="kpi-hint">{hint}</p>' if hint else ""
    return (
        f'<article class="kpi kpi-{esc(tone)} {esc(value_class)}">'
        f'<p class="kpi-label">{esc(label)}</p>'
        f'<p class="kpi-value">{value_html}{unit_html}</p>'
        f"{hint_html}</article>"
    )


def detail(summary: str, body: str, *, open_: bool = False, cls: str = "") -> str:
    opened = " open" if open_ else ""
    return (
        f'<details class="detail {esc(cls)}"{opened}>'
        f'<summary>{summary}</summary><div class="detail-body">{body}</div></details>'
    )


def details(items: Iterable[tuple[str, str]], *, cls: str = "") -> str:
    return "".join(detail(summary, body, cls=cls) for summary, body in items)


def table(
    headers: Sequence[str],
    rows: Iterable[Sequence[str]],
    *,
    cls: str = "",
    caption: str = "",
    head_cls: Sequence[str] | None = None,
    row_classes: Iterable[str] | None = None,
    numeric_cols: Iterable[int] = (),
    row_attributes: Iterable[dict] | None = None,
) -> str:
    numeric = set(numeric_cols)
    head_cells = []
    for index, header in enumerate(headers):
        classes = ["col-num"] if index in numeric else []
        if head_cls and index < len(head_cls) and head_cls[index]:
            classes.append(head_cls[index])
        head_cells.append(f'<th class="{" ".join(classes)}">{header}</th>')
    row_classes = list(row_classes or [])
    row_attributes = list(row_attributes or [])
    body_rows = []
    for index, row in enumerate(rows):
        classes = row_classes[index] if index < len(row_classes) else ""
        extra_attrs = ""
        if index < len(row_attributes) and isinstance(row_attributes[index], dict):
            extra_attrs = "".join(
                f' {esc(key)}="{esc(value)}"' for key, value in row_attributes[index].items() if value is not None
            )
        cells = []
        for column, cell in enumerate(row):
            cell_class = "col-num" if column in numeric else ""
            cells.append(f'<td class="{cell_class}">{cell}</td>')
        body_rows.append(f'<tr class="{esc(classes)}"{extra_attrs}>{"".join(cells)}</tr>')
    caption_html = f"<caption>{caption}</caption>" if caption else ""
    return (
        f'<div class="table-wrap"><table class="data-table {esc(cls)}">{caption_html}'
        f"<thead><tr>{''.join(head_cells)}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>"
    )


def empty(message: str, *, cls: str = "") -> str:
    return f'<p class="empty-state {esc(cls)}">{esc(message)}</p>'


def note(message: str, *, tone: str = "", cls: str = "") -> str:
    tone_class = f" note-{esc(tone)}" if tone else ""
    return f'<p class="note{tone_class} {esc(cls)}">{message}</p>'


def fact_list(items: Iterable[tuple[str, str]], *, cls: str = "") -> str:
    rows = "".join(
        f'<div class="fact"><dt>{esc(label)}</dt><dd>{value}</dd></div>' for label, value in items
    )
    return f'<dl class="fact-list {esc(cls)}">{rows}</dl>'


_METER_GROUP_BY_COLOR = {"fire": "fire", "cold": "cold", "ignite": "ignite", "body": "body"}


def meter(segments: Iterable[dict], *, cls: str = "", show_values: bool = True, meter_id: str = "") -> str:
    """Stacked composition bar. Segments: {key,label,value,share,color,group}.

    ``group``/``color`` tells the dynamic-scenario client which target-side
    multiplier group the segment (and its legend value) belongs to.
    """

    rendered = []
    legend = []
    for segment in segments:
        share = segment.get("share")
        width = f"{max(0.0, min(1.0, float(share))) * 100:.2f}%" if share is not None else "0%"
        label = segment.get("label") or ""
        value = segment.get("value")
        color = segment.get("color") or "body"
        group = segment.get("group") or _METER_GROUP_BY_COLOR.get(color, "body")
        text = units.fmt_compact(value, "damage") if value is not None else units.MISSING
        rendered.append(
            f'<span class="meter-seg meter-{esc(color)}" data-meter-group="{esc(group)}" '
            f'data-meter-value="{value if value is not None else ""}" '
            f'style="width:{width}" title="{esc(label)} {esc(text)}"></span>'
        )
        legend.append(
            f'<span class="meter-key"><i class="meter-dot meter-{esc(color)}"></i>'
            f"{esc(label)} "
            f"<b>{units.num(value, 'damage', sc='group:' + group)}</b> "
            f'<span class="meter-share">{pct(share) if share is not None else "—"}</span></span>'
        )
    value_html = "" if not show_values else f'<div class="meter-legend">{"".join(legend)}</div>'
    id_attr = f' id="{esc(meter_id)}"' if meter_id else ""
    return f'<div class="meter {esc(cls)}" data-meter{id_attr}>{"".join(rendered)}</div>{value_html}'


# --------------------------------------------------------------------------- #
# source references
# --------------------------------------------------------------------------- #

_CATEGORY_TONES = {
    "equipment": "equip",
    "idol": "idol",
    "blessing": "blessing",
    "idol_altar": "altar",
    "passive_node": "passive",
    "skill_node": "skill",
    "base_character": "base",
    "derived": "derived",
    "quest": "quest",
    "unknown": "muted",
}


def source_color(source: dict) -> str:
    return _CATEGORY_TONES.get(source.get("category") or "unknown", "muted")


def source_chip(source: dict, *, text: str | None = None, cls: str = "", role: str = "") -> str:
    """Clickable chip that opens the source sidebar."""

    if not source:
        return empty("来源缺失")
    name = text or source.get("name") or source.get("display_name") or "来源"
    role_html = f'<span class="src-role">{esc(role)}</span>' if role else ""
    return (
        f'<button type="button" class="src-chip src-{esc(source_color(source))} {esc(cls)}" '
        f'data-source-id="{esc(source.get("id"))}" title="{esc(source.get("display_name") or name)}">'
        f'<i class="src-dot"></i><span class="src-name">{esc(name)}</span>{role_html}</button>'
    )


def source_chip_by_id(view: dict, source_id: str, *, text: str | None = None, role: str = "", cls: str = "") -> str:
    source = (view.get("sources") or {}).get(source_id) or {}
    if not source:
        return f'<span class="src-missing">{esc(text or source_id)}</span>'
    return source_chip(source, text=text, role=role, cls=cls)


def source_link(source: dict, *, text: str | None = None, cls: str = "") -> str:
    if not source:
        return empty("来源缺失")
    name = text or source.get("display_name") or source.get("name") or "来源"
    return (
        f'<button type="button" class="link-btn {esc(cls)}" data-source-id="{esc(source.get("id"))}">'
        f'{esc(name)}</button>'
    )


def inline_list(items: Iterable[str], *, sep: str = "、", cls: str = "") -> str:
    values = [item for item in items if item]
    if not values:
        return '<span class="muted">—</span>'
    return f'<span class="{esc(cls)}">{sep.join(values)}</span>'
