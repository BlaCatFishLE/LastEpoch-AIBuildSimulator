/* ==========================================================================
   Black Hole 伤害分析 · 离线单文件报告交互
   - 页签 / 模式 / 策略 / 窗口切换
   - 来源详情侧栏、场景与报告信息抽屉
   - 来源筛选（来源与校验页）
   - 时间轴四条轨道：共享游标、分箱 DPS、层数阶梯、状态轨道
   所有业务数字来自内嵌 JSON；这里只做展示、插值与坐标计算，不重新实现伤害规则。
   ========================================================================== */

(function () {
  "use strict";

  var resultEl = document.getElementById("result-data");
  var viewEl = document.getElementById("view-data");
  if (!resultEl || !viewEl) return;

  var RESULT;
  var VIEW;
  try {
    RESULT = JSON.parse(resultEl.textContent);
    VIEW = JSON.parse(viewEl.textContent);
  } catch (err) {
    document.body.insertAdjacentHTML(
      "afterbegin",
      '<p class="status-banner">内嵌数据解析失败：' + String(err) + "</p>"
    );
    return;
  }

  var SVG_NS = "http://www.w3.org/2000/svg";
  var state = {
    mode: "single",
    policy: null,
    window: 30,
    page: "overview",
    cursor: null,
    fullNumbers: false,
    detailEvents: false
  };

  /* ---------------------------------------------------------------------- */
  /* helpers                                                                */
  /* ---------------------------------------------------------------------- */

  function $(selector, root) {
    return (root || document).querySelector(selector);
  }

  function $$(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }

  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function num(value) {
    if (value === null || value === undefined || value === "") return null;
    var n = Number(value);
    return isFinite(n) ? n : null;
  }

  function trimFixed(text) {
    if (text.indexOf(".") === -1) return text;
    return text.replace(/0+$/, "").replace(/\.$/, "");
  }

  function groupNum(value, digits) {
    if (!isFinite(value)) return "—";
    var text = digits > 0 ? value.toFixed(digits) : String(Math.round(value));
    if (digits > 0) text = trimFixed(text);
    var parts = text.split(".");
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return parts.length > 1 ? parts[0] + "." + parts[1] : parts[0];
  }

  function compact(value, kind, full) {
    var v = num(value);
    if (v === null) return "—";
    switch (kind) {
      case "damage":
      case "dps":
        if (full) return groupNum(v, 2);
        if (Math.abs(v) >= 1e8) return groupNum(v / 1e8, 2) + " 亿";
        if (Math.abs(v) >= 1e4) return groupNum(v / 1e4, 2) + " 万";
        return groupNum(v, 2);
      case "fraction":
        return groupNum(v * 100, 2) + "%";
      case "pp":
        return groupNum(v, 2) + "%";
      case "factor":
        return "×" + groupNum(v, 4);
      case "seconds":
        return trimFixed(v.toFixed(2));
      case "stacks":
      case "stacks_rate":
        return trimFixed(v.toFixed(3));
      case "count":
      case "points":
      case "integer":
        return groupNum(v, 0);
      default:
        return groupNum(v, 2);
    }
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function findLastIndex(times, t) {
    var lo = 0;
    var hi = times.length - 1;
    var result = -1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (times[mid] <= t + 1e-9) {
        result = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    return result;
  }

  function interp(xs, ys, t) {
    if (!xs || !xs.length) return 0;
    if (t <= xs[0]) return ys[0];
    if (t >= xs[xs.length - 1]) return ys[ys.length - 1];
    var index = findLastIndex(xs, t);
    if (index < 0) return ys[0];
    if (index >= xs.length - 1) return ys[ys.length - 1];
    var x0 = xs[index];
    var x1 = xs[index + 1];
    if (x1 === x0) return ys[index + 1];
    var ratio = (t - x0) / (x1 - x0);
    return ys[index] + (ys[index + 1] - ys[index]) * ratio;
  }

  function stepAt(xs, ys, t) {
    if (!xs || !xs.length) return 0;
    var index = findLastIndex(xs, t);
    return index >= 0 ? ys[index] : 0;
  }

  /* ---------------------------------------------------------------------- */
  /* page tabs                                                              */
  /* ---------------------------------------------------------------------- */

  function setPage(pageId, options) {
    state.page = pageId;
    document.body.dataset.page = pageId;
    $$("[data-page-btn]").forEach(function (btn) {
      var active = btn.dataset.pageBtn === pageId;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    $$("[data-page-panel]").forEach(function (panel) {
      panel.classList.toggle("is-active", panel.dataset.pagePanel === pageId);
    });
    if (history.replaceState) {
      history.replaceState(null, "", "#" + pageId);
    }
    if (pageId === "timeline") {
      window.requestAnimationFrame(function () {
        renderCharts();
      });
    }
    if (!options || !options.keepScroll) {
      var main = $("#app-main");
      if (main && main.getBoundingClientRect().top < 0) {
        window.scrollTo({ top: 0, behavior: "smooth" });
      }
    }
  }

  function initPageTabs() {
    $$("[data-page-btn]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setPage(btn.dataset.pageBtn);
      });
      btn.addEventListener("keydown", function (event) {
        var buttons = $$("[data-page-btn]");
        var index = buttons.indexOf(btn);
        var next = null;
        if (event.key === "ArrowRight") next = buttons[(index + 1) % buttons.length];
        if (event.key === "ArrowLeft") next = buttons[(index - 1 + buttons.length) % buttons.length];
        if (next) {
          event.preventDefault();
          next.focus();
          setPage(next.dataset.pageBtn);
        }
      });
    });
    $$("[data-goto-page]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setPage(btn.dataset.gotoPage);
      });
    });
  }

  /* ---------------------------------------------------------------------- */
  /* mode / policy / window                                                 */
  /* ---------------------------------------------------------------------- */

  var loops = (VIEW.context && VIEW.context.loops) || [];

  function activeLoop() {
    for (var i = 0; i < loops.length; i += 1) {
      if (loops[i].id === state.policy) return loops[i];
    }
    return loops[0] || null;
  }

  function updateModePanels() {
    document.body.dataset.mode = state.mode;
    $$("[data-mode-panel]").forEach(function (panel) {
      panel.hidden = panel.dataset.modePanel !== state.mode;
    });
    $$("[data-mode-content]").forEach(function (panel) {
      panel.hidden = panel.dataset.modeContent !== state.mode;
    });
    $$("[data-mode-btn]").forEach(function (btn) {
      var active = btn.dataset.modeBtn === state.mode;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    $$("[data-policy-btn]").forEach(function (btn) {
      btn.classList.toggle("is-active", btn.dataset.policyBtn === state.policy);
    });
    $$("[data-window-btn]").forEach(function (btn) {
      btn.classList.toggle("is-active", Number(btn.dataset.windowBtn) === state.window);
    });
    $$("[data-loop-kpi]").forEach(function (block) {
      var active = block.dataset.policy === state.policy && Number(block.dataset.window) === state.window;
      block.classList.toggle("is-active", active);
    });
    $$("[data-window-label]").forEach(function (node) {
      var loop = activeLoop();
      node.textContent =
        state.mode === "loop" ? state.window + " 秒窗口" : "单次释放全程";
      if (state.mode === "loop" && loop) {
        node.setAttribute("title", loop.label + " · " + state.window + " 秒");
      } else {
        node.removeAttribute("title");
      }
    });
    var precision = $("[data-precision-note]");
    if (precision) {
      if (state.mode === "loop") {
        var loop = activeLoop();
        precision.textContent =
          (loop && loop.precision_note) ||
          "CD 循环曲线来自引擎采样；窗口合计以引擎汇总为准。";
      } else {
        var single = (VIEW.timeline && VIEW.timeline.single) || {};
        precision.textContent = single.precision_note || "单次释放由引擎结果精确推导。";
      }
    }
  }

  function setMode(mode) {
    state.mode = mode;
    if (mode === "loop" && !state.policy) {
      state.policy = loops.length ? loops[0].id : null;
    }
    updateModePanels();
    if (state.page === "timeline") renderCharts();
  }

  function setPolicy(policy) {
    state.policy = policy;
    updateModePanels();
    if (state.page === "timeline") renderCharts();
  }

  function setWindow(seconds) {
    state.window = Number(seconds);
    updateModePanels();
    if (state.page === "timeline") renderCharts();
  }

  function initModeControls() {
    state.policy = loops.length ? loops[0].id : null;
    var defaultWindow = (VIEW.timeline && VIEW.timeline.window_default) || 30;
    state.window = Number(defaultWindow);
    $$("[data-mode-btn]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setMode(btn.dataset.modeBtn);
      });
    });
    $$("[data-policy-btn]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setMode("loop");
        setPolicy(btn.dataset.policyBtn);
      });
    });
    $$("[data-window-btn]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setMode("loop");
        setWindow(Number(btn.dataset.windowBtn));
      });
    });
    updateModePanels();
  }

  /* ---------------------------------------------------------------------- */
  /* number display toggle                                                  */
  /* ---------------------------------------------------------------------- */

  function applyNumberMode() {
    $$(".num[data-value]").forEach(function (node) {
      var text = state.fullNumbers ? node.getAttribute("data-full") : node.getAttribute("data-compact");
      if (text !== null) node.textContent = text;
    });
    $$("[data-toggle-numbers]").forEach(function (toggle) {
      toggle.checked = state.fullNumbers;
    });
  }

  function initNumberToggle() {
    $$("[data-toggle-numbers]").forEach(function (toggle) {
      toggle.addEventListener("change", function () {
        state.fullNumbers = !!toggle.checked;
        applyNumberMode();
        if (state.page === "timeline") renderCharts();
      });
    });
  }

  /* ---------------------------------------------------------------------- */
  /* drawers                                                                */
  /* ---------------------------------------------------------------------- */

  var lastFocused = null;

  function openDrawer(id) {
    var drawer = document.getElementById("drawer-" + id);
    if (!drawer) return;
    drawer.hidden = false;
    document.body.classList.add("has-overlay");
    lastFocused = document.activeElement;
    var target = $(".icon-btn, [data-close-drawer]", drawer);
    if (target) target.focus();
  }

  function closeDrawers() {
    $$(".drawer").forEach(function (drawer) {
      drawer.hidden = true;
    });
    if (!document.querySelector(".source-panel.is-open")) {
      document.body.classList.remove("has-overlay");
    }
    if (lastFocused && typeof lastFocused.focus === "function") lastFocused.focus();
    lastFocused = null;
  }

  function initDrawers() {
    $$("[data-open-drawer]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        openDrawer(btn.dataset.openDrawer);
      });
    });
    $$("[data-close-drawer]").forEach(function (btn) {
      btn.addEventListener("click", closeDrawers);
    });
  }

  /* ---------------------------------------------------------------------- */
  /* source sidebar                                                         */
  /* ---------------------------------------------------------------------- */

  var sourcePanel = $("#source-panel");
  var sourceBackdrop = $("#source-backdrop");
  var sourceBody = $("#source-panel-body");
  var sourceTrigger = null;

  var CATEGORY_TONE = {
    equipment: "equip",
    idol: "idol",
    blessing: "blessing",
    idol_altar: "altar",
    passive_node: "passive",
    skill_node: "skill",
    base_character: "base",
    derived: "derived",
    quest: "quest"
  };

  var STATUS_TONE = {
    confirmed: "ok",
    derived: "info",
    assumed: "warn",
    unresolved: "bad"
  };

  var CONDITION_TEXT = {
    always: "恒定生效",
    satisfied: "条件已满足",
    not_satisfied: "条件未满足",
    unknown: "条件待确认",
    unsupported: "未建模"
  };

  function sourceDetailHtml(source) {
    // Player-facing drawer only: name / slot / effect / participation / condition.
    // Engineering fields (status, evidence, raw text, reason, imported values)
    // stay in the embedded report JSON and are not rendered as visible text.
    var labels = [];
    function addLabel(value) {
      var text = String(value || "").trim();
      if (text && labels.indexOf(text) === -1) labels.push(text);
    }
    addLabel(source.category_label);
    if (source.channel_label !== source.category_label) addLabel(source.channel_label);
    if (source.detail_line !== source.category_label && source.detail_line !== source.channel_label) {
      addLabel(source.detail_line);
    }
    var chips = labels
      .map(function (label) { return '<span class="chip">' + esc(label) + "</span>"; })
      .join("");

    var applies = source.applies || [];
    var applyHtml = applies.length
      ? '<ul class="source-detail-list">' +
        applies
          .map(function (item) {
            return (
              "<li><b>" +
              esc(item.label || item.scope) +
              "</b>" +
              (item.detail ? ' <span class="muted">' + esc(item.detail) + "</span>" : "") +
              "</li>"
            );
          })
          .join("") +
        "</ul>"
      : '<p class="muted small">本次伤害计算没有使用该来源。</p>';

    var conditionLabel = "";
    if (source.condition_label && source.condition_status && source.condition_status !== "always") {
      conditionLabel = String(source.condition_label).trim();
    }

    return (
      '<div class="source-detail-category">' +
      chips +
      "</div>" +
      '<h3 class="source-detail-title">' +
      esc(source.name || "来源") +
      "</h3>" +
      (source.effect_label
        ? '<p class="source-detail-effect">' + esc(source.effect_label) + "</p>"
        : "") +
      (conditionLabel
        ? '<p class="source-detail-condition small muted">适用条件：' + esc(conditionLabel) + "</p>"
        : "") +
      '<section class="source-detail-section"><h3>本次参与</h3>' +
      applyHtml +
      "</section>"
    );
  }

  function openSource(sourceId) {
    var source = (VIEW.sources || {})[sourceId];
    if (!source || !sourcePanel || !sourceBody) return;
    sourceBody.innerHTML = sourceDetailHtml(source);
    if (sourcePanel.dataset) {
      sourcePanel.dataset.sourceId = String(source.id || sourceId || "");
      sourcePanel.dataset.sourceRawText = String(source.raw_text || "");
      sourcePanel.dataset.sourceStatus = String(source.status || "");
      sourcePanel.dataset.sourceCondition = String(source.condition_status || "");
    }
    sourcePanel.classList.add("is-open");
    sourcePanel.setAttribute("aria-hidden", "false");
    if (sourceBackdrop) sourceBackdrop.hidden = false;
    document.body.classList.add("has-overlay");
    sourceTrigger = document.activeElement;
    var closeBtn = $("[data-close-source]", sourcePanel);
    if (closeBtn) closeBtn.focus();
    var panelTitle = $("#source-panel-title");
    if (panelTitle) panelTitle.textContent = "来源详情";
    applyNumberMode();
  }

  function closeSource() {
    if (!sourcePanel) return;
    sourcePanel.classList.remove("is-open");
    sourcePanel.setAttribute("aria-hidden", "true");
    if (sourceBackdrop) sourceBackdrop.hidden = true;
    if (!document.querySelector(".drawer:not([hidden])")) {
      document.body.classList.remove("has-overlay");
    }
    if (sourceTrigger && typeof sourceTrigger.focus === "function") sourceTrigger.focus();
    sourceTrigger = null;
  }

  function closeAllOverlays() {
    closeSource();
    closeDrawers();
  }

  function initSourcePanel() {
    document.addEventListener("click", function (event) {
      var target = event.target.closest("[data-source-id]");
      if (target) {
        event.preventDefault();
        openSource(target.dataset.sourceId);
      }
    });
    $$("[data-close-source]").forEach(function (btn) {
      btn.addEventListener("click", closeSource);
    });
    if (sourceBackdrop) sourceBackdrop.addEventListener("click", closeSource);
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closeAllOverlays();
    });
  }

  /* ---------------------------------------------------------------------- */
  /* generic tabs and formula steps                                         */
  /* ---------------------------------------------------------------------- */

  function activateGroup(group) {
    var buttons = $$("[data-tab-target]", group);
    if (!buttons.length) return;
    var active = buttons.filter(function (btn) {
      return btn.classList.contains("is-active");
    })[0] || buttons[0];
    var scope = group.closest(".page") || document;
    var targetId = active.dataset.tabTarget;
    $$(".tab-panel[data-tab-panel]", scope).forEach(function (panel) {
      panel.classList.toggle("is-active", panel.dataset.tabPanel === targetId);
    });
    buttons.forEach(function (btn) {
      btn.classList.toggle("is-active", btn === active);
      btn.setAttribute("aria-selected", btn === active ? "true" : "false");
    });
  }

  function initTabs() {
    $$(".tabs").forEach(activateGroup);
    $$("[data-tab-target]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var group = btn.closest(".tabs");
        if (!group) return;
        $$("[data-tab-target]", group).forEach(function (other) {
          other.classList.toggle("is-active", other === btn);
          other.setAttribute("aria-selected", other === btn ? "true" : "false");
        });
        var scope = group.closest(".page") || document;
        $$(".tab-panel[data-tab-panel]", scope).forEach(function (panel) {
          panel.classList.toggle("is-active", panel.dataset.tabPanel === btn.dataset.tabTarget);
        });
      });
    });
  }

  function initFormulaSteps() {
    $$("[data-step-target]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var chain = btn.closest(".formula-chain");
        if (chain) {
          $$("[data-step-target]", chain).forEach(function (other) {
            other.classList.toggle("is-active", other === btn);
          });
        }
        var scope = btn.closest(".tab-panel") || btn.closest(".page") || document;
        $$("[data-step-panel]", scope).forEach(function (panel) {
          panel.classList.toggle("is-open", panel.dataset.stepPanel === btn.dataset.stepTarget);
        });
      });
    });
    $$(".step-group").forEach(function (group) {
      var buttons = $$("[data-step-target]", group);
      if (!buttons.length) return;
      var active = buttons.filter(function (btn) {
        return btn.classList.contains("is-active");
      })[0] || buttons[0];
      active.classList.add("is-active");
      var scope = group.closest(".tab-panel") || group.closest(".page") || document;
      $$("[data-step-panel]", scope).forEach(function (panel) {
        panel.classList.toggle("is-open", panel.dataset.stepPanel === active.dataset.stepTarget);
      });
    });
  }

  /* ---------------------------------------------------------------------- */
  /* validation source filters                                              */
  /* ---------------------------------------------------------------------- */

  function initSourceFilters() {
    var rows = $$("[data-source-row]");
    if (!rows.length) return;
    var search = $("[data-source-search]");
    var category = $("[data-source-category]");
    var included = $("[data-source-included]");

    function normalizeFilter(value) {
      var text = value === null || value === undefined ? "" : String(value).trim();
      if (!text || text === "all" || text === "any") return "";
      return text;
    }

    function apply() {
      var query = search ? search.value.trim().toLowerCase() : "";
      var categoryValue = category ? normalizeFilter(category.value) : "";
      var includedValue = included ? normalizeFilter(included.value) : "";
      var visible = 0;
      rows.forEach(function (row) {
        var haystack = row.dataset.search || "";
        var ok = true;
        if (query && haystack.indexOf(query) === -1) ok = false;
        if (ok && categoryValue && row.dataset.category !== categoryValue) ok = false;
        if (ok && includedValue && row.dataset.included !== includedValue) ok = false;
        row.hidden = !ok;
        if (ok) visible += 1;
      });
      $$("[data-source-group]").forEach(function (group) {
        var groupRows = $$("[data-source-row]", group);
        var shown = groupRows.filter(function (row) {
          return !row.hidden;
        });
        group.hidden = shown.length === 0;
        if (query || categoryValue || includedValue) {
          group.open = shown.length > 0;
        }
      });
      var count = $("[data-filter-count]");
      if (count) count.textContent = String(visible);
      var total = $("[data-source-count]");
      if (total) total.textContent = "共 " + visible + " 条";
    }

    if (search) search.addEventListener("input", apply);
    if (category) category.addEventListener("change", apply);
    if (included) included.addEventListener("change", apply);
    $$("[data-expand-all='source']").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var groups = $$("[data-source-group]");
        var anyClosed = groups.some(function (group) {
          return !group.open && !group.hidden;
        });
        groups.forEach(function (group) {
          group.open = anyClosed;
        });
        btn.textContent = anyClosed ? "收起分组" : "展开全部";
      });
    });
  }

  /* ---------------------------------------------------------------------- */
  /* export / print                                                         */
  /* ---------------------------------------------------------------------- */

  function downloadJson() {
    try {
      var blob = new Blob([resultEl.textContent], { type: "application/json" });
      var url = URL.createObjectURL(blob);
      var link = document.createElement("a");
      link.href = url;
      link.download = "black-hole-result.json";
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(function () {
        URL.revokeObjectURL(url);
      }, 1000);
    } catch (err) {
      window.alert("导出失败：" + String(err));
    }
  }

  function initGlobalButtons() {
    $$("[data-export-json]").forEach(function (btn) {
      btn.addEventListener("click", downloadJson);
    });
    $$("[data-print]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        window.print();
      });
    });
  }

  /* ---------------------------------------------------------------------- */
  /* timeline data                                                          */
  /* ---------------------------------------------------------------------- */

  function timelineData() {
    var tl = VIEW.timeline || {};
    if (state.mode === "loop") {
      var loopMap = tl.loops || {};
      var key = state.policy && loopMap[state.policy] ? state.policy : Object.keys(loopMap)[0];
      var loop = loopMap[key];
      if (!loop) return null;
      var end = Math.min(Number(state.window), loop.horizon_s || Number(state.window));
      var series = loop.series || { t: [], body: [], ignite: [], total: [], stacks: [], active: [], charges: [] };
      return scenarioScaleTimeline({
        kind: "loop",
        loop: loop,
        end: end,
        curve: {
          t: series.t,
          body: series.body,
          ignite: series.ignite,
          total: series.total
        },
        stacks: { t: series.t, values: series.stacks },
        active: series.active,
        charges: series.charges,
        casts: loop.casts || [],
        milestones: [
          { t: 0, label: "开始观察" },
          { t: end, label: state.window + " 秒窗口" }
        ],
        precision: "sampled",
        bins: sampledBins(series, end, 0.5)
      });
    }
    var single = tl.single || {};
    return scenarioScaleTimeline({
      kind: "single",
      single: single,
      end: single.end_s || 0,
      curve: single.curve || { t: [], body: [], ignite: [], total: [] },
      stacks: single.stacks || { t: [], values: [] },
      active: null,
      charges: null,
      casts: null,
      milestones: single.milestones || [],
      precision: single.precision || "exact-derived",
      bins: single.bins || [],
      peak: single.peak_stacks || 0,
      spawn: single.spawn_s || 0,
      bhEnd: single.bh_end_s || 0
    });
  }

  function scenarioScaleTimeline(data) {
    if (!data || !scenarioState.applied || !scenarioModelCache) return data;
    var model = scenarioModelCache;
    var curve = data.curve || {};
    var times = curve.t || [];
    if (!times.length) return data;
    var body = (curve.body || []).map(function (value) {
      return (Number(value) || 0) * model.bodyRatio;
    });
    var ignite = (curve.ignite || []).map(function (value) {
      return (Number(value) || 0) * model.igniteRatio;
    });
    var total = body.map(function (value, index) {
      return value + (ignite[index] || 0);
    });
    data.curve = { t: times, body: body, ignite: ignite, total: total };
    if (data.kind === "single") {
      data.bins = (data.bins || []).map(function (bin) {
        var bodyValue = (Number(bin.body) || 0) * model.bodyRatio;
        var igniteValue = (Number(bin.ignite) || 0) * model.igniteRatio;
        var totalValue = bodyValue + igniteValue;
        return {
          index: bin.index,
          start: bin.start,
          end: bin.end,
          width: bin.width,
          body: bodyValue,
          ignite: igniteValue,
          total: totalValue,
          dps: totalValue / (bin.width || 1)
        };
      });
    } else {
      data.bins = sampledBins({ t: times, body: body, ignite: ignite }, data.end, 0.5);
    }
    return data;
  }

  function sampledBins(series, end, width) {
    var bins = [];
    var count = Math.max(1, Math.ceil(end / width - 1e-9));
    for (var i = 0; i < count; i += 1) {
      var start = i * width;
      var finish = Math.min((i + 1) * width, end);
      var body0 = interp(series.t, series.body, start);
      var body1 = interp(series.t, series.body, finish);
      var ignite0 = interp(series.t, series.ignite, start);
      var ignite1 = interp(series.t, series.ignite, finish);
      var body = Math.max(0, body1 - body0);
      var ignite = Math.max(0, ignite1 - ignite0);
      bins.push({
        index: i,
        start: start,
        end: finish,
        width: finish - start,
        body: body,
        ignite: ignite,
        total: body + ignite,
        dps: (body + ignite) / (finish - start || 1)
      });
    }
    return bins;
  }

  /* ---------------------------------------------------------------------- */
  /* svg helpers                                                            */
  /* ---------------------------------------------------------------------- */

  function createSvg(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        node.setAttribute(key, attrs[key]);
      });
    }
    return node;
  }

  function niceMax(value) {
    if (!isFinite(value) || value <= 0) return 1;
    var exponent = Math.floor(Math.log(value) / Math.LN10);
    var magnitude = Math.pow(10, exponent);
    var fraction = value / magnitude;
    var nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10;
    return nice * magnitude;
  }

  function tickValues(maxValue, count) {
    var values = [];
    var step = niceMax(maxValue) / count;
    for (var i = 0; i <= count; i += 1) values.push(step * i);
    return values;
  }

  function fmtAxis(value, kind) {
    return compact(value, kind);
  }

  function timeTicks(end) {
    var candidates = [0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120];
    var step = candidates[candidates.length - 1];
    for (var i = 0; i < candidates.length; i += 1) {
      if (end / candidates[i] <= 9) {
        step = candidates[i];
        break;
      }
    }
    var values = [];
    for (var t = 0; t <= end + 1e-9; t += step) values.push(Number(t.toFixed(6)));
    var last = values.length ? values[values.length - 1] : 0;
    if (values.length && last < end - 1e-9 && end - last >= step * 0.55) values.push(end);
    return values;
  }

  function pathFromPoints(points) {
    return points
      .map(function (point, index) {
        return (index === 0 ? "M" : "L") + point[0].toFixed(2) + "," + point[1].toFixed(2);
      })
      .join(" ");
  }

  function stepPathFromPoints(points) {
    var parts = [];
    points.forEach(function (point, index) {
      if (index === 0) {
        parts.push("M" + point[0].toFixed(2) + "," + point[1].toFixed(2));
      } else {
        var previous = points[index - 1];
        parts.push("H" + point[0].toFixed(2));
        parts.push("V" + point[1].toFixed(2));
      }
    });
    return parts.join(" ");
  }

  function stepPolyline(points) {
    var out = [];
    points.forEach(function (point, index) {
      if (index === 0) {
        out.push([point[0], point[1]]);
      } else {
        var previous = points[index - 1];
        out.push([point[0], previous[1]]);
        out.push([point[0], point[1]]);
      }
    });
    return out;
  }

  /* ---------------------------------------------------------------------- */
  /* chart rendering                                                        */
  /* ---------------------------------------------------------------------- */

  var chartGeometry = {};
  var chartStack = $("#chart-stack");
  var tooltip = $("#chart-tooltip");
  var resizeTimer = null;

  var COLORS = {
    fire: "#c2410c",
    ignite: "#a21caf",
    total: "#111827",
    peak: "#b45309",
    cast: "#2563eb",
    state: "#0f766e"
  };

  function chartSize() {
    if (!chartStack) return { width: 900 };
    return { width: Math.max(560, chartStack.clientWidth - 8) };
  }

  function xTicks(svg, geom, end) {
    timeTicks(end).forEach(function (t) {
      var x = geom.x(t);
      svg.appendChild(
        createSvg("line", {
          x1: x,
          x2: x,
          y1: geom.top,
          y2: geom.top + geom.innerH,
          class: "grid-line"
        })
      );
      var label = createSvg("text", {
        x: x,
        y: geom.top + geom.innerH + 14,
        "text-anchor": "middle"
      });
      label.textContent = compact(t, "seconds") + "s";
      svg.appendChild(label);
    });
  }

  function yTicks(svg, geom, maxValue, kind, count) {
    tickValues(maxValue, count || 4).forEach(function (value) {
      var y = geom.y(value);
      svg.appendChild(
        createSvg("line", { x1: geom.left, x2: geom.left + geom.innerW, y1: y, y2: y, class: "grid-line" })
      );
      var label = createSvg("text", { x: geom.left - 8, y: y + 3.5, "text-anchor": "end" });
      label.textContent = fmtAxis(value, kind);
      svg.appendChild(label);
    });
  }

  function buildSvg(track, height) {
    var container = $('.chart[data-track="' + track + '"]');
    if (!container) return null;
    container.innerHTML = "";
    var width = chartSize().width;
    var svg = createSvg("svg", {
      width: width,
      height: height,
      viewBox: "0 0 " + width + " " + height,
      "data-track": track,
      role: "presentation"
    });
    container.appendChild(svg);
    return { svg: svg, width: width, height: height };
  }

  function renderCumulative(data) {
    var built = buildSvg("cumulative", 240);
    if (!built) return;
    var svg = built.svg;
    var left = 64;
    var right = 16;
    var top = 36;
    var bottom = 24;
    var innerW = built.width - left - right;
    var innerH = built.height - top - bottom;
    var end = data.end || 1;
    var curve = data.curve || { t: [], body: [], ignite: [], total: [] };
    var points = [];
    var maxTotal = 0;
    for (var i = 0; i < curve.t.length; i += 1) {
      var t = curve.t[i];
      if (t > end + 1e-9) break;
      points.push({
        t: t,
        body: curve.body[i] || 0,
        ignite: curve.ignite[i] || 0,
        total: curve.total[i] || 0
      });
      maxTotal = Math.max(maxTotal, curve.total[i] || 0);
    }
    if (!points.length) points.push({ t: 0, body: 0, ignite: 0, total: 0 });
    var maxY = niceMax(maxTotal * 1.03 || 1);
    var geom = {
      left: left,
      top: top,
      innerW: innerW,
      innerH: innerH,
      end: end,
      x: function (t) {
        return left + (clamp(t, 0, end) / end) * innerW;
      },
      y: function (value) {
        return top + innerH - clamp(value / maxY, 0, 1) * innerH;
      }
    };
    yTicks(svg, geom, maxY, "damage", 4);
    xTicks(svg, geom, end);

    var baseY = top + innerH;
    var bodyPoints = points.map(function (point) {
      return [geom.x(point.t), geom.y(point.body)];
    });
    var totalPoints = points.map(function (point) {
      return [geom.x(point.t), geom.y(point.body + point.ignite)];
    });

    var bodyStep = stepPolyline(bodyPoints);
    var bodyArea = pathFromPoints(
      [[geom.x(0), baseY]].concat(bodyStep).concat([[geom.x(end), baseY]])
    );
    svg.appendChild(createSvg("path", { d: bodyArea + " Z", class: "series-area series-body" }));

    var igniteBand = totalPoints.concat(bodyStep.slice().reverse());
    svg.appendChild(createSvg("path", { d: pathFromPoints(igniteBand) + " Z", class: "series-area series-ignite" }));

    svg.appendChild(
      createSvg("path", { d: pathFromPoints(totalPoints), class: "series-line series-total" })
    );

    var labelRows = [[], []];
    var milestoneIndex = 0;
    (data.milestones || []).forEach(function (milestone) {
      var t = num(milestone.t);
      if (t === null || t < 0 || t > end) return;
      var x = geom.x(t);
      svg.appendChild(
        createSvg("line", {
          x1: x,
          x2: x,
          y1: top,
          y2: top + innerH,
          class: "milestone-line"
        })
      );
      // two staggered label rows: nearby milestones stay readable without a
      // single row of overlapping text
      var row = milestoneIndex % 2;
      milestoneIndex += 1;
      var collides = labelRows[row].some(function (anchorX) {
        return Math.abs(anchorX - x) < 108;
      });
      if (collides) return;
      labelRows[row].push(x);
      var anchor = x > left + innerW - 90 ? "end" : "start";
      var label = createSvg("text", {
        x: anchor === "end" ? x - 3 : x + 3,
        y: top - 10 - row * 14,
        "text-anchor": anchor,
        class: "milestone-label"
      });
      label.textContent = milestone.label;
      svg.appendChild(label);
    });

    chartGeometry.cumulative = geom;
    chartGeometry.svgCumulative = svg;
    svg.appendChild(createSvg("line", { x1: 0, x2: 0, y1: top, y2: baseY, class: "cursor-line", "data-cursor": "1" }));
  }

  function renderDps(data) {
    var built = buildSvg("dps", 170);
    if (!built) return;
    var svg = built.svg;
    var left = 64;
    var right = 16;
    var top = 18;
    var bottom = 24;
    var innerW = built.width - left - right;
    var innerH = built.height - top - bottom;
    var end = data.end || 1;
    var bins = (data.bins || []).filter(function (bin) {
      return bin.start < end - 1e-9;
    });
    var maxDps = 0;
    bins.forEach(function (bin) {
      maxDps = Math.max(maxDps, bin.total / (bin.width || 1));
    });
    var maxY = niceMax(maxDps * 1.08 || 1);
    var geom = {
      left: left,
      top: top,
      innerW: innerW,
      innerH: innerH,
      end: end,
      x: function (t) {
        return left + (clamp(t, 0, end) / end) * innerW;
      },
      y: function (value) {
        return top + innerH - clamp(value / maxY, 0, 1) * innerH;
      }
    };
    yTicks(svg, geom, maxY, "dps", 3);
    xTicks(svg, geom, end);
    var approximate = data.precision === "sampled";
    bins.forEach(function (bin) {
      var x0 = geom.x(bin.start);
      var x1 = geom.x(bin.end) - (bin.index === bins.length - 1 ? 0 : 1);
      if (x1 <= x0) x1 = x0 + 1;
      var bodyDps = bin.body / (bin.width || 1);
      var igniteDps = bin.ignite / (bin.width || 1);
      var bodyTop = geom.y(bodyDps);
      var totalTop = geom.y(bodyDps + igniteDps);
      svg.appendChild(
        createSvg("rect", {
          x: x0,
          y: totalTop,
          width: Math.max(1, x1 - x0),
          height: Math.max(0, bodyTop - totalTop),
          class: "bar-ignite" + (approximate ? " bar-approx" : "")
        })
      );
      svg.appendChild(
        createSvg("rect", {
          x: x0,
          y: bodyTop,
          width: Math.max(1, x1 - x0),
          height: Math.max(0, geom.y(0) - bodyTop),
          class: "bar-body" + (approximate ? " bar-approx" : "")
        })
      );
    });
    if (approximate) {
      var badge = createSvg("text", {
        x: left + 4,
        y: top + 11,
        "text-anchor": "start",
        class: "sampling-badge"
      });
      badge.textContent = "采样近似（0.5 秒分箱）";
      svg.appendChild(badge);
    }
    chartGeometry.dps = geom;
    svg.appendChild(createSvg("line", { x1: 0, x2: 0, y1: top, y2: top + innerH, class: "cursor-line", "data-cursor": "1" }));
  }

  function renderStacks(data) {
    var built = buildSvg("stacks", 170);
    if (!built) return;
    var svg = built.svg;
    var left = 64;
    var right = 16;
    var top = 18;
    var bottom = 24;
    var innerW = built.width - left - right;
    var innerH = built.height - top - bottom;
    var end = data.end || 1;
    var xs = (data.stacks && data.stacks.t) || [];
    var ys = (data.stacks && data.stacks.values) || [];
    var points = [];
    var maxY = 0;
    for (var i = 0; i < xs.length; i += 1) {
      if (xs[i] > end + 1e-9) break;
      points.push({ t: xs[i], value: ys[i] || 0 });
      maxY = Math.max(maxY, ys[i] || 0);
    }
    if (!points.length) points.push({ t: 0, value: 0 });
    var domainMax = niceMax((maxY || 1) * 1.12);
    var geom = {
      left: left,
      top: top,
      innerW: innerW,
      innerH: innerH,
      end: end,
      x: function (t) {
        return left + (clamp(t, 0, end) / end) * innerW;
      },
      y: function (value) {
        return top + innerH - clamp(value / domainMax, 0, 1) * innerH;
      }
    };
    yTicks(svg, geom, domainMax, "stacks", 3);
    xTicks(svg, geom, end);
    var baseY = top + innerH;
    var stepPoints = [[geom.x(0), geom.y(0)]].concat(
      points.map(function (point) {
        return [geom.x(point.t), geom.y(point.value)];
      })
    );
    var line = stepPathFromPoints(stepPoints);
    svg.appendChild(
      createSvg("path", {
        d: line + " L" + geom.x(end).toFixed(2) + "," + baseY.toFixed(2) + " L" + left + "," + baseY.toFixed(2) + " Z",
        class: "series-stacks-area"
      })
    );
    svg.appendChild(createSvg("path", { d: line, class: "series-line series-stacks" }));

    if (maxY > 0) {
      var peakIndex = 0;
      for (var p = 0; p < points.length; p += 1) {
        if (points[p].value >= points[peakIndex].value) peakIndex = p;
      }
      var peak = points[peakIndex];
      var px = geom.x(peak.t);
      var py = geom.y(peak.value);
      svg.appendChild(createSvg("circle", { cx: px, cy: py, r: 3.2, fill: COLORS.peak }));
      var label = createSvg("text", {
        x: px + (px > left + innerW - 90 ? -6 : 6),
        y: py - 7,
        "text-anchor": px > left + innerW - 90 ? "end" : "start",
        class: "peak-label"
      });
      label.textContent = "峰值 " + compact(peak.value, "stacks") + " 层";
      svg.appendChild(label);
    }
    chartGeometry.stacks = geom;
    svg.appendChild(createSvg("line", { x1: 0, x2: 0, y1: top, y2: baseY, class: "cursor-line", "data-cursor": "1" }));
  }

  function renderState(data) {
    var height = data.kind === "loop" ? 132 : 96;
    var built = buildSvg("state", height);
    if (!built) return;
    var svg = built.svg;
    var left = 64;
    var right = 16;
    var top = 16;
    var innerW = built.width - left - right;
    var innerH = built.height - top - 18;
    var end = data.end || 1;
    var geom = {
      left: left,
      top: top,
      innerW: innerW,
      innerH: innerH,
      end: end,
      x: function (t) {
        return left + (clamp(t, 0, end) / end) * innerW;
      }
    };
    xTicks(svg, geom, end);

    if (data.kind === "single") {
      var spawn = data.spawn || 0;
      var bhEnd = data.bhEnd || 0;
      var phases = [
        { start: 0, end: spawn, label: "施法", cls: "phase-band-cast", text: "施法" },
        { start: spawn, end: bhEnd, label: "黑洞存续", cls: "phase-band-bh", text: "黑洞存续" },
        { start: bhEnd, end: end, label: "仅点燃尾部", cls: "phase-band-tail", text: "仅点燃尾部" }
      ];
      phases.forEach(function (phase) {
        var x0 = geom.x(phase.start);
        var x1 = geom.x(phase.end);
        var width = Math.max(0, x1 - x0);
        if (width <= 0) return;
        svg.appendChild(
          createSvg("rect", {
            x: x0,
            y: top + 8,
            width: width,
            height: innerH - 16,
            class: phase.cls
          })
        );
        if (width > 64) {
          var text = createSvg("text", {
            x: x0 + width / 2,
            y: top + innerH / 2 + 4,
            "text-anchor": "middle",
            class: "phase-label"
          });
          text.textContent = phase.text + " · " + compact(phase.start, "seconds") + "–" + compact(phase.end, "seconds") + "s";
          svg.appendChild(text);
        }
      });
    } else {
      var activeSeries = data.active || [];
      var chargeSeries = data.charges || [];
      var times = (data.curve && data.curve.t) || [];
      var maxActive = 1;
      activeSeries.forEach(function (value) {
        if (value !== null && value > maxActive) maxActive = value;
      });
      var maxCharges = 1;
      chargeSeries.forEach(function (value) {
        if (value !== null && value > maxCharges) maxCharges = value;
      });
      var laneH = (innerH - 10) / 2;
      function lane(series, maxValue, y0, tone, title) {
        var base = y0 + laneH - 4;
        var scale = (laneH - 14) / (maxValue || 1);
        var pts = [];
        for (var i = 0; i < times.length; i += 1) {
          if (times[i] > end + 1e-9) break;
          pts.push([geom.x(times[i]), base - (Number(series[i]) || 0) * scale]);
        }
        if (!pts.length) return;
        svg.appendChild(
          createSvg("line", { x1: left, x2: left + innerW, y1: base, y2: base, class: "axis-line" })
        );
        svg.appendChild(
          createSvg("path", { d: stepPathFromPoints(pts), class: "series-line", style: "stroke:" + tone })
        );
        var label = createSvg("text", { x: left - 8, y: y0 + laneH / 2, "text-anchor": "end" });
        label.textContent = title + " ≤" + maxValue;
        svg.appendChild(label);
      }
      lane(activeSeries, maxActive, top, COLORS.state, "黑洞数");
      lane(chargeSeries, maxCharges, top + laneH + 6, COLORS.cast, "充能");
    }
    chartGeometry.state = geom;
    svg.appendChild(createSvg("line", { x1: 0, x2: 0, y1: top, y2: top + innerH, class: "cursor-line", "data-cursor": "1" }));
  }

  function drawCursor() {
    var geom = chartGeometry.cumulative;
    if (!geom) return;
    var t = state.cursor;
    $$(".chart svg").forEach(function (svg) {
      var line = svg.querySelector('[data-cursor="1"]');
      if (!line) return;
      if (t === null || t === undefined) {
        line.setAttribute("x1", 0);
        line.setAttribute("x2", 0);
        line.setAttribute("opacity", "0");
        return;
      }
      var x = geom.x(clamp(t, 0, geom.end));
      line.setAttribute("x1", x);
      line.setAttribute("x2", x);
      line.setAttribute("opacity", "1");
    });
    $$("[data-set-time]").forEach(function (btn) {
      var time = Number(btn.dataset.setTime);
      btn.classList.toggle("is-active", t !== null && Math.abs(time - t) < 0.005);
    });
  }

  function readoutAt(data, t) {
    if (t === null || t === undefined) {
      return { t: "—", body: "—", ignite: "—", total: "—", dps: "—", stacks: "—", bh: "—", charges: "—" };
    }
    var curve = data.curve || { t: [], body: [], ignite: [], total: [] };
    var body;
    var ignite;
    var total;
    if (data.kind === "single") {
      body = stepAt(curve.t, curve.body, t);
      ignite = interp(curve.t, curve.ignite, t);
      total = body + ignite;
    } else {
      body = interp(curve.t, curve.body, t);
      ignite = interp(curve.t, curve.ignite, t);
      total = body + ignite;
    }
    var bin = null;
    (data.bins || []).forEach(function (item) {
      if (t >= item.start - 1e-9 && t < item.end + 1e-9) bin = item;
    });
    var stacks = stepAt((data.stacks && data.stacks.t) || [], (data.stacks && data.stacks.values) || [], t);
    var bh;
    if (data.kind === "single") {
      bh = t < (data.spawn || 0) ? 0 : t < (data.bhEnd || 0) ? 1 : 0;
    } else {
      var times = (data.curve && data.curve.t) || [];
      bh = stepAt(times, data.active || [], t);
    }
    var charges = data.kind === "loop" ? stepAt((data.curve && data.curve.t) || [], data.charges || [], t) : null;
    return {
      t: compact(t, "seconds") + " 秒",
      body: compact(body, "damage"),
      ignite: compact(ignite, "damage"),
      total: compact(total, "damage"),
      dps: bin ? compact(bin.dps, "dps") : "—",
      stacks: compact(stacks, "stacks") + " 层",
      bh: compact(bh, "count") + " 个",
      charges: charges === null ? "—" : compact(charges, "count") + " 层"
    };
  }

  function updateReadout() {
    var data = timelineData();
    if (!data) return;
    var values = readoutAt(data, state.cursor);
    $$("[data-readout]").forEach(function (node) {
      var key = node.dataset.readout;
      if (Object.prototype.hasOwnProperty.call(values, key)) {
        node.textContent = values[key];
      }
    });
  }

  function setCursor(t, options) {
    var data = timelineData();
    if (!data) return;
    if (t === null) {
      state.cursor = null;
    } else {
      state.cursor = clamp(Number(t), 0, data.end);
    }
    drawCursor();
    updateReadout();
    if (tooltip && !(options && options.keepTooltip)) {
      if (state.cursor === null) tooltip.hidden = true;
    }
  }

  function eventTimes() {
    var data = timelineData();
    if (!data) return [];
    var times = [];
    (data.milestones || []).forEach(function (item) {
      var value = num(item.t);
      if (value !== null) times.push(value);
    });
    if (data.kind === "single") {
      (data.single.key_events || []).forEach(function (item) {
        var value = num(item.t);
        if (value !== null) times.push(value);
      });
    }
    times.sort(function (a, b) {
      return a - b;
    });
    var unique = [];
    times.forEach(function (time) {
      if (!unique.length || Math.abs(time - unique[unique.length - 1]) > 0.001) unique.push(time);
    });
    return unique;
  }

  function stepCursor(direction) {
    var times = eventTimes();
    if (!times.length) return;
    var current = state.cursor;
    var target = null;
    if (current === null) {
      target = times[0];
    } else if (direction > 0) {
      for (var i = 0; i < times.length; i += 1) {
        if (times[i] > current + 0.001) {
          target = times[i];
          break;
        }
      }
      if (target === null) target = times[times.length - 1];
    } else {
      for (var j = times.length - 1; j >= 0; j -= 1) {
        if (times[j] < current - 0.001) {
          target = times[j];
          break;
        }
      }
      if (target === null) target = times[0];
    }
    setCursor(target);
  }

  function bindPointerCursor() {
    if (!chartStack) return;

    function activeChartSvg() {
      return (
        chartGeometry.svgCumulative ||
        $('.chart[data-track="cumulative"] svg') ||
        $(".chart svg")
      );
    }

    function timeFromEvent(event) {
      var svg = activeChartSvg();
      var geom = chartGeometry.cumulative;
      if (!svg || !geom) return null;
      var data = timelineData();
      if (!data) return null;
      var rect = svg.getBoundingClientRect();
      var totalWidth = geom.left + geom.innerW + 16;
      var innerLeft = (geom.left / totalWidth) * rect.width;
      var innerW = (geom.innerW / totalWidth) * rect.width;
      var ratio = (event.clientX - rect.left - innerLeft) / Math.max(1, innerW);
      return clamp(ratio, 0, 1) * data.end;
    }

    chartStack.addEventListener("pointermove", function (event) {
      var data = timelineData();
      var t = timeFromEvent(event);
      if (!data || t === null) return;
      setCursor(t, { keepTooltip: true });
      if (tooltip) {
        var values = readoutAt(data, t);
        tooltip.innerHTML =
          "<b>" +
          esc(values.t) +
          "</b><br>累计总伤害 <b>" +
          esc(values.total) +
          "</b><br>区间 DPS <b>" +
          esc(values.dps) +
          "</b><br>活跃层数 <b>" +
          esc(values.stacks) +
          "</b>";
        tooltip.hidden = false;
        var rect = chartStack.getBoundingClientRect();
        var x = clamp(event.clientX - rect.left + 14, 8, Math.max(8, rect.width - 190));
        var y = clamp(event.clientY - rect.top + 12, 8, Math.max(8, rect.height - 90));
        tooltip.style.left = x + "px";
        tooltip.style.top = y + "px";
      }
    });
    chartStack.addEventListener("pointerleave", function () {
      if (tooltip) tooltip.hidden = true;
    });
    chartStack.addEventListener("keydown", function (event) {
      var data = timelineData();
      if (!data) return;
      if (event.key === "ArrowRight") {
        event.preventDefault();
        stepCursor(1);
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        stepCursor(-1);
      } else if (event.key === "Home") {
        event.preventDefault();
        setCursor(0);
      } else if (event.key === "End") {
        event.preventDefault();
        setCursor(data.end);
      } else if (event.key === "Escape") {
        setCursor(null);
      }
    });
  }

  function renderCharts() {
    if (!chartStack) return;
    var data = timelineData();
    if (!data) return;
    if (state.cursor !== null && state.cursor > data.end) state.cursor = data.end;
    renderCumulative(data);
    renderDps(data);
    renderStacks(data);
    renderState(data);
    drawCursor();
    updateReadout();
  }

  function initTimeline() {
    if (!chartStack) return;
    bindPointerCursor();
    $$("[data-set-time]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setPage("timeline", { keepScroll: true });
        setCursor(Number(btn.dataset.setTime));
      });
    });
    $$("[data-cursor-step]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        stepCursor(Number(btn.dataset.cursorStep));
      });
    });
    $$("[data-cursor-reset]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setCursor(null);
      });
    });
    $$("[data-toggle-detail-events]").forEach(function (toggle) {
      toggle.addEventListener("change", function () {
        state.detailEvents = !!toggle.checked;
        var list = $("#detail-event-list");
        if (list) list.hidden = !state.detailEvents;
      });
    });
    $$("[data-goto-time]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setMode("single");
        setPage("timeline", { keepScroll: true });
        setCursor(Number(btn.dataset.gotoTime));
      });
    });
    var observer = null;
    if (window.ResizeObserver) {
      observer = new ResizeObserver(function () {
        if (resizeTimer) window.cancelAnimationFrame(resizeTimer);
        resizeTimer = window.requestAnimationFrame(function () {
          if (state.page === "timeline") renderCharts();
        });
      });
      observer.observe(chartStack);
    } else {
      window.addEventListener("resize", function () {
        if (state.page === "timeline") renderCharts();
      });
    }
  }

  /* ---------------------------------------------------------------------- */
  /* dynamic target scenario (monster level / target fire resistance)       */
  /* ---------------------------------------------------------------------- */

  var SCENARIO_CONTRACT = VIEW.scenario || {};
  var SCENARIO_BASE = SCENARIO_CONTRACT.baseline || {};
  var scenarioState = {
    level: Number(SCENARIO_BASE.level || 100),
    fireRes: Number(SCENARIO_BASE.fire_resistance || 0),
    applied: false
  };
  var scenarioModelCache = null;
  var scenarioSnapshot = null;
  var scenarioMeterWidths = null;
  var scenarioShareSnapshot = null;

  function scenarioLevelRows() {
    return (VIEW.sensitivity && VIEW.sensitivity.rows) || [];
  }

  function scenarioLevelRow(level) {
    var rows = scenarioLevelRows();
    for (var i = 0; i < rows.length; i += 1) {
      if (Number(rows[i].level) === Number(level)) return rows[i];
    }
    return null;
  }

  function scenarioIsBaseline() {
    return (
      Math.abs(Number(scenarioState.level) - Number(SCENARIO_BASE.level)) < 1e-9 &&
      Math.abs(Number(scenarioState.fireRes) - Number(SCENARIO_BASE.fire_resistance || 0)) < 1e-9
    );
  }

  function scenarioLoopWindow(policy, windowKey) {
    var loops = (VIEW.context && VIEW.context.loops) || [];
    var loop = null;
    for (var i = 0; i < loops.length; i += 1) {
      if (loop === null || loops[i].id === policy) loop = loops[i];
      if (loops[i].id === policy) break;
    }
    if (!loop) return { body: 0, ignite: 0, total: 0 };
    var windows = loop.windows || [];
    var chosen = null;
    for (var j = 0; j < windows.length; j += 1) {
      if (Number(windows[j].key) === Number(windowKey)) {
        chosen = windows[j];
        break;
      }
    }
    if (!chosen && windows.length) chosen = windows[windows.length - 1];
    if (!chosen) return { body: 0, ignite: 0, total: 0 };
    var body = Number(chosen.body || 0);
    var ignite = Number(chosen.ignite || 0);
    var total = Number(chosen.total || body + ignite);
    return { body: body, ignite: ignite, total: total };
  }

  function buildScenarioModel() {
    var contract = SCENARIO_CONTRACT;
    var baseline = contract.baseline || {};
    var components = contract.components || {};
    var penetration = contract.penetration || {};
    var multipliers = contract.multipliers || {};
    var shares = contract.shares || {};
    var row = scenarioLevelRow(scenarioState.level);
    var drFactor =
      row && row.factor !== null && row.factor !== undefined
        ? Number(row.factor)
        : Number(baseline.dr_factor || 0.13);
    var dr = row && row.dr !== null && row.dr !== undefined ? Number(row.dr) : 1 - drFactor;
    var baseDrFactor = Number(baseline.dr_factor || 0.13) || 0.13;
    var drRatio = baseDrFactor ? drFactor / baseDrFactor : 1;

    var baselineFire = Number(baseline.fire_resistance || 0);
    var baselineCold = Number(baseline.cold_resistance || 0);
    var bodyFirePen = Number(penetration.body_fire || 0);
    var bodyColdPen = Number(penetration.body_cold || 0);
    var ignitePen = Number(penetration.ignite || 0);

    var fireMultBase = Number(multipliers.body_fire);
    if (!isFinite(fireMultBase)) fireMultBase = 1 - (baselineFire - bodyFirePen);
    var coldMultBase = Number(multipliers.body_cold);
    if (!isFinite(coldMultBase)) coldMultBase = 1 - (baselineCold - bodyColdPen);
    var igniteMultBase = Number(multipliers.ignite);
    if (!isFinite(igniteMultBase)) igniteMultBase = 1 - (baselineFire - ignitePen);

    var fireMult = 1 - (scenarioState.fireRes - bodyFirePen);
    var coldMult = 1 - (baselineCold - bodyColdPen);
    var igniteMult = 1 - (scenarioState.fireRes - ignitePen);

    var fireRatio = drRatio * (fireMultBase ? fireMult / fireMultBase : 1);
    var coldRatio = drRatio * (coldMultBase ? coldMult / coldMultBase : 1);
    var igniteRatio = drRatio * (igniteMultBase ? igniteMult / igniteMultBase : 1);

    var bodyFire = Number(components.body_fire || 0);
    var bodyCold = Number(components.body_cold || 0);
    var bodyTotal = Number(components.body || bodyFire + bodyCold);
    var igniteDamage = Number(components.ignite || 0);
    var totalDamage = Number(components.total || bodyTotal + igniteDamage);
    var bodyRatio = bodyTotal
      ? (bodyFire * fireRatio + bodyCold * coldRatio) / bodyTotal
      : fireRatio;
    var totalRatio = totalDamage
      ? (bodyFire * fireRatio + bodyCold * coldRatio + igniteDamage * igniteRatio) /
        totalDamage
      : fireRatio;

    // resistance-only multipliers (used by the sensitivity table, whose rows
    // already contain their own level factor)
    var fireResRatio = fireMultBase ? fireMult / fireMultBase : 1;
    var igniteResRatio = igniteMultBase ? igniteMult / igniteMultBase : 1;
    var coldResRatio = coldMultBase ? coldMult / coldMultBase : 1;
    var singleResRatio = totalDamage
      ? (bodyFire * fireResRatio + bodyCold * coldResRatio + igniteDamage * igniteResRatio) /
        totalDamage
      : 1;
    var bodyFireShare = Number(shares.body_fire || (bodyTotal ? bodyFire / bodyTotal : 0));
    var bodyColdShare = Number(shares.body_cold || (bodyTotal ? bodyCold / bodyTotal : 0));

    function loopResRatio(policyId) {
      var weights = scenarioLoopWindow(policyId, "120");
      if (!weights.total) return singleResRatio;
      return (
        weights.body * (bodyFireShare * fireResRatio + bodyColdShare * coldResRatio) +
        weights.ignite * igniteResRatio
      ) / weights.total;
    }

    return {
      level: Number(scenarioState.level),
      fireRes: Number(scenarioState.fireRes),
      dr: dr,
      drFactor: drFactor,
      drRatio: drRatio,
      fireMult: fireMult,
      coldMult: coldMult,
      igniteMult: igniteMult,
      fireRatio: fireRatio,
      coldRatio: coldRatio,
      igniteRatio: igniteRatio,
      bodyRatio: bodyRatio,
      totalRatio: totalRatio,
      fireResRatio: fireResRatio,
      coldResRatio: coldResRatio,
      igniteResRatio: igniteResRatio,
      singleResRatio: singleResRatio,
      bodyFireShare: bodyFireShare,
      bodyColdShare: bodyColdShare,
      loopResRatioEager: loopResRatio("eager"),
      loopResRatioPatient: loopResRatio("patient")
    };
  }

  function scenarioLoopRatio(policy, windowKey, model) {
    var weights = scenarioLoopWindow(policy, windowKey);
    if (!weights.total) return model.totalRatio;
    return (
      weights.body * (model.bodyFireShare * model.fireRatio + model.bodyColdShare * model.coldRatio) +
      weights.ignite * model.igniteRatio
    ) / weights.total;
  }

  function scenarioGroupRatio(group, node, model) {
    if (!model) return null;
    switch (group) {
      case "fire":
        return model.fireRatio;
      case "cold":
        return model.coldRatio;
      case "ignite":
      case "tail":
      case "ignite-tail":
        return model.igniteRatio;
      case "body":
        return model.bodyRatio;
      case "total":
        return model.totalRatio;
      case "loop": {
        var holder = node.closest("[data-loop-kpi], [data-loop-policy]");
        if (!holder) return model.totalRatio;
        var policy = holder.dataset.loopPolicy || holder.dataset.policy || "";
        var windowKey = holder.dataset.loopWindow || holder.dataset.window || "120";
        return scenarioLoopRatio(policy, windowKey, model);
      }
      default:
        return null;
    }
  }

  function scenarioStatValue(name, model) {
    var penetration = SCENARIO_CONTRACT.penetration || {};
    switch (name) {
      case "level":
        return model.level;
      case "dr":
        return model.dr;
      case "drFactor":
        return model.drFactor;
      case "targetRes":
        return model.fireRes;
      case "effFire":
        return model.fireRes - Number(penetration.body_fire || 0);
      case "multFire":
        return model.fireMult;
      case "effIgnite":
        return model.fireRes - Number(penetration.ignite || 0);
      case "multIgnite":
        return model.igniteMult;
      case "effCold":
        return (
          Number((SCENARIO_CONTRACT.baseline || {}).cold_resistance || 0) -
          Number(penetration.body_cold || 0)
        );
      case "multCold":
        return model.coldMult;
      default:
        return null;
    }
  }

  function scenarioFullText(value, kind) {
    var v = Number(value);
    if (!isFinite(v)) return "—";
    switch (kind) {
      case "damage":
      case "dps":
        return groupNum(v, 2);
      case "fraction":
        return groupNum(v * 100, 2) + "%";
      case "pp":
        return groupNum(v, 2) + "%";
      case "factor":
        return "×" + groupNum(v, 6);
      case "seconds":
        return trimFixed(v.toFixed(5));
      case "stacks":
      case "stacks_rate":
        return trimFixed(v.toFixed(5));
      case "count":
      case "points":
      case "integer":
        return groupNum(v, 0);
      default:
        return trimFixed(v.toFixed(6));
    }
  }

  function updateScenarioNum(node, value) {
    var kind = node.getAttribute("data-kind") || "number";
    var fullText = scenarioFullText(value, kind);
    var compactText = node.classList.contains("n-custom") ? fullText : compact(value, kind);
    node.setAttribute("data-value", String(value));
    node.setAttribute("data-compact", compactText);
    node.setAttribute("data-full", fullText);
    node.textContent = state.fullNumbers ? fullText : compactText;
    node.title = "精确值 " + fullText;
  }

  function captureScenarioSnapshot() {
    if (scenarioSnapshot) return;
    var nodes = $$(".num[data-sc], [data-sens-row] .num, [data-sc-bin] .num");
    scenarioSnapshot = nodes.map(function (node) {
      if (!node.hasAttribute("data-base-value")) {
        node.setAttribute("data-base-value", node.getAttribute("data-value") || "0");
      }
      return {
        node: node,
        text: node.textContent,
        value: node.getAttribute("data-value"),
        compact: node.getAttribute("data-compact"),
        full: node.getAttribute("data-full"),
        title: node.title || ""
      };
    });
    scenarioMeterWidths = $$("[data-meter] .meter-seg").map(function (seg) {
      return seg.style.width;
    });
    scenarioShareSnapshot = $$(".meter-share").map(function (node) {
      return { node: node, text: node.textContent };
    });
  }

  function restoreScenarioSnapshot() {
    if (!scenarioSnapshot) return;
    scenarioSnapshot.forEach(function (item) {
      var node = item.node;
      node.textContent = item.text;
      if (item.value !== null) node.setAttribute("data-value", item.value);
      if (item.compact !== null) node.setAttribute("data-compact", item.compact);
      if (item.full !== null) node.setAttribute("data-full", item.full);
      node.title = item.title;
    });
    $$("[data-meter] .meter-seg").forEach(function (seg, index) {
      if (scenarioMeterWidths && scenarioMeterWidths[index] !== undefined) {
        seg.style.width = scenarioMeterWidths[index];
      }
    });
    if (scenarioShareSnapshot) {
      scenarioShareSnapshot.forEach(function (item) {
        item.node.textContent = item.text;
      });
    }
  }

  function updateScenarioMeters(model) {
    $$("[data-meter]").forEach(function (meter) {
      var segments = $$(".meter-seg", meter);
      var values = [];
      var total = 0;
      segments.forEach(function (seg) {
        var group = seg.getAttribute("data-meter-group") || "body";
        var base = Number(seg.getAttribute("data-meter-value") || 0);
        var ratio = scenarioGroupRatio(group, seg, model);
        var value = isFinite(base) && isFinite(ratio) ? base * ratio : 0;
        values.push(value);
        total += value;
      });
      segments.forEach(function (seg, index) {
        seg.style.width = total > 0 ? ((Math.max(0, values[index]) / total) * 100).toFixed(2) + "%" : "0%";
      });
      var legend = meter.nextElementSibling;
      if (legend && legend.classList.contains("meter-legend")) {
        var keys = $$(".meter-key", legend);
        keys.forEach(function (key, index) {
          var share = total > 0 ? values[index] / total : null;
          var shareNode = $(".meter-share", key);
          if (shareNode) shareNode.textContent = share === null ? "—" : groupNum(share * 100, 2) + "%";
        });
      }
    });
  }

  function updateScenarioBins(model) {
    $$("[data-sc-bin]").forEach(function (row) {
      var baseBody = Number(row.getAttribute("data-body") || 0);
      var baseIgnite = Number(row.getAttribute("data-ignite") || 0);
      var width = Number(row.getAttribute("data-width") || 0);
      var body = baseBody * model.bodyRatio;
      var ignite = baseIgnite * model.igniteRatio;
      var total = body + ignite;
      var dps = width > 0 ? total / width : 0;
      $$("[data-bin-cell]", row).forEach(function (cell) {
        var name = cell.getAttribute("data-bin-cell");
        var value = name === "body" ? body : name === "ignite" ? ignite : name === "total" ? total : dps;
        var kind = name === "dps" ? "dps" : "damage";
        var numNode = $(".num", cell);
        if (numNode) updateScenarioNum(numNode, value);
        else cell.textContent = compact(value, kind);
      });
    });
  }

  function updateScenarioSensitivity(model) {
    var rows = scenarioLevelRows();
    var singleRatio = model.singleResRatio;
    var eagerRatio = model.loopResRatioEager;
    var patientRatio = model.loopResRatioPatient;
    var resistanceChanged =
      Math.abs(Number(scenarioState.fireRes) - Number(SCENARIO_BASE.fire_resistance || 0)) > 1e-9;
    $$("[data-sens-row]").forEach(function (row) {
      var level = Number(row.getAttribute("data-sens-row"));
      var viewRow = null;
      for (var i = 0; i < rows.length; i += 1) {
        if (Number(rows[i].level) === level) {
          viewRow = rows[i];
          break;
        }
      }
      row.classList.toggle("is-current", level === Number(scenarioState.level));
      if (!viewRow || !resistanceChanged) return;
      [["single", singleRatio], ["eager", eagerRatio], ["patient", patientRatio]].forEach(function (pair) {
        var cell = $('[data-sens-cell="' + pair[0] + '"]', row);
        if (!cell) return;
        var numNode = $(".num", cell);
        if (!numNode) return;
        var baseValue = Number(numNode.getAttribute("data-base-value") || numNode.getAttribute("data-value") || 0);
        if (!numNode.hasAttribute("data-base-value")) numNode.setAttribute("data-base-value", String(baseValue));
        updateScenarioNum(numNode, baseValue * pair[1]);
      });
    });
  }

  function updateScenarioBanner(model) {
    var banner = $("#scenario-banner");
    var summary = $("[data-sc-banner-summary]");
    var check = $("[data-sc-banner-check]");
    if (!banner) return;
    var isBaseline = scenarioIsBaseline();
    banner.hidden = isBaseline;
    if (!summary) return;
    summary.textContent =
      "Lv" + groupNum(model.level, 0) +
      " · 怪物 DR " + groupNum(model.dr * 100, 2) + "%" +
      " · 目标火抗 " + groupNum(model.fireRes * 100, 2) + "%";
    if (check) {
      var row = scenarioLevelRow(scenarioState.level);
      var fireChanged =
        Math.abs(Number(scenarioState.fireRes) - Number(SCENARIO_BASE.fire_resistance || 0)) > 1e-9;
      if (!fireChanged && row) {
        var expected = Number((VIEW.context.single || {}).total || 0) * model.totalRatio;
        var reference = Number(row.single || expected);
        var delta = Math.abs(expected - reference);
        var ok = delta <= Math.max(1, Math.abs(reference)) * 1e-8;
        check.textContent = ok ? "引擎等级表校验 ✓" : "引擎等级表校验未通过";
        check.className = "scenario-check " + (ok ? "check-pass" : "check-fail");
      } else {
        check.textContent = "自定义火抗 · 目标侧公式";
        check.className = "scenario-check check-pill";
      }
      check.hidden = false;
    }
  }

  function scenarioError(message) {
    var node = $("[data-scenario-error]");
    if (!node) return;
    node.textContent = message || "";
    node.hidden = !message;
  }

  function syncScenarioInputs() {
    var levelInput = $("[data-scenario-level]");
    var fireInput = $("[data-scenario-fire-res]");
    if (levelInput) levelInput.value = String(Math.round(Number(scenarioState.level)));
    if (fireInput) fireInput.value = trimFixed((Number(scenarioState.fireRes) * 100).toFixed(2));
    scenarioError("");
  }

  function readScenarioInputs() {
    var levelInput = $("[data-scenario-level]");
    var fireInput = $("[data-scenario-fire-res]");
    var limits = SCENARIO_CONTRACT.limits || {};
    var levelRaw = levelInput ? levelInput.value.trim() : "";
    var fireRaw = fireInput ? fireInput.value.trim() : "";
    var level = Number(levelRaw);
    var fire = Number(fireRaw);
    if (levelRaw === "" || !isFinite(level) || Math.round(level) !== level) {
      return { error: "怪物等级需要是整数。" };
    }
    if (level < Number(limits.level_min || 1) || level > Number(limits.level_max || 100)) {
      return { error: "怪物等级范围是 " + (limits.level_min || 1) + "–" + (limits.level_max || 100) + "。" };
    }
    if (fireRaw === "" || !isFinite(fire)) {
      return { error: "目标火抗需要是数字（百分比）。" };
    }
    var minFire = Number(limits.resistance_input_min || -100);
    var maxFire = Number(limits.resistance_input_max || 200);
    if (fire < minFire || fire > maxFire) {
      return { error: "目标火抗范围是 " + minFire + "%–" + maxFire + "%。" };
    }
    return { level: level, fireRes: fire / 100 };
  }

  function applyScenarioToDom(model) {
    $$(".num[data-sc]").forEach(function (node) {
      var binding = node.getAttribute("data-sc") || "";
      var base = Number(node.getAttribute("data-base-value") || node.getAttribute("data-value") || 0);
      var value = null;
      if (binding.indexOf("group:") === 0) {
        var ratio = scenarioGroupRatio(binding.slice(6), node, model);
        if (isFinite(ratio)) value = base * ratio;
      } else if (binding.indexOf("stat:") === 0) {
        value = scenarioStatValue(binding.slice(5), model);
      }
      if (value !== null && isFinite(value)) updateScenarioNum(node, value);
    });
    updateScenarioMeters(model);
    updateScenarioBins(model);
    updateScenarioSensitivity(model);
    updateScenarioBanner(model);
  }

  function renderScenarioCharts() {
    if (state.page === "timeline") renderCharts();
  }

  function applyScenario() {
    var read = readScenarioInputs();
    if (read.error) {
      scenarioError(read.error);
      return;
    }
    captureScenarioSnapshot();
    scenarioState.level = read.level;
    scenarioState.fireRes = read.fireRes;
    scenarioState.applied = !scenarioIsBaseline();
    scenarioModelCache = buildScenarioModel();
    scenarioError("");
    if (!scenarioState.applied) {
      resetScenario();
      return;
    }
    applyScenarioToDom(scenarioModelCache);
    renderScenarioCharts();
  }

  function resetScenario() {
    restoreScenarioSnapshot();
    scenarioState.level = Number(SCENARIO_BASE.level || 100);
    scenarioState.fireRes = Number(SCENARIO_BASE.fire_resistance || 0);
    scenarioState.applied = false;
    scenarioModelCache = null;
    syncScenarioInputs();
    var banner = $("#scenario-banner");
    if (banner) banner.hidden = true;
    $$("[data-sens-row]").forEach(function (row) {
      row.classList.toggle(
        "is-current",
        Number(row.getAttribute("data-sens-row")) === Number(scenarioState.level)
      );
    });
    state.cursor = null;
    drawCursor();
    updateReadout();
    renderScenarioCharts();
  }

  function initScenario() {
    captureScenarioSnapshot();
    syncScenarioInputs();
    $$("[data-sens-row]").forEach(function (row) {
      row.classList.toggle(
        "is-current",
        Number(row.getAttribute("data-sens-row")) === Number(scenarioState.level)
      );
    });
    $$("[data-apply-scenario]").forEach(function (btn) {
      btn.addEventListener("click", applyScenario);
    });
    $$("[data-reset-scenario]").forEach(function (btn) {
      btn.addEventListener("click", resetScenario);
    });
    window.__leScenario = {
      state: scenarioState,
      model: buildScenarioModel,
      apply: applyScenario,
      reset: resetScenario
    };
    $$("[data-scenario-level], [data-scenario-fire-res]").forEach(function (input) {
      input.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          applyScenario();
        }
      });
      input.addEventListener("input", function () {
        scenarioError("");
      });
    });
  }

  /* ---------------------------------------------------------------------- */
  /* init                                                                   */
  /* ---------------------------------------------------------------------- */

  function init() {
    initPageTabs();
    initModeControls();
    initNumberToggle();
    initDrawers();
    initSourcePanel();
    initTabs();
    initFormulaSteps();
    initSourceFilters();
    initGlobalButtons();
    initScenario();
    initTimeline();
    applyNumberMode();
    var hash = (window.location.hash || "").replace("#", "");
    if (hash && document.getElementById("page-" + hash)) {
      setPage(hash, { keepScroll: true });
    } else {
      setPage("overview", { keepScroll: true });
    }
    window.requestAnimationFrame(function () {
      if (state.page === "timeline") renderCharts();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

/* ==========================================================================
   条件 Buff 页（page-conditions）· 离线场景配置与状态求值
   - 读取内嵌 view 数据（view.conditions.client_rules / baseline_scenario）
   - 控件 -> 本地 scenario -> 按契约 §8.4 优先级重算“选择/条件/支持”状态
   - 预设 / 场景 JSON 导出（Blob 下载）/ 导入（FileReader，本地校验）
   - 只更新状态徽章、计数、筛选与“与基线不同”提示；不重算 DPS
   - 纯本地交互（无网络请求）；数据缺失时静默跳过
   ========================================================================== */
(function () {
  "use strict";

  var SCENARIO_SCHEMA_DEFAULT = "bh-conditional-scenario-v1";

  var BINDING_KEYS = [
    "build_id",
    "revision_id",
    "build_content_sha256",
    "calc_input_sha256",
    "dataset_id",
    "registry_id",
    "ruleset_id"
  ];

  var STATUS_TONES = {
    not_available: "bad",
    unsupported_action: "bad",
    auto_active: "ok",
    auto_inactive: "muted",
    auto_unknown: "warn",
    not_selected: "muted",
    selected_condition_unmet: "warn",
    selected_ready_needs_rules: "info",
    selected_ready_needs_time: "info",
    selected_unconfirmed: "warn",
    active_supported: "ok",
    already_in_baseline: "muted"
  };

  var PROPOSAL_STATUSES = {
    selected_ready_needs_rules: true,
    selected_ready_needs_time: true,
    selected_unconfirmed: true,
    active_supported: true,
    auto_active: true
  };

  var DEFAULT_STATUS_LABELS = {
    not_available: "构筑无此来源",
    unsupported_action: "动作机制未支持",
    auto_active: "自动生效",
    auto_inactive: "自动未生效",
    auto_unknown: "自动判定待确认",
    not_selected: "未选择",
    selected_condition_unmet: "已选择·条件不足",
    selected_ready_needs_rules: "已选择·条件满足·缺规则映射",
    selected_ready_needs_time: "已选择·条件满足·需时间/动作模拟",
    selected_unconfirmed: "已选择·机制待确认",
    active_supported: "已选择·已接入计算",
    already_in_baseline: "已计入基础"
  };

  function $(selector, root) {
    return (root || document).querySelector(selector);
  }

  function $$(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }

  function has(object, key) {
    return Object.prototype.hasOwnProperty.call(object, key);
  }

  function isObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }

  function clone(value) {
    if (value === undefined) return undefined;
    try {
      return JSON.parse(JSON.stringify(value));
    } catch (error) {
      return undefined;
    }
  }

  function toNumber(value) {
    var number = Number(value);
    return isFinite(number) ? number : null;
  }

  function formatInt(value) {
    var number = toNumber(value);
    if (number === null) return "—";
    return String(Math.round(number)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function stableStringify(value) {
    if (value === null || typeof value !== "object") return JSON.stringify(value);
    if (Array.isArray(value)) {
      return "[" + value.map(stableStringify).join(",") + "]";
    }
    var keys = Object.keys(value).sort();
    var parts = [];
    for (var index = 0; index < keys.length; index += 1) {
      parts.push(JSON.stringify(keys[index]) + ":" + stableStringify(value[keys[index]]));
    }
    return "{" + parts.join(",") + "}";
  }

  function initConditionsPage() {
    var page = document.getElementById("page-conditions");
    if (!page) return;
    // Create the shared controller at load so the client can restore the
    // report tab/mode/scroll and the applied status after document.write.
    ensureRecalcController();
    if (window.__leRecalcRestoredApplied) {
      liveStatus("已应用（本次计算结果）。", "muted", "applied");
    } else {
      liveStatus("已应用（生成时场景）。", "muted", "applied");
    }

    var viewEl = document.getElementById("view-data");
    if (!viewEl) return;

    var VIEW;
    try {
      VIEW = JSON.parse(viewEl.textContent);
    } catch (error) {
      return;
    }
    if (!VIEW || typeof VIEW !== "object") return;

    var conditions = isObject(VIEW.conditions) ? VIEW.conditions : {};
    if (!conditions || conditions.available === false) return;

    var rules = isObject(conditions.client_rules) ? conditions.client_rules : {};
    var viewEffects = Array.isArray(conditions.effects) ? conditions.effects : [];
    var viewEffectsById = {};
    viewEffects.forEach(function (effect) {
      if (isObject(effect) && effect.effect_id) viewEffectsById[effect.effect_id] = effect;
    });

    /* ------------------------------------------------------------------ */
    /* rule payload lookups                                               */
    /* ------------------------------------------------------------------ */

    var rulesEffects = [];
    if (Array.isArray(rules.effects) && rules.effects.length) {
      rules.effects.forEach(function (effect) {
        if (isObject(effect) && effect.effect_id) rulesEffects.push(effect);
      });
    }
    if (!rulesEffects.length && viewEffects.length) {
      rulesEffects = viewEffects.map(function (effect) {
        var control = isObject(effect.control) ? effect.control : { kind: "none" };
        var conditionChecks = Array.isArray(effect.condition_checks) ? effect.condition_checks : [];
        var condition = { all: [], any: [] };
        conditionChecks.forEach(function (check) {
          if (isObject(check) && check.state_id) {
            condition.all.push({ state: check.state_id, equals: check.expected });
          }
        });
        var status = isObject(effect.status) ? effect.status : {};
        var relevance = isObject(effect.relevance) ? effect.relevance : {};
        var support = isObject(effect.support) ? effect.support : {};
        return {
          effect_id: effect.effect_id,
          name: effect.name,
          control: control,
          condition: condition,
          support: support.level || "mechanism_unresolved",
          baseline_relation: relevance.baseline_relation || "new",
          already_in_baseline: status.initial === "already_in_baseline",
          status_when_met: null
        };
      });
    }
    var rulesEffectsById = {};
    rulesEffects.forEach(function (effect) {
      rulesEffectsById[effect.effect_id] = effect;
    });

    var ruleStates = Array.isArray(rules.states) ? rules.states : [];
    var ruleStatesById = {};
    ruleStates.forEach(function (state) {
      if (isObject(state) && state.id) ruleStatesById[state.id] = state;
    });
    var viewStates = Array.isArray(conditions.states) ? conditions.states : [];
    viewStates.forEach(function (state) {
      if (!isObject(state) || !state.id || ruleStatesById[state.id]) return;
      ruleStatesById[state.id] = {
        id: state.id,
        label: state.label,
        kind: state.kind,
        source: state.source,
        default: state.value,
        bounds: state.bounds
      };
    });

    var comparisonsById = {};
    var autoValuesById = {};
    var autoStates = Array.isArray(rules.auto_states) ? rules.auto_states : [];
    autoStates.forEach(function (state) {
      if (!isObject(state) || !state.id) return;
      autoValuesById[state.id] = state;
      (Array.isArray(state.comparisons) ? state.comparisons : []).forEach(function (comparison) {
        if (isObject(comparison) && comparison.state) comparisonsById[comparison.state] = comparison;
      });
    });

    var statusLabels = {};
    Object.keys(DEFAULT_STATUS_LABELS).forEach(function (key) {
      statusLabels[key] = DEFAULT_STATUS_LABELS[key];
    });
    if (isObject(rules.status_labels)) {
      Object.keys(rules.status_labels).forEach(function (key) {
        if (rules.status_labels[key]) statusLabels[key] = String(rules.status_labels[key]);
      });
    }

    var presets = [];
    if (Array.isArray(rules.presets)) {
      rules.presets.forEach(function (preset) {
        if (isObject(preset)) presets.push(preset);
      });
    }
    var presetsById = {};
    presets.forEach(function (preset) {
      var presetId = preset.preset_id || preset.id;
      if (presetId) presetsById[presetId] = preset;
    });

    var scenarioSchema = SCENARIO_SCHEMA_DEFAULT;
    if (isObject(rules.baseline_scenario) && rules.baseline_scenario.schema) {
      scenarioSchema = String(rules.baseline_scenario.schema);
    } else if (isObject(conditions.scenario) && conditions.scenario.schema) {
      scenarioSchema = String(conditions.scenario.schema);
    }

    /* ------------------------------------------------------------------ */
    /* scenario store                                                     */
    /* ------------------------------------------------------------------ */

    var scenario = null;
    var baselineScenario = null;
    var scenarioSaved = false;
    var scenarioRevision = 0;
    var statusCache = {};

    function emptyScenario() {
      return {
        schema: scenarioSchema,
        mode: "fixed_conditions",
        baseline_profile: "build_base",
        preset_id: "custom",
        binding: {},
        target: {},
        states: {},
        effects: {},
        single_cast: {},
        loop: {}
      };
    }

    function normalizeScenario(raw) {
      var base = emptyScenario();
      if (!isObject(raw)) return base;
      if (raw.schema) base.schema = String(raw.schema);
      if (raw.mode) base.mode = String(raw.mode);
      if (raw.baseline_profile) base.baseline_profile = String(raw.baseline_profile);
      if (raw.preset_id) base.preset_id = String(raw.preset_id);
      if (isObject(raw.binding)) base.binding = clone(raw.binding);
      if (isObject(raw.target)) base.target = clone(raw.target);
      if (isObject(raw.states)) base.states = clone(raw.states);
      if (isObject(raw.effects)) {
        base.effects = clone(raw.effects);
        // Effect entry keys are control-kind specific: a stacks effect must not
        // carry `enabled` (the backend validator rejects it), and a toggle must
        // not carry `stacks`.  Normalise legacy/hand-edited/exported scenarios.
        Object.keys(base.effects).forEach(function (effectId) {
          var rule = rulesEffectsById[effectId];
          var kind = isObject(rule) && isObject(rule.control) ? rule.control.kind : "";
          var entry = base.effects[effectId];
          if (!isObject(entry)) return;
          if (kind === "stacks") {
            delete entry.enabled;
          } else if (kind === "toggle" || kind === "cast_state" || kind === "none") {
            delete entry.stacks;
          }
        });
      }
      if (isObject(raw.single_cast)) base.single_cast = clone(raw.single_cast);
      if (isObject(raw.loop)) base.loop = clone(raw.loop);
      return base;
    }

    function readTargetBlock() {
      var state = null;
      try {
        state = window.__leScenario && window.__leScenario.state ? window.__leScenario.state : null;
      } catch (error) {
        state = null;
      }
      var levelInput = document.querySelector("[data-scenario-level]");
      var fireInput = document.querySelector("[data-scenario-fire-res]");
      var level = state && isFinite(Number(state.level)) ? Number(state.level) : null;
      if (level === null && levelInput) level = Number(levelInput.value);
      var fire = state && isFinite(Number(state.fireRes)) ? Number(state.fireRes) : null;
      if (fire === null && fireInput) fire = Number(fireInput.value) / 100;
      if (!isFinite(level)) level = 100;
      if (!isFinite(fire)) fire = 0;
      return { monster_level: Math.round(level), fire_resistance: fire };
    }

    function applyTargetBlock(target) {
      if (!isObject(target)) return;
      var level = Number(target.monster_level);
      var fire = Number(target.fire_resistance);
      if (!isFinite(level) || !isFinite(fire)) return;
      var levelInput = document.querySelector("[data-scenario-level]");
      var fireInput = document.querySelector("[data-scenario-fire-res]");
      if (levelInput) levelInput.value = String(Math.round(level));
      if (fireInput) fireInput.value = String(Math.round(fire * 10000) / 100);
      try {
        if (window.__leScenario && typeof window.__leScenario.apply === "function") {
          // applyScenario reads the DOM inputs and updates scenarioState/display.
          window.__leScenario.apply();
          return;
        }
      } catch (error) {
        /* fall back to the apply button */
      }
      var applyButton = document.querySelector("[data-apply-scenario]");
      if (applyButton) applyButton.click();
    }

    function scenarioFromDom() {
      var next = emptyScenario();
      next.target = readTargetBlock();
      $$("[data-cond-toggle]", page).forEach(function (input) {
        var effectId = input.getAttribute("data-cond-toggle");
        if (!effectId) return;
        next.effects[effectId] = { enabled: !!input.checked };
        var rule = rulesEffectsById[effectId];
        var control = rule && isObject(rule.control) ? rule.control : {};
        if (control.state_id) next.states[control.state_id] = !!input.checked;
      });
      $$("[data-cond-stacks]", page).forEach(function (input) {
        var effectId = input.getAttribute("data-cond-stacks");
        if (!effectId) return;
        var stacks = toNumber(input.value);
        if (stacks === null) stacks = 0;
        next.effects[effectId] = { stacks: stacks };
        var rule = rulesEffectsById[effectId];
        var control = rule && isObject(rule.control) ? rule.control : {};
        if (control.stack_state_id) next.states[control.stack_state_id] = stacks;
      });
      $$("[data-cond-cast]", page).forEach(function (input) {
        var key = input.getAttribute("data-cond-cast");
        if (key) next.single_cast[key] = !!input.checked;
      });
      $$("[data-cond-state]", document).forEach(function (input) {
        var stateId = input.getAttribute("data-cond-state");
        if (!stateId) return;
        var definition = ruleStatesById[stateId] || {};
        if (input.type === "checkbox") {
          next.states[stateId] = !!input.checked;
        } else {
          var number = toNumber(input.value);
          next.states[stateId] = number === null ? 0 : number;
        }
        if (definition.kind === "bool") next.states[stateId] = !!next.states[stateId];
      });
      return next;
    }

    function scenarioSections(target) {
      return {
        states: isObject(target.states) ? target.states : {},
        effects: isObject(target.effects) ? target.effects : {},
        single_cast: isObject(target.single_cast) ? target.single_cast : {},
        loop: isObject(target.loop) ? target.loop : {}
      };
    }

    function changedFromBaseline() {
      if (!scenario || !baselineScenario) return false;
      return (
        stableStringify(scenarioSections(scenario)) !== stableStringify(scenarioSections(baselineScenario))
      );
    }

    /* ------------------------------------------------------------------ */
    /* state / condition evaluation                                       */
    /* ------------------------------------------------------------------ */

    function stateValue(stateId) {
      var id = String(stateId || "");
      if (!id) return null;
      if (id.indexOf("single_cast.") === 0) {
        var key = id.slice("single_cast.".length);
        return scenario.single_cast[key] === true;
      }
      if (id.indexOf("loop.") === 0) {
        var loopKey = id.slice("loop.".length);
        if (has(scenario.loop, loopKey)) return scenario.loop[loopKey];
        return null;
      }
      if (has(scenario.states, id)) return scenario.states[id];
      if (comparisonsById[id]) return comparisonsById[id].met === true;
      if (autoValuesById[id] && autoValuesById[id].value !== undefined) return autoValuesById[id].value;
      var definition = ruleStatesById[id];
      if (definition) return has(definition, "default") ? definition.default : null;
      return null;
    }

    function stateLabel(stateId) {
      var id = String(stateId || "");
      var definition = ruleStatesById[id];
      if (definition && definition.label) return String(definition.label);
      var comparison = comparisonsById[id];
      if (comparison && comparison.label) return String(comparison.label);
      var auto = autoValuesById[id];
      if (auto && auto.label) return String(auto.label);
      return id;
    }

    function equalValue(actual, expected) {
      if (typeof expected === "boolean") {
        if (typeof actual === "boolean") return actual === expected;
        var number = toNumber(actual);
        return number === null ? false : (number !== 0) === expected;
      }
      if (typeof expected === "number") {
        var numeric = toNumber(actual);
        return numeric === null ? false : numeric === expected;
      }
      return String(actual) === String(expected);
    }

    function compareCondition(actual, entry) {
      if (actual === null || actual === undefined) return null;
      if (has(entry, "equals")) return equalValue(actual, entry.equals);
      var target = has(entry, "value") ? entry.value : entry.threshold;
      var op = entry.op;
      if (op && target !== undefined) {
        var left = toNumber(actual);
        var right = toNumber(target);
        if (left !== null && right !== null) {
          switch (String(op)) {
            case ">=":
              return left >= right;
            case ">":
              return left > right;
            case "<=":
              return left <= right;
            case "<":
              return left < right;
            case "==":
            case "=":
              return left === right;
            case "!=":
              return left !== right;
            default:
              break;
          }
        }
      }
      if (entry.min !== undefined || entry.max !== undefined) {
        var value = toNumber(actual);
        if (value === null) return null;
        if (entry.min !== undefined && value < Number(entry.min)) return false;
        if (entry.max !== undefined && value > Number(entry.max)) return false;
        return true;
      }
      return null;
    }

    function expectedValue(entry) {
      if (has(entry, "equals")) return entry.equals;
      if (has(entry, "value")) return entry.value;
      if (entry.threshold !== undefined) return entry.threshold;
      if (entry.min !== undefined || entry.max !== undefined) return { min: entry.min, max: entry.max };
      return null;
    }

    function evaluateCondition(condition) {
      var entries = [];
      if (isObject(condition)) {
        (Array.isArray(condition.all) ? condition.all : []).forEach(function (entry) {
          if (isObject(entry)) entries.push({ group: "all", entry: entry });
        });
        (Array.isArray(condition.any) ? condition.any : []).forEach(function (entry) {
          if (isObject(entry)) entries.push({ group: "any", entry: entry });
        });
      }
      var checks = [];
      var byState = {};
      var hasUnknown = false;
      var anyFailed = false;
      var anyMet = false;
      var hasAny = false;
      entries.forEach(function (item) {
        var entry = item.entry;
        var stateId = String(entry.state || "");
        var actual = stateValue(stateId);
        var met = compareCondition(actual, entry);
        var label = String(entry.label || "") || stateLabel(stateId);
        var check = {
          state: stateId,
          label: label,
          expected: expectedValue(entry),
          actual: actual,
          met: met,
          group: item.group
        };
        checks.push(check);
        if (!has(byState, stateId)) byState[stateId] = check;
        if (met === null) hasUnknown = true;
        if (item.group === "all") {
          if (met !== true) anyFailed = true;
        } else {
          hasAny = true;
          if (met === true) anyMet = true;
        }
      });
      var allMet = !anyFailed;
      if (hasAny && !anyMet) allMet = false;
      return {
        allMet: allMet,
        hasUnknown: hasUnknown,
        condition: allMet,
        checks: checks,
        byState: byState
      };
    }

    function unmetLabels(checks) {
      var labels = [];
      checks.forEach(function (check) {
        if (check.met !== true && check.label && labels.indexOf(check.label) < 0) {
          labels.push(check.label);
        }
      });
      return labels;
    }

    function isSelected(effect) {
      var effectId = effect.effect_id;
      var control = isObject(effect.control) ? effect.control : {};
      var kind = control.kind || "none";
      var entry = isObject(scenario.effects[effectId]) ? scenario.effects[effectId] : {};
      if (kind === "toggle") return entry.enabled === true;
      if (kind === "stacks") {
        var stacks = toNumber(entry.stacks);
        if (stacks === null) stacks = toNumber(scenario.states[control.stack_state_id]);
        return stacks !== null && stacks > 0;
      }
      if (kind === "cast_state") {
        var key = control.cast_state_id || control.state_id || effectId;
        return scenario.single_cast[key] === true;
      }
      return false;
    }

    function statusLabel(statusId) {
      return statusLabels[statusId] || statusId;
    }

    function result(statusId, reason, evaluation) {
      return {
        id: statusId,
        label: statusLabel(statusId),
        tone: STATUS_TONES[statusId] || "muted",
        reason: reason || "",
        condition: evaluation ? evaluation.allMet === true : null,
        allMet: evaluation ? evaluation.allMet === true : null,
        hasUnknown: evaluation ? evaluation.hasUnknown === true : false,
        checks: evaluation ? evaluation.byState : {}
      };
    }

    function computeStatus(effect) {
      var effectId = effect.effect_id;
      var viewEffect = viewEffectsById[effectId] || {};
      var control = isObject(effect.control) ? effect.control : { kind: "none" };
      var kind = control.kind || "none";
      var support = String(effect.support || "");
      var activationKind =
        effect.activation_kind ||
        (isObject(effect.activation) && effect.activation.kind) ||
        "";
      var baselineRelation = String(effect.baseline_relation || "");
      var qualification = isObject(effect.qualification) ? effect.qualification : null;
      var evaluation = evaluateCondition(isObject(effect.condition) ? effect.condition : {});
      var already =
        effect.already_in_baseline === true || baselineRelation === "already_in_baseline";

      function reasonFor(statusId, fallback) {
        var statusPayload = viewEffect.status;
        if (isObject(statusPayload) && statusPayload.initial === statusId && statusPayload.reason) {
          return String(statusPayload.reason);
        }
        return fallback;
      }

      if (effect.available === false || (qualification && qualification.available === false)) {
        return result(
          "not_available",
          reasonFor("not_available", "来源资格不满足（未装备/未分配/点数不足）。"),
          evaluation
        );
      }
      if (
        activationKind === "action" ||
        (support === "needs_action_or_time_simulation" &&
          kind !== "toggle" &&
          kind !== "stacks" &&
          kind !== "cast_state")
      ) {
        return result(
          "unsupported_action",
          reasonFor("unsupported_action", "动作机制不在固定条件模式支持范围；本页不伪造生效。"),
          evaluation
        );
      }
      if (already) {
        var baselineReason =
          (viewEffect.status && viewEffect.status.reason) ||
          viewEffect.baseline_note ||
          "已计入基础快照；条件说明仅供参考。";
        return result("already_in_baseline", baselineReason, evaluation);
      }
      if (kind === "none") {
        if (evaluation.hasUnknown) {
          return result("auto_unknown", reasonFor("auto_unknown", "包含无法判定的自动状态。"), evaluation);
        }
        if (evaluation.allMet) {
          return result("auto_active", reasonFor("auto_active", "条件自动满足。"), evaluation);
        }
        var unmet = unmetLabels(evaluation.checks);
        return result(
          "auto_inactive",
          reasonFor(
            "auto_inactive",
            unmet.length ? "自动未满足：" + unmet.join("、") : "自动判定未满足。"
          ),
          evaluation
        );
      }
      if (!isSelected(effect)) {
        return result("not_selected", reasonFor("not_selected", "未选择该效果。"), evaluation);
      }
      if (!evaluation.allMet) {
        var failed = unmetLabels(evaluation.checks);
        return result(
          "selected_condition_unmet",
          reasonFor(
            "selected_condition_unmet",
            failed.length ? "条件未满足：" + failed.join("、") : "条件未满足。"
          ),
          evaluation
        );
      }
      if (support === "needs_rule_mapping") {
        return result(
          "selected_ready_needs_rules",
          reasonFor("selected_ready_needs_rules", "条件已满足，但现有引擎缺少规则映射。"),
          evaluation
        );
      }
      if (support === "needs_action_or_time_simulation") {
        return result(
          "selected_ready_needs_time",
          reasonFor("selected_ready_needs_time", "条件已满足，但需要时间/动作模拟。"),
          evaluation
        );
      }
      if (support === "mechanism_unresolved") {
        return result(
          "selected_unconfirmed",
          reasonFor("selected_unconfirmed", "机制尚未确认，不能断言会改变伤害。"),
          evaluation
        );
      }
      if (support === "engine_supported") {
        return result(
          "active_supported",
          reasonFor("active_supported", "条件已满足且已接入计算。"),
          evaluation
        );
      }
      return result(
        effect.status_when_met || "selected_ready_needs_rules",
        "条件已满足；支持级别待确认。",
        evaluation
      );
    }

    /* ------------------------------------------------------------------ */
    /* DOM references                                                     */
    /* ------------------------------------------------------------------ */

    var searchInput = $("[data-cond-search]", page);
    var categorySelect = $("[data-cond-category]", page);
    var relevanceSelect = $("[data-cond-relevance]", page);
    var supportSelect = $("[data-cond-support]", page);
    var statusSelect = $("[data-cond-status]", page);
    var presetSelect = $("[data-cond-preset]", page);
    var importInput = $("[data-cond-import]", page);
    var errorNode = $("[data-cond-import-error]", page);
    var statusNode = $("[data-cond-import-status]", page);
    var changedNode = $("[data-cond-changed]", page);

    function showError(message) {
      if (!errorNode) return;
      errorNode.textContent = message || "";
      errorNode.hidden = !message;
      if (message && statusNode) statusNode.hidden = true;
    }

    function showStatus(message) {
      if (!statusNode) return;
      statusNode.textContent = message || "";
      statusNode.hidden = !message;
      if (message && errorNode) errorNode.hidden = true;
    }

    function clearMessages() {
      showError("");
      showStatus("");
    }

    /* ------------------------------------------------------------------ */
    /* DOM sync: controls, badges, checks, counts, notice                 */
    /* ------------------------------------------------------------------ */

    function syncControlsFromScenario() {
      $$("[data-cond-toggle]", page).forEach(function (input) {
        var effectId = input.getAttribute("data-cond-toggle");
        var entry = isObject(scenario.effects[effectId]) ? scenario.effects[effectId] : {};
        input.checked = entry.enabled === true;
      });
      $$("[data-cond-stacks]", page).forEach(function (input) {
        var effectId = input.getAttribute("data-cond-stacks");
        var entry = isObject(scenario.effects[effectId]) ? scenario.effects[effectId] : {};
        var rule = rulesEffectsById[effectId];
        var control = rule && isObject(rule.control) ? rule.control : {};
        var stacks = toNumber(entry.stacks);
        if (stacks === null) stacks = toNumber(scenario.states[control.stack_state_id]);
        if (stacks === null) stacks = 0;
        input.value = String(stacks);
      });
      $$("[data-cond-cast]", page).forEach(function (input) {
        var key = input.getAttribute("data-cond-cast");
        input.checked = scenario.single_cast[key] === true;
      });
      $$("[data-cond-state]", document).forEach(function (input) {
        var stateId = input.getAttribute("data-cond-state");
        var value = has(scenario.states, stateId) ? scenario.states[stateId] : null;
        if (input.type === "checkbox") {
          input.checked = value === true;
        } else {
          var number = toNumber(value);
          input.value = String(number === null ? 0 : number);
        }
      });
    }

    function findOption(select, value) {
      if (!select) return null;
      for (var index = 0; index < select.options.length; index += 1) {
        if (select.options[index].value === value) return select.options[index];
      }
      return null;
    }

    function syncPresetSelect() {
      if (!presetSelect) return;
      var changed = changedFromBaseline();
      if (!changed) {
        var current = scenario.preset_id || (isObject(baselineScenario) && baselineScenario.preset_id) || "";
        var option = current ? findOption(presetSelect, current) : null;
        if (option) {
          presetSelect.value = current;
        } else if (presetSelect.options.length) {
          presetSelect.selectedIndex = 0;
        }
        return;
      }
      var custom = findOption(presetSelect, "custom");
      if (!custom) {
        custom = document.createElement("option");
        custom.value = "custom";
        presetSelect.appendChild(custom);
      }
      custom.textContent = scenarioSaved ? "自定义（已保存）" : "自定义（未保存）";
      presetSelect.value = "custom";
    }

    function applicationStateOf(effectId) {
      if (!(isObject(conditions) && conditions.scenario_applied === true)) return null;
      var recompute = isObject(conditions.recompute) ? conditions.recompute : {};
      var states = isObject(recompute.application_states) ? recompute.application_states : null;
      if (states && isObject(states[effectId])) return states[effectId];
      var application = isObject(recompute.condition_application) ? recompute.condition_application : {};
      var applied = Array.isArray(application.applied) ? application.applied : [];
      for (var index = 0; index < applied.length; index += 1) {
        if (isObject(applied[index]) && applied[index].effect_id === effectId) {
          return {
            state: applied[index].scope === "single_cast" ? "applied_single" : "applied_both",
            assumptions: applied[index].assumption_ids || (applied[index].assumption_note ? [applied[index].assumption_note] : []),
            reason: "",
          };
        }
      }
      var skipped = Array.isArray(application.skipped) ? application.skipped : [];
      for (var sk = 0; sk < skipped.length; sk += 1) {
        if (isObject(skipped[sk]) && skipped[sk].effect_id === effectId) {
          return { state: "skipped", reason: skipped[sk].detail || skipped[sk].reason || "" };
        }
      }
      return { state: "not_selected", reason: "当前生成场景未选择该效果。" };
    }

    var APPLICATION_LABELS = {
      applied_both: ["已计入", "ok"],
      applied_single: ["已计入·仅单次", "info"],
      applied_loop: ["已计入·仅循环", "info"],
      skipped: ["未计入", "muted"],
      not_selected: ["未启用", "muted"],
      baseline: ["已计入基线", "muted"],
    };

    function updateStatusBadges() {
      $$("[data-cond-status-for]", page).forEach(function (node) {
        var effectId = node.getAttribute("data-cond-status-for");
        var application = applicationStateOf(effectId);
        if (application) {
          var pair = APPLICATION_LABELS[application.state] || [application.state, "muted"];
          var applicationLabel = pair[0];
          if (Array.isArray(application.assumptions) && application.assumptions.length && applicationLabel.indexOf("已计入") === 0) {
            applicationLabel += "·含假设";
          }
          node.textContent = applicationLabel;
          node.className = "badge badge-" + pair[1] + " cond-status";
          node.setAttribute("data-cond-application", application.state);
          var reasons = [];
          if (application.reason) reasons.push(String(application.reason));
          if (Array.isArray(application.assumptions) && application.assumptions.length) {
            reasons.push("含假设：" + application.assumptions.slice(0, 3).join("；"));
          }
          if (reasons.length) node.setAttribute("title", reasons.join(" "));
          else node.removeAttribute("title");
          return;
        }
        var status = statusCache[effectId];
        if (!status) return;
        node.textContent = status.label;
        node.className = "badge badge-" + status.tone + " cond-status";
        node.removeAttribute("data-cond-application");
        if (status.reason) node.setAttribute("title", status.reason);
        else node.removeAttribute("title");
      });
    }

    function updateStatusDetails() {
      $$("[data-cond-status-reason-for]", page).forEach(function (node) {
        var effectId = node.getAttribute("data-cond-status-reason-for");
        var status = statusCache[effectId];
        var reason = status && status.reason ? String(status.reason) : "";
        var textNode = node.querySelector(".cond-status-reason-text");
        if (textNode) textNode.textContent = reason;
        node.hidden = !reason;
      });
      $$("[data-cond-proposal-for]", page).forEach(function (node) {
        var effectId = node.getAttribute("data-cond-proposal-for");
        var application = applicationStateOf(effectId);
        if (application) {
          node.hidden = application.state === "not_selected";
          return;
        }
        var status = statusCache[effectId];
        node.hidden = !(status && has(PROPOSAL_STATUSES, status.id));
      });
    }

    function updateConditionChecks() {
      $$("[data-cond-check-for]", page).forEach(function (item) {
        var effectId = item.getAttribute("data-cond-check-for");
        var stateId = item.getAttribute("data-cond-check-state") || "";
        var status = statusCache[effectId];
        var check = status && status.checks ? status.checks[stateId] : null;
        var met = check ? check.met : null;
        var mark = met === true ? "✓" : met === false ? "!" : "?";
        var markNode = item.querySelector(".cond-check-mark");
        if (markNode) markNode.textContent = mark;
        item.classList.toggle("check-pass", met === true);
        item.classList.toggle("check-fail", met === false);
      });
    }

    function updateRows() {
      $$("[data-cond-row]", page).forEach(function (row) {
        var effectId = row.getAttribute("data-effect-id");
        var status = statusCache[effectId];
        if (status) row.setAttribute("data-status", status.id);
      });
    }

    function refreshCounts() {
      var counts = {
        configured: 0,
        condition_met: 0,
        condition_unmet: 0,
        not_selected: 0,
        auto_active: 0,
        auto_inactive: 0,
        auto_unknown: 0,
        already_in_baseline: 0,
        engine_supported: 0,
        needs_rules: 0,
        needs_time: 0,
        mechanism_unresolved: 0
      };
      rulesEffects.forEach(function (effect) {
        var support = String(effect.support || "");
        if (support === "engine_supported") counts.engine_supported += 1;
        else if (support === "needs_rule_mapping") counts.needs_rules += 1;
        else if (support === "needs_action_or_time_simulation") counts.needs_time += 1;
        else counts.mechanism_unresolved += 1;
        if (isSelected(effect)) counts.configured += 1;
        var status = statusCache[effect.effect_id];
        if (!status) return;
        if (has(counts, status.id)) counts[status.id] += 1;
        if (status.condition === true) counts.condition_met += 1;
        else if (status.condition === false) counts.condition_unmet += 1;
      });
      $$("[data-cond-count]", page).forEach(function (node) {
        var key = node.getAttribute("data-cond-count");
        if (has(counts, key)) node.textContent = formatInt(counts[key]);
      });
    }

    function updateChangedNotice() {
      if (!changedNode) return;
      changedNode.hidden = !changedFromBaseline();
    }

    /* ------------------------------------------------------------------ */
    /* filtering                                                          */
    /* ------------------------------------------------------------------ */

    function normalizeFilter(value) {
      var text = value === null || value === undefined ? "" : String(value).trim();
      return !text || text === "all" ? "" : text;
    }

    function applyFilters() {
      var query = searchInput ? String(searchInput.value || "").trim().toLowerCase() : "";
      var category = normalizeFilter(categorySelect && categorySelect.value);
      var relevance = normalizeFilter(relevanceSelect && relevanceSelect.value);
      var support = normalizeFilter(supportSelect && supportSelect.value);
      var status = normalizeFilter(statusSelect && statusSelect.value);
      var rows = $$("[data-cond-row]", page);
      var visible = 0;
      rows.forEach(function (row) {
        var haystack = row.getAttribute("data-search") || "";
        var ok =
          (!query || haystack.indexOf(query) >= 0) &&
          (!category || row.getAttribute("data-category") === category) &&
          (!relevance || row.getAttribute("data-relevance") === relevance) &&
          (!support || row.getAttribute("data-support") === support) &&
          (!status || row.getAttribute("data-status") === status);
        row.hidden = !ok;
        if (ok) visible += 1;
      });
      $$("[data-cond-row-container]", page).forEach(function (container) {
        container.hidden = !container.querySelector("[data-cond-row]:not([hidden])");
      });
      var emptyNode = $("[data-cond-empty]", page);
      if (emptyNode) emptyNode.hidden = rows.length === 0 || visible > 0;
      var countNode = $("[data-cond-visible]", page);
      if (countNode) {
        countNode.textContent =
          visible === rows.length
            ? "共 " + rows.length + " 个效果"
            : "显示 " + visible + " / " + rows.length + " 个效果";
      }
    }

    /* ------------------------------------------------------------------ */
    /* scenario mutations                                                 */
    /* ------------------------------------------------------------------ */

    function ensureEffectEntry(effectId) {
      if (!isObject(scenario.effects[effectId])) scenario.effects[effectId] = {};
      return scenario.effects[effectId];
    }

    var recalcController = null;

    function liveStatus(text, tone, state) {
      var node = document.querySelector("[data-cond-live-status]");
      if (!node) return;
      node.hidden = false;
      node.className = "cond-live-status small " + (tone === "error" ? "cond-live-error" : "muted");
      var resolved = state || (tone === "error" ? "error" : "changed");
      if (window.ReportActions) {
        window.ReportActions.setStatus(node, text, resolved);
      } else {
        node.textContent = String(text || "");
      }
    }

    function currentReportBinding() {
      var node = document.getElementById("le-recalc-bindings");
      if (!node) return null;
      try { return JSON.parse(node.textContent || "{}"); } catch (error) { return null; }
    }

    function ensureRecalcController() {
      if (recalcController) return recalcController;
      if (!window.LeRecalc) return null;
      recalcController = window.LeRecalc.createController({
        getScenario: function () {
          var payload = normalizeScenario(scenario);
          if (changedFromBaseline()) payload.preset_id = "custom";
          payload.target = readTargetBlock();
          return payload;
        },
        onStatus: function (text, tone, state) { liveStatus(text, tone, state); },
        onBusy: function (busy) {
          var button = document.querySelector("[data-cond-live-recompute]");
          if (button) button.disabled = !!busy;
        },
        onAccepted: function (payload) {
          liveStatus("计算完成（已应用本次结果）。", "muted", "applied");
          if (payload && payload.html) {
            document.open();
            document.write(payload.html);
            document.close();
          }
        },
        onPrepared: function () { liveStatus("配置已准备（未变更当前结果）。", "muted"); }
      });
      return recalcController;
    }

    function bumpScenarioRevision() {
      scenarioRevision += 1;
      var controller = ensureRecalcController();
      if (controller) controller.markEdit();
    }

    function markCustomPreset() {
      scenario.preset_id = "custom";
      scenarioSaved = false;
      bumpScenarioRevision();
    }

    function setEffectEnabled(effectId, enabled) {
      var rule = rulesEffectsById[effectId] || {};
      var control = isObject(rule.control) ? rule.control : {};
      var entry = ensureEffectEntry(effectId);
      entry.enabled = enabled === true;
      if (control.state_id && ruleStatesById[control.state_id]) {
        scenario.states[control.state_id] = enabled === true;
      }
      markCustomPreset();
    }

    function clampStacks(value, bounds) {
      var number = toNumber(value);
      if (number === null) number = 0;
      number = Math.round(number);
      var limits = isObject(bounds) ? bounds : {};
      if (toNumber(limits.min) !== null) number = Math.max(Number(limits.min), number);
      if (toNumber(limits.max) !== null) number = Math.min(Number(limits.max), number);
      return number;
    }

    function setEffectStacks(effectId, rawValue) {
      var rule = rulesEffectsById[effectId] || {};
      var control = isObject(rule.control) ? rule.control : {};
      var bounds = control.bounds;
      if (!isObject(bounds) && control.stack_state_id) {
        var stateDefinition = ruleStatesById[control.stack_state_id];
        if (stateDefinition) bounds = stateDefinition.bounds;
      }
      var stacks = clampStacks(rawValue, bounds);
      var entry = ensureEffectEntry(effectId);
      entry.stacks = stacks;
      delete entry.enabled;
      if (control.stack_state_id) scenario.states[control.stack_state_id] = stacks;
      markCustomPreset();
    }

    function setStateValue(stateId, input) {
      var definition = ruleStatesById[stateId] || {};
      var kind = definition.kind || "bool";
      if (kind === "bool" || input.type === "checkbox") {
        scenario.states[stateId] = !!input.checked;
      } else {
        scenario.states[stateId] = clampStacks(input.value, definition.bounds);
      }
      rulesEffects.forEach(function (effect) {
        var control = isObject(effect.control) ? effect.control : {};
        if (control.kind === "toggle" && control.state_id === stateId) {
          var entry = ensureEffectEntry(effect.effect_id);
          entry.enabled = scenario.states[stateId] === true;
        }
        if (control.kind === "stacks" && control.stack_state_id === stateId) {
          var stackEntry = ensureEffectEntry(effect.effect_id);
          var stacks = toNumber(scenario.states[stateId]);
          stackEntry.stacks = stacks === null ? 0 : stacks;
          delete stackEntry.enabled;
        }
      });
      markCustomPreset();
    }

    function setCastState(key, enabled) {
      scenario.single_cast[key] = enabled === true;
      markCustomPreset();
    }

    /* ------------------------------------------------------------------ */
    /* presets / reset / export / import                                  */
    /* ------------------------------------------------------------------ */

    function applyPreset() {
      var presetId = presetSelect ? presetSelect.value : "";
      var preset = presetsById[presetId];
      if (!preset) {
        if (presetId === "custom") return;
        if (isObject(baselineScenario) && presetId && presetId === baselineScenario.preset_id) {
          resetScenario();
          return;
        }
        showError("未找到预设：" + (presetId || "（未选择）"));
        return;
      }
      var next = normalizeScenario({
        schema: scenario.schema,
        mode: preset.mode || scenario.mode,
        baseline_profile: preset.baseline_profile || scenario.baseline_profile,
        preset_id: presetId,
        binding: clone(scenario.binding),
        states: preset.states,
        effects: preset.effects,
        single_cast: preset.single_cast,
        loop: preset.loop
      });
      scenario = next;
      scenario.target = readTargetBlock();
      scenarioSaved = false;
      bumpScenarioRevision();
      syncControlsFromScenario();
      clearMessages();
      refresh();
      showStatus("已应用预设：" + String(preset.label || presetId) + "。");
    }

    function resetScenario() {
      scenario = normalizeScenario(baselineScenario);
      if (isObject(scenario.target) && Object.keys(scenario.target).length) applyTargetBlock(scenario.target);
      scenarioSaved = false;
      bumpScenarioRevision();
      syncControlsFromScenario();
      clearMessages();
      refresh();
      showStatus("已恢复默认 Buff。");
    }

    function exportScenario() {
      var payload = normalizeScenario(scenario);
      payload.preset_id = changedFromBaseline() ? "custom" : payload.preset_id || "custom";
      payload.target = readTargetBlock();
      payload = {
        schema_version: "le-report-config-v1",
        binding: currentReportBinding(),
        scenario: payload
      };
      var filename = "bh-report-config.json";
      var exportInfo = isObject(conditions.export) ? conditions.export : {};
      if (exportInfo.scenario_filename) filename = String(exportInfo.scenario_filename);
      filename = filename.replace(/[\\/:*?"<>|]/g, "_");
      var text = JSON.stringify(payload, null, 2) + "\n";
      var blob = new Blob([text], { type: "application/json;charset=utf-8" });
      var url = URL.createObjectURL(blob);
      var link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.style.display = "none";
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.setTimeout(function () {
        URL.revokeObjectURL(url);
      }, 1500);
      scenarioSaved = true;
      syncPresetSelect();
      showStatus("已保存配置。");
    }

    function liveRecompute() {
      var controller = ensureRecalcController();
      if (!controller) {
        liveStatus("页面缺少共享重算客户端，无法计算。", "error");
        return;
      }
      controller.compute("compute");
    }

    function knownCastKeys() {
      var keys = {};
      if (isObject(baselineScenario) && isObject(baselineScenario.single_cast)) {
        Object.keys(baselineScenario.single_cast).forEach(function (key) {
          keys[key] = true;
        });
      }
      rulesEffects.forEach(function (effect) {
        var control = isObject(effect.control) ? effect.control : {};
        if (control.kind === "cast_state") {
          var key = control.cast_state_id || control.state_id;
          if (key) keys[key] = true;
        }
        var condition = isObject(effect.condition) ? effect.condition : {};
        (Array.isArray(condition.all) ? condition.all : []).forEach(function (entry) {
          if (isObject(entry) && typeof entry.state === "string" && entry.state.indexOf("single_cast.") === 0) {
            keys[entry.state.slice("single_cast.".length)] = true;
          }
        });
      });
      return keys;
    }

    function knownLoopKeys() {
      var keys = {};
      if (isObject(baselineScenario) && isObject(baselineScenario.loop)) {
        Object.keys(baselineScenario.loop).forEach(function (key) {
          keys[key] = key === "action_stream" ? "string" : typeof baselineScenario.loop[key];
        });
      }
      return keys;
    }

    function boundsOf(control, stackStateId) {
      if (isObject(control.bounds)) return control.bounds;
      var definition = ruleStatesById[stackStateId];
      return definition && isObject(definition.bounds) ? definition.bounds : null;
    }

    function validateScenario(raw) {
      var errors = [];
      var warnings = [];
      if (!isObject(raw)) {
        return { errors: ["文件内容不是 JSON 对象。"], warnings: warnings };
      }
      if (String(raw.schema || "") !== scenarioSchema) {
        errors.push("schema 不匹配：需要 " + scenarioSchema + "，实际 " + String(raw.schema || "（缺失）") + "。");
      }
      var expectedBinding = isObject(baselineScenario) ? baselineScenario.binding : null;
      if (isObject(expectedBinding) && Object.keys(expectedBinding).length) {
        var rawBinding = isObject(raw.binding) ? raw.binding : {};
        BINDING_KEYS.forEach(function (key) {
          if (!expectedBinding[key]) return;
          if (!has(rawBinding, key)) {
            errors.push("缺少构筑绑定字段 " + key + "（旧场景需要重新生成）。");
          } else if (String(rawBinding[key]) !== String(expectedBinding[key])) {
            errors.push("构筑绑定不一致（" + key + "）：场景来自其他构筑或输入，不能直接套用。");
          }
        });
      }
      if (raw.states !== undefined) {
        if (!isObject(raw.states)) {
          errors.push("states 必须是对象。");
        } else {
          Object.keys(raw.states).forEach(function (stateId) {
            var definition = ruleStatesById[stateId];
            if (!definition) {
              errors.push("未知状态 id：" + stateId + "。");
              return;
            }
            var value = raw.states[stateId];
            if (definition.kind === "bool") {
              if (typeof value !== "boolean") errors.push("状态 " + stateId + " 需要布尔值。");
            } else {
              var number = typeof value === "number" ? value : null;
              if (number === null || !isFinite(number)) {
                errors.push("状态 " + stateId + " 需要数字。");
                return;
              }
              if (isObject(definition.bounds)) {
                if (definition.bounds.min !== undefined && number < Number(definition.bounds.min)) {
                  errors.push("状态 " + stateId + " 超出下限 " + definition.bounds.min + "。");
                }
                if (definition.bounds.max !== undefined && number > Number(definition.bounds.max)) {
                  errors.push("状态 " + stateId + " 超出上限 " + definition.bounds.max + "。");
                }
              }
            }
          });
        }
      }
      if (raw.effects !== undefined) {
        if (!isObject(raw.effects)) {
          errors.push("effects 必须是对象。");
        } else {
          Object.keys(raw.effects).forEach(function (effectId) {
            var rule = rulesEffectsById[effectId];
            if (!rule) {
              errors.push("未知 effect id：" + effectId + "。");
              return;
            }
            var value = raw.effects[effectId];
            if (!isObject(value)) {
              errors.push("效果 " + effectId + " 的配置必须是对象。");
              return;
            }
            var control = isObject(rule.control) ? rule.control : { kind: "none" };
            var kind = control.kind || "none";
            if (value.enabled !== undefined && kind === "none") {
              errors.push("效果 " + effectId + " 没有用户控制，不能设置 enabled。");
            }
            if (value.enabled !== undefined && typeof value.enabled !== "boolean") {
              errors.push("效果 " + effectId + " 的 enabled 需要布尔值。");
            }
            if (kind === "stacks") {
              var stacks = value.stacks;
              if (stacks !== undefined) {
                if (typeof stacks !== "number" || !isFinite(stacks)) {
                  errors.push("效果 " + effectId + " 的 stacks 需要数字。");
                } else {
                  var limits = boundsOf(control, control.stack_state_id);
                  if (isObject(limits)) {
                    if (limits.min !== undefined && stacks < Number(limits.min)) {
                      errors.push("效果 " + effectId + " 层数低于下限 " + limits.min + "。");
                    }
                    if (limits.max !== undefined && stacks > Number(limits.max)) {
                      errors.push("效果 " + effectId + " 层数超过上限 " + limits.max + "。");
                    }
                  }
                }
              }
            }
            if (
              value.enabled === true &&
              String(rule.support || "") === "needs_action_or_time_simulation" &&
              kind !== "none"
            ) {
              warnings.push("效果 " + effectId + " 标记了动作机制，实际重算仍不支持。");
            }
          });
        }
      }
      if (raw.single_cast !== undefined) {
        if (!isObject(raw.single_cast)) {
          errors.push("single_cast 必须是对象。");
        } else {
          var castKeys = knownCastKeys();
          Object.keys(raw.single_cast).forEach(function (key) {
            if (!castKeys[key]) {
              errors.push("未知单次施法状态：" + key + "。");
              return;
            }
            if (typeof raw.single_cast[key] !== "boolean") {
              errors.push("单次施法状态 " + key + " 需要布尔值。");
            }
          });
        }
      }
      if (raw.loop !== undefined) {
        if (!isObject(raw.loop)) {
          errors.push("loop 必须是对象。");
        } else {
          var loopKeys = knownLoopKeys();
          Object.keys(raw.loop).forEach(function (key) {
            var kind = loopKeys[key];
            if (!kind) {
              warnings.push("未识别的循环字段：" + key + "（已忽略）。");
              return;
            }
            if (kind === "boolean" && typeof raw.loop[key] !== "boolean") {
              errors.push("循环字段 " + key + " 需要布尔值。");
            }
            if (kind === "number" && typeof raw.loop[key] !== "number") {
              errors.push("循环字段 " + key + " 需要数字。");
            }
          });
        }
      }
      return { errors: errors, warnings: warnings };
    }

    function reportImportErrors(errors) {
      var unique = [];
      errors.forEach(function (message) {
        if (unique.indexOf(message) < 0) unique.push(message);
      });
      var head = unique.slice(0, 4).join("；");
      if (unique.length > 4) head += "；…共 " + unique.length + " 处问题";
      showError("导入失败：" + head);
    }

    function importScenario(file) {
      if (!file) return;
      var reader = new FileReader();
      reader.onerror = function () {
        showError("读取文件失败，请重试。");
        if (importInput) importInput.value = "";
      };
      reader.onload = function () {
        var parsed;
        try {
          parsed = JSON.parse(String(reader.result || ""));
        } catch (error) {
          showError("JSON 解析失败：" + String(error && error.message ? error.message : error));
          if (importInput) importInput.value = "";
          return;
        }
        if (window.ReportActions) {
          var envelope = window.ReportActions.validateEnvelope(parsed, {
            currentBinding: currentReportBinding(),
            legacyScenarioValidator: function (candidate) {
              var legacy = validateScenario(candidate);
              return { errors: legacy.errors, warnings: legacy.warnings };
            }
          });
          if (!envelope.ok) {
            reportImportErrors(
              envelope.errors && envelope.errors.length ? envelope.errors : [envelope.error]
            );
            if (importInput) importInput.value = "";
            return;
          }
          if (!envelope.legacy && envelope.scenario) { parsed = envelope.scenario; }
        }
        var validation = validateScenario(parsed);
        if (validation.errors.length) {
          reportImportErrors(validation.errors);
          if (importInput) importInput.value = "";
          return;
        }
        scenario = normalizeScenario(parsed);
        if (!scenario.preset_id) scenario.preset_id = "custom";
        if (isObject(parsed.target)) applyTargetBlock(parsed.target);
        else scenario.target = readTargetBlock();
        scenarioSaved = true;
        bumpScenarioRevision();
        syncControlsFromScenario();
        clearMessages();
        refresh();
        var message = "已载入配置。" + (changedFromBaseline() ? "" : "");
        if (validation.warnings.length) message += "（提示：" + validation.warnings.slice(0, 2).join("；") + "）";
        showStatus(message);
        if (importInput) importInput.value = "";
      };
      reader.readAsText(file, "utf-8");
    }

    /* ------------------------------------------------------------------ */
    /* refresh                                                            */
    /* ------------------------------------------------------------------ */

    function refresh() {
      syncControlsFromScenario();
      statusCache = {};
      rulesEffects.forEach(function (effect) {
        statusCache[effect.effect_id] = computeStatus(effect);
      });
      updateStatusBadges();
      updateStatusDetails();
      updateRows();
      refreshCounts();
      updateConditionChecks();
      updateChangedNotice();
      syncPresetSelect();
      applyFilters();
    }

    /* ------------------------------------------------------------------ */
    /* events                                                             */
    /* ------------------------------------------------------------------ */

    document.addEventListener("change", function (event) {
      var target = event.target;
      if (!target || !target.matches) return;
      if (target.matches("[data-cond-toggle]")) {
        setEffectEnabled(target.getAttribute("data-cond-toggle"), target.checked);
        clearMessages();
        refresh();
        return;
      }
      if (target.matches("[data-cond-stacks]")) {
        setEffectStacks(target.getAttribute("data-cond-stacks"), target.value);
        clearMessages();
        refresh();
        return;
      }
      if (target.matches("[data-cond-state]")) {
        setStateValue(target.getAttribute("data-cond-state"), target);
        clearMessages();
        refresh();
        return;
      }
      if (target.matches("[data-cond-cast]")) {
        setCastState(target.getAttribute("data-cond-cast"), target.checked);
        clearMessages();
        refresh();
        return;
      }
      if (target.matches("[data-cond-category],[data-cond-relevance],[data-cond-support],[data-cond-status]")) {
        applyFilters();
        return;
      }
      if (target.matches("[data-cond-import]")) {
        var file = target.files && target.files[0];
        if (file) importScenario(file);
      }
    });

    page.addEventListener("input", function (event) {
      var target = event.target;
      if (target && target.matches && target.matches("[data-cond-search]")) {
        applyFilters();
      }
    });

    $$("[data-cond-preset-apply]", page).forEach(function (button) {
      button.addEventListener("click", applyPreset);
    });
    $$("[data-cond-reset]", page).forEach(function (button) {
      button.addEventListener("click", resetScenario);
    });
    $$("[data-cond-export]", page).forEach(function (button) {
      button.addEventListener("click", exportScenario);
    });
    $$("[data-cond-live-recompute]", page).forEach(function (button) {
      button.addEventListener("click", liveRecompute);
    });
    // "恢复默认目标" resets target-side states too; level/fire is handled by the
    // shell scenario module.  Target-state inputs now live in the target drawer.
    var targetStateIds = [
      "target.boss_or_rare",
      "target.within_center_1m",
      "target.both_stars_cover",
      "target.chilled",
      "target.frozen"
    ];
    $$("[data-reset-target]").forEach(function (button) {
      button.addEventListener("click", function () {
        targetStateIds.forEach(function (stateId) {
          var baselineStates = isObject(baselineScenario) ? baselineScenario.states : null;
          if (isObject(baselineStates) && has(baselineStates, stateId)) {
            scenario.states[stateId] = baselineStates[stateId];
          } else {
            delete scenario.states[stateId];
          }
        });
        markCustomPreset();
        refresh();
      });
    });
    // R3: target edits are part of the scenario too.  Applying a new target
    // while a recompute request is in flight must invalidate the old response.
    $$("[data-apply-scenario]").forEach(function (button) {
      button.addEventListener("click", bumpScenarioRevision);
    });
    $$("[data-scenario-level], [data-scenario-fire-res]").forEach(function (input) {
      input.addEventListener("change", bumpScenarioRevision);
    });

    /* ------------------------------------------------------------------ */
    /* init                                                               */
    /* ------------------------------------------------------------------ */

    var initialScenario = null;
    if (isObject(conditions.scenario)) {
      // A scenario-applied report carries the generated scenario in the view;
      // it must win over the registered preset baseline so the controls/KPIs
      // show the conditions that were actually applied.
      initialScenario = conditions.scenario;
    } else if (isObject(rules.baseline_scenario)) {
      initialScenario = rules.baseline_scenario;
    }
    if (initialScenario) {
      scenario = normalizeScenario(initialScenario);
      baselineScenario = normalizeScenario(
        isObject(rules.baseline_scenario) ? rules.baseline_scenario : initialScenario
      );
      if (!isObject(scenario.target) || !Object.keys(scenario.target).length) {
        scenario.target = readTargetBlock();
      }
      if (conditions.scenario_applied === true) {
        // The server-rendered scenario-applied report is the current baseline:
        // only edits made after this page load should show "设置已更改".
        baselineScenario = normalizeScenario(initialScenario);
        scenarioSaved = true;
      }
    } else {
      scenario = scenarioFromDom();
      baselineScenario = normalizeScenario(scenario);
    }
    syncControlsFromScenario();
    clearMessages();
    refresh();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initConditionsPage);
  } else {
    initConditionsPage();
  }
})();
