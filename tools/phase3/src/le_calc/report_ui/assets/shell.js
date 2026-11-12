/* Shared player-report chrome (P3R): page tabs, 「更多」menu, print/export,
   source-detail panel, print mode and the offline local-recompute notice.

   Model adapters may add their own interaction script for result-specific
   controls (Buff 设置 / 本机重算); this file only drives the shared shell. */
(function () {
  "use strict";

  function $$(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }

  function currentResult() {
    var node = document.getElementById("result-data");
    if (!node) { return null; }
    try { return JSON.parse(node.textContent || "{}"); } catch (error) { return null; }
  }

  /* ---- page tabs ------------------------------------------------------ */
  var tabs = $$("[data-page-btn]");
  var panels = $$("[data-page-panel]");
  function showPage(pageId) {
    tabs.forEach(function (tab) {
      var active = tab.getAttribute("data-page-btn") === pageId;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    panels.forEach(function (panel) {
      panel.classList.toggle("is-active", panel.getAttribute("data-page-panel") === pageId);
    });
    document.body.setAttribute("data-page", pageId || "");
    var heading = document.getElementById("page-" + pageId);
    if (heading && typeof heading.focus === "function") {
      heading.setAttribute("tabindex", "-1");
    }
  }
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      showPage(tab.getAttribute("data-page-btn"));
    });
  });
  var initial = (location.hash || "").replace(/^#page-/, "");
  if (initial && document.getElementById("page-" + initial)) {
    showPage(initial);
  } else if (tabs.length) {
    showPage(tabs[0].getAttribute("data-page-btn"));
  }

  /* ---- 「更多」menu / print / export ---------------------------------- */
  var printButton = document.querySelector("[data-print]");
  if (printButton) {
    printButton.addEventListener("click", function () { window.print(); });
  }
  var exportButton = document.querySelector("[data-export-json]");
  if (exportButton) {
    exportButton.addEventListener("click", function () {
      var payload = currentResult();
      if (!payload) { return; }
      var model = document.body.getAttribute("data-model") || "report";
      var blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      var link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = model + "-result.json";
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(link.href);
    });
  }
  var numberToggle = document.querySelector("[data-toggle-numbers]");
  if (numberToggle) {
    numberToggle.addEventListener("change", function () {
      document.body.classList.toggle("show-full-numbers", numberToggle.checked);
    });
  }

  /* ---- source-detail panel (shared shell) ----------------------------- */
  var panel = document.getElementById("source-panel");
  var backdrop = document.getElementById("source-backdrop");
  function closeSource() {
    if (!panel) { return; }
    panel.classList.remove("is-open");
    panel.setAttribute("aria-hidden", "true");
    if (backdrop) { backdrop.hidden = true; }
  }
  function openSource(trigger) {
    if (!panel) { return; }
    var body = document.getElementById("source-panel-body");
    var title = trigger.getAttribute("data-source-title") || "来源详情";
    var detail = trigger.getAttribute("data-source-detail") || "";
    var heading = document.getElementById("source-panel-title");
    if (heading) { heading.textContent = title; }
    if (body) {
      body.innerHTML = "";
      var name = document.createElement("p");
      name.className = "source-panel-name";
      name.textContent = title;
      body.appendChild(name);
      if (detail) {
        var rows = detail.split("|");
        var list = document.createElement("dl");
        list.className = "fact-list";
        rows.forEach(function (row) {
          var parts = row.split(":");
          if (parts.length < 2) { return; }
          var dt = document.createElement("dt");
          dt.textContent = parts[0];
          var dd = document.createElement("dd");
          dd.textContent = parts.slice(1).join(":");
          list.appendChild(dt);
          list.appendChild(dd);
        });
        body.appendChild(list);
      }
    }
    panel.classList.add("is-open");
    panel.setAttribute("aria-hidden", "false");
    if (backdrop) { backdrop.hidden = false; }
  }
  $$("[data-source-open]").forEach(function (trigger) {
    trigger.addEventListener("click", function (event) {
      event.preventDefault();
      openSource(trigger);
    });
  });
  $$("[data-close-source]").forEach(function (button) {
    button.addEventListener("click", closeSource);
  });
  if (backdrop) { backdrop.addEventListener("click", closeSource); }
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") { closeSource(); }
  });

  /* ---- local-recompute availability notice (R6-b) -------------------- */
  // Availability follows the service bindings/endpoint, not the page protocol:
  // a file:// page with valid bindings may explicitly call the loopback service.
  var fileMode = location.protocol === "file:";
  var bindings = null;
  try {
    var bindingsNode = document.getElementById("le-recalc-bindings");
    bindings = bindingsNode ? JSON.parse(bindingsNode.textContent || "{}") : null;
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
  $$("[data-recalc-offline-note]").forEach(function (note) {
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
