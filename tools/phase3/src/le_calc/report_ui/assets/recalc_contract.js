/* Shared local-recompute client for the player report (P3R-R).
 *
 * - reads the service-injected #le-recalc-bindings payload;
 * - builds le-recalc-request-v1 with a per-page client_session_id and a
 *   session-scoped, strictly increasing client_edit_revision (R8);
 * - validates the complete le-recalc-response-v1 binding before applying
 *   anything (R5) and drops stale/older responses;
 * - explicitly clicked recompute works for file:// pages with valid bindings
 *   (R6, 3-A convention); page load itself never sends a request.
 */
(function () {
  "use strict";

  var REQUEST_SCHEMA = "le-recalc-request-v1";
  var RESPONSE_SCHEMA = "le-recalc-response-v1";
  var BINDINGS_ID = "le-recalc-bindings";
  var FINGERPRINT_KEYS = ["calc_input", "ruleset", "snapshot", "dataset"];
  var VIEW_STATE_KEY = "le-recalc-view-state";
  var APPLIED_KEY = "le-recalc-applied";

  function activeValue(selector) {
    var nodes = document.querySelectorAll ? document.querySelectorAll(selector) : [];
    for (var index = 0; index < nodes.length; index += 1) {
      var node = nodes[index];
      if (node.classList && node.classList.contains("is-active")) {
        return node.getAttribute("data-page-btn") || node.getAttribute("data-mode-btn") || node.getAttribute("data-window-btn") || "";
      }
      if (node.getAttribute("aria-selected") === "true" || node.getAttribute("aria-pressed") === "true") {
        return node.getAttribute("data-page-btn") || node.getAttribute("data-mode-btn") || node.getAttribute("data-window-btn") || "";
      }
    }
    return "";
  }

  function captureViewState() {
    var state = {
      page: activeValue("[data-page-btn]"),
      mode: activeValue("[data-mode-btn]"),
      window: activeValue("[data-window-btn]"),
      scrollY: window.pageYOffset || (document.documentElement && document.documentElement.scrollTop) || 0
    };
    try {
      window.sessionStorage.setItem(VIEW_STATE_KEY, JSON.stringify(state));
      window.sessionStorage.setItem(APPLIED_KEY, "1");
    } catch (error) { /* optional */ }
  }

  function clickByValue(selector, attribute, value) {
    if (!value || !document.querySelectorAll) { return; }
    var nodes = document.querySelectorAll(selector);
    for (var index = 0; index < nodes.length; index += 1) {
      if (nodes[index].getAttribute(attribute) === value) {
        try { nodes[index].click(); } catch (error) { /* optional */ }
        return;
      }
    }
  }

  function restoreViewState() {
    var raw = null;
    var applied = false;
    try {
      raw = window.sessionStorage.getItem(VIEW_STATE_KEY);
      applied = window.sessionStorage.getItem(APPLIED_KEY) === "1";
    } catch (error) { raw = null; }
    if (!raw) { return applied; }
    var state = null;
    try { state = JSON.parse(raw); } catch (error) { state = null; }
    if (!state) { return applied; }
    var apply = function () {
      clickByValue("[data-page-btn]", "data-page-btn", state.page);
      clickByValue("[data-mode-btn]", "data-mode-btn", state.mode);
      clickByValue("[data-window-btn]", "data-window-btn", state.window);
      try { window.scrollTo(0, Number(state.scrollY) || 0); } catch (error) { /* optional */ }
    };
    apply();
    if (typeof window.requestAnimationFrame === "function") { window.requestAnimationFrame(apply); }
    else { window.setTimeout(apply, 0); }
    try {
      window.sessionStorage.removeItem(VIEW_STATE_KEY);
      window.sessionStorage.removeItem(APPLIED_KEY);
    } catch (error) { /* optional */ }
    return applied;
  }



  function setRecalcAvailability(available) {
    if (document.body && document.body.setAttribute) {
      document.body.setAttribute("data-recalc-available", available ? "true" : "false");
    }
    var notes = document.querySelectorAll ? document.querySelectorAll("[data-recalc-offline-note]") : [];
    for (var index = 0; index < notes.length; index += 1) {
      if (available) {
        notes[index].hidden = true;
        notes[index].textContent = "";
      }
    }
  }

  function readJsonNode(id) {
    var node = document.getElementById(id);
    if (!node) { return null; }
    try { return JSON.parse(node.textContent || "{}"); } catch (error) { return { error: String(error) }; }
  }

  function randomId() {
    try {
      if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return window.crypto.randomUUID();
      }
    } catch (error) { /* fall through */ }
    return "id-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);
  }

  function deepEqual(left, right) {
    if (left === right) { return true; }
    if (!left || !right || typeof left !== "object" || typeof right !== "object") { return false; }
    if (Array.isArray(left) !== Array.isArray(right)) { return false; }
    var leftKeys = Object.keys(left);
    var rightKeys = Object.keys(right);
    if (leftKeys.length !== rightKeys.length) { return false; }
    for (var index = 0; index < leftKeys.length; index += 1) {
      var key = leftKeys[index];
      if (!Object.prototype.hasOwnProperty.call(right, key)) { return false; }
      if (!deepEqual(left[key], right[key])) { return false; }
    }
    return true;
  }

  function isSha256(value) {
    return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
  }

  function readBindings() {
    var raw = readJsonNode(BINDINGS_ID);
    if (!raw) {
      return { error: "页面缺少 #le-recalc-bindings 绑定数据（需由 le.ps1 serve 注入）" };
    }
    if (raw.error) { return { error: "重算绑定 JSON 无法解析：" + raw.error }; }
    var required = [
      "case_id", "model_id", "capabilities_version", "report_ref",
      "input_fingerprints", "recompute_endpoint"
    ];
    var missing = required.filter(function (key) { return !raw[key]; });
    if (missing.length) { return { error: "重算绑定缺少字段：" + missing.join(", ") }; }
    var ref = raw.report_ref || {};
    if (!ref.run_id || !ref.report_path) { return { error: "report_ref 缺少 run_id/report_path" }; }
    var fingerprints = raw.input_fingerprints || {};
    var bad = FINGERPRINT_KEYS.filter(function (key) { return !isSha256(fingerprints[key]); });
    if (bad.length) { return { error: "输入指纹缺失或非法：" + bad.join(", ") }; }
    try {
      if (!/^https?:\/\/[^/]+:\d{1,5}\/recompute$/.test(String(raw.recompute_endpoint))) {
        return { error: "重算端点不是本机回环 /recompute URL：" + raw.recompute_endpoint };
      }
    } catch (error) { return { error: "重算端点非法" }; }
    return raw;
  }

  function issueMessages(issues) {
    return (Array.isArray(issues) ? issues : []).map(function (issue) {
      if (!issue) { return ""; }
      return String(issue.message || issue.code || "").trim();
    }).filter(function (text) { return text; }).join("；");
  }

  function createController(options) {
    options = options || {};
    var bindings = readBindings();
    var sessionId = randomId();
    var revision = Number.isInteger(options.initialRevision) ? options.initialRevision : 0;
    var lastAcceptedRevision = null;
    var busy = false;
    var pending = null;

    function status(text, tone, state) {
      if (typeof options.onStatus === "function") {
        options.onStatus(String(text || ""), tone || "muted", state || "");
      }
    }
    function issueStatus(issues) {
      status(issueMessages(issues) || "请求被拒绝（未返回 issues）", "error", "error");
    }
    function markEdit() { revision += 1; return revision; }
    function reset() { revision += 1; return revision; }
    function revisionValue() { return revision; }
    function sessionValue() { return sessionId; }

    function endpointUrl() {
      if (bindings && bindings.recompute_endpoint) { return String(bindings.recompute_endpoint); }
      var meta = document.querySelector ? document.querySelector('meta[name="le-recalc-endpoint"]') : null;
      return meta ? String(meta.getAttribute("content") || "") : "";
    }

    function buildRequest(command, includeSession) {
      var request = {
        schema_version: REQUEST_SCHEMA,
        request_id: randomId(),
        client_edit_revision: revision,
        case_id: String(bindings.case_id),
        model_id: String(bindings.model_id),
        capabilities_version: String(bindings.capabilities_version),
        report_ref: bindings.report_ref,
        input_fingerprints: {
          calc_input: bindings.input_fingerprints.calc_input,
          ruleset: bindings.input_fingerprints.ruleset,
          snapshot: bindings.input_fingerprints.snapshot,
          dataset: bindings.input_fingerprints.dataset
        },
        scenario: options.getScenario ? options.getScenario() : {},
        command: command
      };
      if (includeSession) { request.client_session_id = sessionId; }
      return request;
    }

    function acceptedRevisionOf(payload, requestRevision) {
      var accepted = payload && payload.accepted_revision;
      if (typeof accepted !== "number" || !isFinite(accepted) || accepted !== Math.floor(accepted) || accepted < 0) {
        return { error: "响应缺少合法的 accepted_revision" };
      }
      if (accepted < requestRevision) {
        return { error: "响应 accepted_revision 小于请求编辑序号（旧响应）" };
      }
      return { value: accepted };
    }

    function validateResponse(payload, request) {
      var problems = [];
      if (!payload || typeof payload !== "object") { return ["响应不是 JSON 对象"]; }
      if (payload.schema_version !== RESPONSE_SCHEMA) { problems.push("schema_version 不匹配"); }
      if (payload.request_id !== request.request_id) { problems.push("request_id 不匹配"); }
      if (payload.client_edit_revision !== request.client_edit_revision) {
        problems.push("client_edit_revision 不匹配");
      }
      var accepted = acceptedRevisionOf(payload, request.client_edit_revision);
      if (accepted.error) { problems.push(accepted.error); }
      if (payload.accepted !== true) { problems.push("accepted 不是 true"); }
      if (payload.case_id !== request.case_id) { problems.push("case_id 不匹配"); }
      if (payload.model_id !== request.model_id) { problems.push("model_id 不匹配"); }
      if (payload.capabilities_version !== request.capabilities_version) {
        problems.push("capabilities_version 不匹配");
      }
      if (!deepEqual(payload.input_fingerprints, request.input_fingerprints)) {
        problems.push("input_fingerprints 不匹配");
      }
      if (!deepEqual(payload.report_ref, request.report_ref)) {
        problems.push("report_ref 不匹配");
      }
      if (String(payload.recompute_endpoint || "") !== String(bindings.recompute_endpoint || "")) {
        problems.push("recompute_endpoint 不匹配");
      }
      if (payload.client_session_id !== undefined) {
        if (payload.client_session_id !== sessionId) {
          problems.push("client_session_id 不匹配");
        }
        if (payload.client_session_legacy === true) {
          problems.push("服务把本页面会话当作 legacy 会话");
        }
      }
      if (payload.accepted_revision !== request.client_edit_revision) {
        problems.push("accepted_revision 与本次编辑序号不一致");
      }
      if (typeof payload.html !== "string" || !payload.html) {
        problems.push("缺少计算后的 HTML");
      }
      return problems;
    }

    function sendRequest(request) {
      var endpoint = endpointUrl();
      return fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request)
      });
    }

    function compute(command) {
      command = command || "compute";
      if (bindings.error) {
        status(bindings.error + "；无法连接本机服务。", "error");
        return Promise.resolve({ accepted: false, error: bindings.error });
      }
      var endpoint = endpointUrl();
      if (!endpoint || endpoint.indexOf("__LE_") === 0) {
        status("本机重算未启用：这是离线报告，启动本机服务后从服务页面计算。", "muted");
        return Promise.resolve({ accepted: false, offline: true });
      }
      // Every explicit compute is a newer client generation for this page session.
      revision += 1;
      var request = buildRequest(command, true);
      var requestRevision = request.client_edit_revision;
      var requestIdValue = request.request_id;
      busy = true;
      pending = { requestId: requestIdValue, revision: requestRevision };
      if (typeof options.onBusy === "function") { options.onBusy(true); }
      status("计算中…", "muted", "busy");

      function finish() {
        busy = false;
        pending = null;
        if (typeof options.onBusy === "function") { options.onBusy(false); }
      }

      function handleRejection(payload) {
        issueStatus(payload && payload.issues);
        finish();
        return payload;
      }

      return sendRequest(request)
        .then(function (response) {
          return response.text().then(function (text) {
            var payload = null;
            try { payload = JSON.parse(text); } catch (error) {
              throw new Error("服务返回非 JSON 响应（HTTP " + response.status + "）");
            }
            if (payload && response.status === 422) {
              // transitional compatibility: current servers may not know
              // client_session_id yet; retry once without it (same revision).
              var messages = (payload.issues || []).map(function (issue) {
                return String(issue.message || "");
              }).join(" ");
              if (messages.indexOf("client_session_id") !== -1 && request.client_session_id) {
                var retry = buildRequest(command, false);
                retry.request_id = randomId();
                return sendRequest(retry).then(function (retryResponse) {
                  return retryResponse.text().then(function (retryText) {
                    return processPayload(JSON.parse(retryText), retry, retryResponse);
                  });
                });
              }
            }
            return processPayload(payload, request, response);
          });
        })
        .catch(function (error) {
          if (!bindings || !bindings.error) { setRecalcAvailability(true); }
          status(
            "计算失败：" + String((error && error.message) || error) + "（已保留当前编辑与旧结果）",
            "error",
            "error"
          );
          finish();
          return { accepted: false, error: String((error && error.message) || error) };
        });

      function processPayload(payload, activeRequest, response) {
        if (!payload || payload.accepted !== true) {
          return handleRejection(payload);
        }
        var problems = validateResponse(payload, activeRequest);
        if (problems.length) {
          status("已丢弃响应：" + problems.join("；") + "（已保留当前编辑与旧结果）", "error");
          finish();
          return payload;
        }
        if (revision !== activeRequest.client_edit_revision) {
          status("设置已更改：已忽略旧结果，请重新计算。", "muted", "changed");
          finish();
          return payload;
        }
        var accepted = acceptedRevisionOf(payload, activeRequest.client_edit_revision).value;
        if (lastAcceptedRevision !== null && accepted <= lastAcceptedRevision) {
          status("已丢弃过期响应（accepted_revision 未前进）。", "muted");
          finish();
          return payload;
        }
        lastAcceptedRevision = accepted;
        if (accepted > revision) { revision = accepted; }
        pending = null;
        busy = false;
        if (typeof options.onBusy === "function") { options.onBusy(false); }
        if (command === "compute" && payload.html) {
          captureViewState();
          setRecalcAvailability(true);
          status("已应用本次计算结果。", "muted", "applied");
          if (typeof options.onAccepted === "function") { options.onAccepted(payload); }
        } else {
          status("配置已应用（未变更当前结果）。", "muted", "applied");
          if (typeof options.onPrepared === "function") { options.onPrepared(payload); }
        }
        return payload;
      }
    }

    var restoredApplied = restoreViewState();
    window.__leRecalcRestoredApplied = !!restoredApplied;
    if (restoredApplied) {
      // Synchronous: the acceptance snapshot may inspect the restored page
      // immediately after document.write, before any timer callback.
      status("已应用（本次计算结果）。", "muted", "applied");
    }

    return {
      markEdit: markEdit,
      reset: reset,
      compute: compute,
      revision: revisionValue,
      sessionId: sessionValue,
      bindings: function () { return bindings; },
      endpoint: endpointUrl,
      busy: function () { return busy; },
      pending: function () { return pending; }
    };
  }

  window.LeRecalc = {
    createController: createController,
    readBindings: readBindings,
    requestSchema: REQUEST_SCHEMA,
    responseSchema: RESPONSE_SCHEMA,
    fingerprintKeys: FINGERPRINT_KEYS
  };
})();
