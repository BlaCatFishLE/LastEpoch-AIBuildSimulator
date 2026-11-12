/* D10 shared report actions: config-envelope validation + edit/apply status.

 * Both models provide a small adapter (current bindings + model scenario
 * validator); the envelope rules, codes and status vocabulary live here once.
 */
(function () {
  "use strict";

  var TOP_FIELDS = ["schema_version", "schema", "binding", "scenario"];
  var BINDING_FIELDS = [
    "schema_version", "case_id", "model_id", "capabilities_version",
    "ui_contract_version", "renderer_fingerprint", "report_ref",
    "input_fingerprints", "recompute_endpoint"
  ];

  function isObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }
  function fail(message, code) { return { ok: false, error: message, code: code || "config_invalid" }; }

  function validateBinding(binding, current) {
    if (!isObject(binding)) {
      return fail(
        "旧格式（无绑定），请重新保存为 le-report-config-v1（未修改当前配置）",
        "legacy_config_no_binding"
      );
    }
    for (var key in binding) {
      if (Object.prototype.hasOwnProperty.call(binding, key) && BINDING_FIELDS.indexOf(key) === -1) {
        return fail("配置绑定含未声明字段 " + key, "config_binding_unknown_field");
      }
    }
    var required = ["case_id", "model_id", "capabilities_version", "input_fingerprints", "report_ref"];
    for (var index = 0; index < required.length; index += 1) {
      if (!Object.prototype.hasOwnProperty.call(binding, required[index])) {
        return fail("配置绑定缺少字段 " + required[index], "config_invalid_binding");
      }
    }
    if (!isObject(current) || current.error) {
      return fail("页面绑定不可用", "config_invalid_binding");
    }
    if (binding.case_id !== current.case_id) {
      return fail("配置案例与当前页面不一致", "config_case_mismatch");
    }
    if (binding.model_id !== current.model_id) {
      return fail("配置模型与当前页面不一致", "config_model_mismatch");
    }
    if (binding.capabilities_version !== current.capabilities_version) {
      return fail("配置能力版本与当前页面不一致", "config_capabilities_version_mismatch");
    }
    if (binding.ui_contract_version !== undefined && binding.ui_contract_version !== current.ui_contract_version) {
      return fail("配置 UI 契约版本与当前页面不一致", "config_ui_contract_mismatch");
    }
    if (binding.renderer_fingerprint !== undefined && binding.renderer_fingerprint !== current.renderer_fingerprint) {
      return fail("配置渲染器指纹与当前页面不一致", "config_renderer_fingerprint_mismatch");
    }
    var keys = ["calc_input", "ruleset", "snapshot", "dataset"];
    var fingerprints = binding.input_fingerprints || {};
    var currentFingerprints = current.input_fingerprints || {};
    for (var first = 0; first < keys.length; first += 1) {
      if (fingerprints[keys[first]] !== currentFingerprints[keys[first]]) {
        return fail(
          "配置输入指纹与当前页面不一致（" + keys[first] + "）",
          "config_input_fingerprint_mismatch"
        );
      }
    }
    if (binding.recompute_endpoint !== undefined && current.recompute_endpoint !== undefined &&
        binding.recompute_endpoint !== current.recompute_endpoint) {
      return fail("配置绑定端点与当前页面不一致", "config_report_ref_mismatch");
    }
    var ref = binding.report_ref || {};
    var currentRef = current.report_ref || {};
    var refKeys = [
      "schema_version", "case_id", "model_id", "run_id", "run_dir", "report_path",
      "recompute_endpoint", "ui_contract_version", "renderer_fingerprint",
      "capabilities_path", "canonical"
    ];
    for (var second = 0; second < refKeys.length; second += 1) {
      if (ref[refKeys[second]] !== currentRef[refKeys[second]]) {
        return fail(
          "配置报告标识与当前页面不一致（report_ref." + refKeys[second] + "）",
          "config_report_ref_mismatch"
        );
      }
    }
    var refFingerprints = ref.input_fingerprints || {};
    var currentRefFingerprints = currentRef.input_fingerprints || {};
    for (var third = 0; third < keys.length; third += 1) {
      if (refFingerprints[keys[third]] !== currentRefFingerprints[keys[third]]) {
        return fail(
          "配置报告指纹与当前页面不一致（report_ref.input_fingerprints." + keys[third] + "）",
          "config_report_ref_mismatch"
        );
      }
    }
    return { ok: true, binding: binding };
  }

  function scenarioFieldMap(capabilities) {
    var declarations = {};
    function add(items, kindDefault) {
      for (var index = 0; index < (items || []).length; index += 1) {
        var item = items[index];
        if (!isObject(item)) { continue; }
        var key = item.state_key || item.state_id || item.field;
        if (!key) { continue; }
        declarations[key] = {
          kind: String(item.kind || kindDefault || "object").toLowerCase(),
          min: item.min,
          max: item.max,
          readonly: !!item.readonly
        };
      }
    }
    add(capabilities.buff_controls || [], "toggle");
    add(capabilities.target_fields || [], "object");
    return declarations;
  }

  function checkDeclaredValue(codeFail, name, value, declaration, path) {
    var kind = String((declaration && declaration.kind) || "object").toLowerCase();
    var toggle = kind === "toggle" || kind === "bool" || kind === "boolean";
    var numeric = kind === "stacks" || kind === "int" || kind === "integer" ||
      kind === "number" || kind === "float";
    if (toggle) {
      if (typeof value !== "boolean") {
        return codeFail(path + " 必须是布尔值，不能使用字符串/数值", "invalid_scenario_value");
      }
      return null;
    }
    if (numeric) {
      if (typeof value !== "number" || !isFinite(value) ||
          (kind !== "float" && kind !== "number" && value !== Math.floor(value))) {
        return codeFail(path + " 必须是数值（不得字符串强转）", "invalid_scenario_value");
      }
      if (declaration.min !== undefined && value < Number(declaration.min)) {
        return codeFail(path + "=" + value + " 小于下限 " + declaration.min, "out_of_range");
      }
      if (declaration.max !== undefined && value > Number(declaration.max)) {
        return codeFail(path + "=" + value + " 超出上限 " + declaration.max, "out_of_range");
      }
      if (kind === "stacks" || kind === "int" || kind === "integer") {
        if (typeof declaration.min === "number" && value < declaration.min) {
          return codeFail(path + " 低于层数下限", "invalid_stack_count");
        }
      }
      return null;
    }
    if (kind === "string") {
      if (typeof value !== "string") {
        return codeFail(path + " 必须是字符串", "invalid_scenario_value");
      }
      return null;
    }
    return null;
  }

  function validateStateMap(states, declarations) {
    if (!isObject(states)) { return { error: "states 必须是对象", code: "config_invalid_scenario" }; }
    for (var key in states) {
      if (!Object.prototype.hasOwnProperty.call(states, key)) { continue; }
      var declaration = declarations[key];
      if (!declaration) {
        return { error: "未知场景状态字段 " + key, code: "unknown_scenario_field" };
      }
      if (declaration.readonly) {
        return { error: "状态 " + key + " 为只读，配置不得修改", code: "readonly_state" };
      }
      var problem = checkDeclaredValue(fail, key, states[key], declaration, "states." + key);
      if (problem) { return problem; }
    }
    return { ok: true };
  }

  function validateScenarioPayload(scenario, capabilities) {
    capabilities = isObject(capabilities) ? capabilities : {};
    if (!isObject(scenario)) {
      return fail("场景必须是 JSON 对象", "config_invalid_scenario");
    }
    var declarations = scenarioFieldMap(capabilities);
    var scenarioFields = capabilities.scenario_fields || {};
    var allowed = (scenarioFields.allowed || []).slice();
    if (!allowed.length) {
      allowed = ["baseline_profile", "target", "states", "hit_override", "mode_policy",
        "model_parameters", "single_hit", "single_cast", "dps", "profiles", "mode",
        "assumptions", "schema", "case_id", "binding"];
    }
    var required = (scenarioFields.required || []).slice();
    if (!required.length) { required = ["baseline_profile", "target", "states"]; }
    for (var key in scenario) {
      if (Object.prototype.hasOwnProperty.call(scenario, key) && allowed.indexOf(key) === -1) {
        return fail("未声明的场景字段 " + JSON.stringify(key), "unknown_scenario_field");
      }
    }
    for (var index = 0; index < required.length; index += 1) {
      if (!Object.prototype.hasOwnProperty.call(scenario, required[index])) {
        return fail("完整场景缺少必需字段 " + required[index], "config_invalid_scenario");
      }
    }
    var profiles = capabilities.baseline_profiles || [];
    if (typeof scenario.baseline_profile !== "string" || !scenario.baseline_profile) {
      return fail("baseline_profile 必须是非空字符串", "invalid_baseline_profile");
    }
    if (profiles.length && profiles.indexOf(scenario.baseline_profile) === -1) {
      return fail("baseline_profile 不在能力声明内", "invalid_baseline_profile");
    }
    if (!isObject(scenario.states)) {
      return fail("场景 states 必须是对象", "config_invalid_scenario");
    }
    var stateProblem = validateStateMap(scenario.states, declarations);
    if (!stateProblem.ok) { return fail(stateProblem.error, stateProblem.code); }

    if (!isObject(scenario.target)) {
      return fail("场景 target 必须是对象", "config_invalid_scenario");
    }
    for (var targetKey in scenario.target) {
      if (!Object.prototype.hasOwnProperty.call(scenario.target, targetKey)) { continue; }
      var targetDecl = null;
      for (var fieldIndex = 0; fieldIndex < (capabilities.target_fields || []).length; fieldIndex += 1) {
        var field = capabilities.target_fields[fieldIndex];
        if (isObject(field) && field.field === targetKey) { targetDecl = field; break; }
      }
      if (!targetDecl) {
        return fail("未知目标字段 " + targetKey, "unknown_scenario_field");
      }
      var targetProblem = checkDeclaredValue(fail, targetKey, scenario.target[targetKey],
        { kind: targetDecl.kind, min: targetDecl.min, max: targetDecl.max }, "target." + targetKey);
      if (targetProblem) { return targetProblem; }
    }

    var hitOverride = scenario.hit_override;
    if (hitOverride !== undefined) {
      if (!isObject(hitOverride)) { return fail("hit_override 必须是对象", "config_invalid_scenario"); }
      for (var overrideKey in hitOverride) {
        if (["hits_per_cast", "damage_weights"].indexOf(overrideKey) === -1) {
          return fail("未知 hit_override 字段 " + overrideKey, "unknown_scenario_field");
        }
      }
      if (!hitOverride.hasOwnProperty("hits_per_cast") ||
          typeof hitOverride.hits_per_cast !== "number" ||
          hitOverride.hits_per_cast !== Math.floor(hitOverride.hits_per_cast) ||
          hitOverride.hits_per_cast < 1) {
        return fail("命中次数必须是正整数", "config_invalid_scenario");
      }
      var hitDecl = null;
      for (var paramIndex = 0; paramIndex < (capabilities.model_parameters || []).length; paramIndex += 1) {
        var parameter = capabilities.model_parameters[paramIndex];
        if (isObject(parameter) && parameter.key === "hits_per_cast") { hitDecl = parameter; break; }
      }
      if (hitDecl) {
        var hitProblem = checkDeclaredValue(fail, "hits_per_cast", hitOverride.hits_per_cast,
          { kind: hitDecl.kind || "int", min: hitDecl.min, max: hitDecl.max }, "hit_override.hits_per_cast");
        if (hitProblem) { return hitProblem; }
      }
      if (!Array.isArray(hitOverride.damage_weights) ||
          hitOverride.damage_weights.length !== hitOverride.hits_per_cast) {
        return fail("命中权重数量必须与命中次数一致", "config_invalid_scenario");
      }
      for (var weightIndex = 0; weightIndex < hitOverride.damage_weights.length; weightIndex += 1) {
        var weight = hitOverride.damage_weights[weightIndex];
        if (typeof weight !== "number" || !isFinite(weight) || weight < 0) {
          return fail("命中权重必须是非负数值", "config_invalid_scenario");
        }
      }
    }

    // nested sections may carry hidden states; validate the state maps and preserve them.
    var nestedSections = ["single_hit", "single_cast", "dps", "profiles"];
    for (var nestedIndex = 0; nestedIndex < nestedSections.length; nestedIndex += 1) {
      var nestedKey = nestedSections[nestedIndex];
      var nestedValue = scenario[nestedKey];
      if (nestedValue === undefined) { continue; }
      if (nestedKey === "profiles") {
        if (!isObject(nestedValue)) { return fail("profiles 必须是对象", "config_invalid_scenario"); }
        continue;
      }
      if (!isObject(nestedValue)) { return fail(nestedKey + " 必须是对象", "config_invalid_scenario"); }
      if (nestedValue.states !== undefined) {
        var nestedProblem = validateStateMap(nestedValue.states, declarations);
        if (!nestedProblem.ok) { return fail(nestedProblem.error, nestedProblem.code); }
      }
    }
    return { ok: true, scenario: JSON.parse(JSON.stringify(scenario)) };
  }

  function validateEnvelope(parsed, adapter) {
    adapter = adapter || {};
    if (!isObject(parsed)) { return fail("配置必须是 JSON 对象", "config_not_object"); }
    var schemaVersion = parsed.schema_version;
    if (schemaVersion === undefined && parsed.schema !== undefined) {
      schemaVersion = parsed.schema;
    }
    if (schemaVersion === undefined && adapter.legacyScenarioValidator) {
      var legacy = adapter.legacyScenarioValidator(parsed);
      return {
        ok: !legacy.errors || legacy.errors.length === 0,
        legacy: true,
        error: legacy.errors && legacy.errors.length ? legacy.errors[0] : "",
        errors: (legacy.errors || []),
        warnings: (legacy.warnings || []).concat(["legacy_config_no_binding"]),
        code: legacy.errors && legacy.errors.length ? "config_invalid_scenario" : "",
        scenario: parsed
      };
    }
    for (var key in parsed) {
      if (Object.prototype.hasOwnProperty.call(parsed, key) && TOP_FIELDS.indexOf(key) === -1) {
        return fail("未知配置字段 " + key, "config_unknown_field");
      }
    }
    if (schemaVersion !== "le-report-config-v1") {
      return fail("schema_version 必须是 le-report-config-v1", "config_unsupported_schema");
    }
    var bindingResult = validateBinding(parsed.binding, adapter.currentBinding);
    if (!bindingResult.ok) { return bindingResult; }
    if (!Object.prototype.hasOwnProperty.call(parsed, "scenario")) {
      return fail("配置缺少 scenario", "config_missing_scenario");
    }
    var scenarioResult = adapter.capabilities
      ? validateScenarioPayload(parsed.scenario, adapter.capabilities)
      : { ok: true, scenario: parsed.scenario };
    if (!scenarioResult.ok) { return scenarioResult; }
    return {
      ok: true,
      binding: bindingResult.binding,
      scenario: scenarioResult.scenario,
      legacy: parsed.schema !== undefined,
      warnings: parsed.schema !== undefined ? ["config_schema_field_legacy"] : []
    };
  }

  function setStatus(node, message, state) {
    if (!node) { return; }
    node.textContent = String(message || "");
    if (node.setAttribute) {
      node.setAttribute("data-status-state", String(state || "changed"));
    }
  }

  window.ReportActions = {
    validateEnvelope: validateEnvelope,
    validateBinding: validateBinding,
    validateScenarioPayload: validateScenarioPayload,
    setStatus: setStatus
  };
})();
