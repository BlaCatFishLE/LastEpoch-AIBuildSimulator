"""Shared player-report document shell (HTML-01/04/09/11/12).

Both damage models render through this module: the model adapter supplies the
player *view*, the model-specific result pages and capability flags; this module
owns the shared document skeleton — skip link, page header (brand / short status
/ target summary / 「更多」menu / model controls), shared page tabs, shared page
wrapper, footer, source-detail panel, drawers, embedded result/view JSON, shared
chrome JS, and the report metadata required by the player-report contract
(``ui_contract_version``, ``renderer_fingerprint``, capabilities consumption and
the local-recompute endpoint placeholder injected by the service adapter).

The model-specific result areas are passed as fully rendered page sections, so
only the result content differs per skill; page chrome and interactions stay
shared. BH keeps its existing page modules and ``app.js``; the hit model passes
its result pages through the shared page wrapper and uses the shared
``shell.js`` for document chrome.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from html import escape as _escape
from pathlib import Path
from typing import Any

from . import components as c

UI_CONTRACT_VERSION = "le-player-report-ui-v1"
RECALC_PLACEHOLDER = "__LE_RECALC_ENDPOINT__"
OFFLINE_FOOTER = "预计伤害，不是游戏内实测 DPS。"
OFFLINE_NOTICE_TEXT = "离线打开：本机重算不可用（请通过 le.ps1 serve 的页面重算）。"

#: Always-included shell bootstrap: offline/file detection must not depend on a
#: model-specific script (BH only loads app.js).  Model scripts may read
#: ``body[data-recalc-mode]`` for their own compute guards.
INLINE_SHELL_SCRIPT = r"""
(function () {
  "use strict";
  var fileMode = location.protocol === "file:";
  var bindings = null;
  try {
    var node = document.getElementById("le-recalc-bindings");
    bindings = node ? JSON.parse(node.textContent || "{}") : null;
  } catch (error) { bindings = null; }
  var meta = document.querySelector('meta[name="le-recalc-endpoint"]');
  var metaEndpoint = meta ? (meta.getAttribute("content") || "") : "";
  var endpoint = (bindings && bindings.recompute_endpoint) || metaEndpoint || "";
  var validEndpoint = /^https?:\/\/[^/]+:\d{1,5}\/recompute$/.test(String(endpoint)) &&
    String(endpoint).indexOf("__LE_") !== 0;
  var available = !!(bindings && !bindings.error && validEndpoint &&
    bindings.report_ref && bindings.input_fingerprints);
  document.body.setAttribute("data-recalc-available", available ? "true" : "false");
  document.body.setAttribute("data-recalc-mode", fileMode ? "file" : "serve");
  document.body.setAttribute("data-recalc-source", bindings ? "bindings" : "none");
  var notes = document.querySelectorAll("[data-recalc-offline-note]");
  Array.prototype.forEach.call(notes, function (note) {
    if (available) {
      note.hidden = true;
      note.textContent = "";
      return;
    }
    note.textContent = fileMode || !bindings
      ? "离线打开：本机重算不可用（请通过 le.ps1 serve 的页面重算）。"
      : "本机重算未启用：报告缺少有效的本机服务端点。";
    note.hidden = false;
  });
})();
"""

_ASSETS_DIR = Path(__file__).with_name("assets")


def asset(name: str) -> str:
    path = _ASSETS_DIR / name
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def embed_json(payload: Any) -> str:
    """Serialize JSON for a ``<script type="application/json">`` block."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def renderer_fingerprint() -> str:
    """O-C: delegate to the single authoritative fingerprint implementation.

    The implementation lives in ``le_calc.report_fingerprint`` (kh-engine) and
    covers the shared package plus all assets (including app.js) and the model
    renderers.  No second algorithm is kept here.
    """

    from .. import report_fingerprint

    repo_root = Path(__file__).resolve().parents[5]
    return report_fingerprint.renderer_fingerprint(repo_root)


def _more_menu(number_toggle: bool, export_json: bool) -> str:
    items = []
    if number_toggle:
        items.append(
            '<label class="toggle"><input type="checkbox" data-toggle-numbers> 显示完整数字</label>'
        )
    items.append('<button type="button" class="more-item" data-print>打印 / 导出 PDF</button>')
    if export_json:
        items.append('<button type="button" class="more-item" data-export-json>导出结果 JSON</button>')
    return (
        '<details class="more-menu">'
        '<summary class="btn btn-small more-summary">更多</summary>'
        f'<div class="more-popover">{"".join(items)}</div></details>'
    )


@dataclass
class DocumentSpec:
    """Model adapter input to the shared document renderer."""

    model: str
    title: str
    brand_mark: str
    heading: str
    subtitle: str
    result: dict[str, Any]
    view: dict[str, Any]
    pages: list[dict[str, Any]]
    page_html: dict[str, str]
    first_page: str
    header_summary_html: str = ""
    header_extra_html: str = ""
    header_controls_html: str = ""
    target_button: bool = True
    number_toggle: bool = False
    drawers_html: str = ""
    extra_css: str = ""
    footer_text: str = OFFLINE_FOOTER
    model_scripts: tuple[str, ...] = ()
    inline_scripts: tuple[str, ...] = ()
    capabilities: dict[str, Any] | None = None
    recalc_endpoint: str | None = None
    status_label: str = "预计伤害"
    embed_view_excluded: tuple[str, ...] = ()
    body_attrs_extra: str = ""
    #: service-injected recalc page bindings (case/model/report_ref/fingerprints)
    bindings: dict[str, Any] | None = None


def _header(spec: DocumentSpec) -> str:
    target_button = (
        '<button type="button" class="btn btn-primary btn-small" data-open-drawer="scenario">目标设置</button>'
        if spec.target_button
        else ""
    )
    summary = (
        f'<span class="header-target-summary" aria-label="当前目标">{spec.header_summary_html}</span>'
        if spec.header_summary_html
        else ""
    )
    status_chip = (
        f'<span class="header-result-kind">{_escape(spec.status_label)}</span>'
        if spec.status_label
        else ""
    )
    header_actions = (
        summary
        + status_chip
        + spec.header_extra_html
        + target_button
        + _more_menu(spec.number_toggle, export_json=True)
    )
    return (
        '<header class="app-header">'
        '<div class="header-top">'
        '<div class="brand">'
        f'<span class="brand-mark" aria-hidden="true">{_escape(spec.brand_mark)}</span>'
        f'<div class="brand-text"><h1>{_escape(spec.heading)}</h1>'
        f'<p class="brand-sub">{_escape(spec.subtitle)}</p></div>'
        "</div>"
        f'<div class="header-actions">{header_actions}</div>'
        "</div>"
        f"{spec.header_controls_html}"
        f"{_nav(spec)}"
        "</header>"
    )


def _nav(spec: DocumentSpec) -> str:
    buttons = []
    for index, page in enumerate(spec.pages):
        page_id = str(page.get("id"))
        active = " is-active" if index == 0 else ""
        selected = "true" if index == 0 else "false"
        buttons.append(
            f'<button type="button" class="page-tab{active}" id="tab-{c.esc(page_id)}" '
            f'role="tab" aria-selected="{selected}" aria-controls="page-{c.esc(page_id)}" '
            f'data-page-btn="{c.esc(page_id)}">'
            f'<span class="page-tab-label">{c.esc(page.get("label") or page_id)}</span></button>'
        )
    return f'<nav class="page-tabs" role="tablist" aria-label="报告页面">{"".join(buttons)}</nav>'


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


def _provided_fingerprint(spec: DocumentSpec) -> str:
    """D1: use the service-provided single fingerprint when available.

    The renderer must not compute a second, divergent renderer fingerprint: the
    report-capabilities artifact / bindings payload is the single source.  The
    local calculation is only an offline fallback for hand-rendered pages.
    """

    bindings = spec.bindings or {}
    value = bindings.get("renderer_fingerprint")
    if isinstance(value, str) and value:
        return value
    capabilities = spec.capabilities or {}
    for candidate in (
        capabilities.get("renderer_fingerprint") if isinstance(capabilities, dict) else None,
        ((capabilities.get("recalculation") or {}) if isinstance(capabilities, dict) else {}).get(
            "renderer_fingerprint"
        ),
    ):
        if isinstance(candidate, str) and candidate:
            return candidate
    return renderer_fingerprint()


def _metadata_head(spec: DocumentSpec) -> str:
    endpoint = spec.recalc_endpoint or RECALC_PLACEHOLDER
    capabilities = spec.capabilities or {}
    return (
        f'<meta name="le-ui-contract-version" content="{_escape(UI_CONTRACT_VERSION)}">\n'
        f'<meta name="le-model" content="{_escape(spec.model)}">\n'
        f'<meta name="le-renderer-fingerprint" content="{_escape(_provided_fingerprint(spec))}">\n'
        f'<meta name="le-recalc-endpoint" content="{_escape(endpoint)}">\n'
        f'<script id="report-capabilities" type="application/json">{embed_json(capabilities)}</script>\n'
    )


def render_document(spec: DocumentSpec) -> str:
    """Render one complete offline single-file player report."""

    pages_html = "".join(spec.page_html.get(str(page.get("id")), "") for page in spec.pages)
    pages_html = pages_html.replace('<section class="page" ', '<section class="page is-active" ', 1)
    body_attrs = (
        f'data-model="{_escape(spec.model)}" data-page="{_escape(spec.first_page)}"'
        + spec.body_attrs_extra
    )
    embedded_view = {
        key: value for key, value in spec.view.items() if key not in spec.embed_view_excluded
    }
    scripts = [asset(name) for name in spec.model_scripts] + list(spec.inline_scripts)
    scripts = [script for script in scripts if script]
    script_html = "".join(f"<script>\n{script}\n</script>\n" for script in scripts)
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_escape(spec.title)}</title>\n"
        '<meta name="color-scheme" content="light">\n'
        + _metadata_head(spec)
        + f"<style>\n{asset('style.css')}\n{spec.extra_css}\n</style>\n</head>\n"
        f'<body {body_attrs}>\n'
        f'<a class="skip-link" href="#page-{c.esc(spec.first_page)}">跳到主要内容</a>\n'
        f"{_header(spec)}\n"
        '<p class="section-note recalc-offline-note" id="recalc-offline-note" '
        'data-recalc-offline-note hidden>'
        + OFFLINE_NOTICE_TEXT
        + "</p>\n"
        f'<main class="app-main" id="app-main">\n{pages_html}</main>\n'
        f'<footer class="app-footer"><span>{_escape(spec.footer_text)}</span></footer>\n'
        f"{_source_panel()}\n{spec.drawers_html}\n"
        f'<script id="result-data" type="application/json">{embed_json(spec.result)}</script>\n'
        f'<script id="view-data" type="application/json">{embed_json(embedded_view)}</script>\n'
        + (
            f'<script id="le-recalc-bindings" type="application/json">'
            f"{embed_json(spec.bindings)}</script>\n"
            if spec.bindings
            else ""
        )
        + f"<script>\n{INLINE_SHELL_SCRIPT}\n</script>\n"
        f'<script>\n{asset("recalc_contract.js")}\n</script>\n'
        f'<script>\n{asset("report_actions.js")}\n</script>\n'
        f"{script_html}"
        "</body>\n</html>\n"
    )


__all__ = [
    "DocumentSpec",
    "OFFLINE_FOOTER",
    "INLINE_SHELL_SCRIPT",
    "OFFLINE_NOTICE_TEXT",
    "RECALC_PLACEHOLDER",
    "UI_CONTRACT_VERSION",
    "asset",
    "embed_json",
    "render_document",
    "renderer_fingerprint",
]
