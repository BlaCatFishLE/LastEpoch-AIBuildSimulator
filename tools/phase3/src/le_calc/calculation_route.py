"""共享计算路径解析与编译输入加载（第三步正式入口）。

CLI 与 workflow 必须调用同一实现；本模块不得 import ``scripts``。
路由结果纯 JSON；统一 plan 来源只有 ``profile_v2`` 与 ``definitions_v2``，未知来源
不回落旧计算器。旧 black-hole/hit 只在**显式**旧来源时进入 legacy 隔离分支。
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from . import calculation_plan as planmod
from . import plan_compiler
from . import plan_handlers

SCHEMA = "le-calculation-route-v1"
ENGINE_UNIFIED = "unified_dps"
ENGINE_LEGACY = "legacy"
ENGINE_UNSUPPORTED = "unsupported"
ENGINE_NEEDS_RESEARCH = "needs_research"
ENGINE_NEEDS_INPUT = "needs_input"
SOURCE_PROFILE_V2 = "profile_v2"
SOURCE_DEFINITIONS_V2 = "definitions_v2"
SOURCE_DEFINITIONS_V3 = "definitions_v3"
ENGINE_VALUES = (
    ENGINE_UNIFIED,
    ENGINE_LEGACY,
    ENGINE_UNSUPPORTED,
    ENGINE_NEEDS_RESEARCH,
    ENGINE_NEEDS_INPUT,
)
SOURCE_VALUES = (SOURCE_PROFILE_V2, SOURCE_DEFINITIONS_V2, SOURCE_DEFINITIONS_V3)
TEMPLATE_SCHEMA = "le-skill-template-v2"
LEGACY_MODELS = ("black-hole", "hit")
UNIFIED_COMPATIBLE_MODELS = ("", "research", "dps", "unified_dps", "unified")
DEFINITIONS_GLOB = "knowledge/definitions/*.json"
DEFINITION_CATALOG = "knowledge/definitions/index.json"


def _diagnostic(code: str, message: str, **extra: Any) -> dict[str, Any]:
    record = {"code": code, "message": message, "severity": "error"}
    record.update(extra)
    return record


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _case_model(case: dict[str, Any]) -> str:
    return str(case.get("model") or "").strip().lower()


def _main_skill(case: dict[str, Any]) -> str:
    return str((case.get("main_skill") or {}).get("ability_id") or "").strip()


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_file_ref(path: str, root: Path, *, role: str = "template", schema_version: Optional[str] = None) -> dict[str, Any]:
    absolute = root / path
    if not absolute.is_file():
        raise FileNotFoundError(path)
    return {
        "role": role,
        "path": _relative(absolute, root),
        "sha256": _sha256_file(absolute),
        "schema_version": schema_version,
    }


def _resolve_definitions_source(
    source: dict[str, Any], root: Path, case: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """解析 definitions_v2 文件引用；只校验存在/类型，schema 与 ability 在加载时校验。"""
    diagnostics: list[dict[str, Any]] = []
    template_path = str(source.get("template") or "").strip()
    if not template_path:
        diagnostics.append(_diagnostic("missing_template", "definitions_v2 来源缺少 template 路径"))
        return {}, diagnostics
    absolute = root / template_path
    if not absolute.is_file():
        diagnostics.append(
            _diagnostic("missing_template_file", f"原生定义模板文件不存在：{template_path}")
        )
        return {}, diagnostics
    refs = [
        _source_file_ref(template_path, root, role="template", schema_version=TEMPLATE_SCHEMA)
    ]
    mechanisms = source.get("mechanisms") or []
    rules = source.get("rules") or []
    if not isinstance(mechanisms, list) or not isinstance(rules, list):
        diagnostics.append(
            _diagnostic("invalid_source_files", "mechanisms/rules 必须是仓库相对文件路径数组")
        )
        return {}, diagnostics
    for role, values in (("mechanism_library", mechanisms), ("rule_file", rules)):
        for value in values:
            text = str(value or "").strip()
            if not text:
                diagnostics.append(_diagnostic("invalid_source_file", f"{role} 含空路径"))
                continue
            if not (root / text).is_file():
                diagnostics.append(
                    _diagnostic("missing_source_file", f"{role} 文件不存在：{text}")
                )
                continue
            refs.append(_source_file_ref(text, root, role=role))
    ability = _main_skill(case)
    if not ability:
        diagnostics.append(_diagnostic("missing_main_skill", "原生定义来源需要 case.main_skill.ability_id"))
    else:
        try:
            template = json.loads(absolute.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            diagnostics.append(
                _diagnostic("invalid_template_json", f"模板不可读：{template_path}（{exc}）")
            )
            return {}, diagnostics
        if template.get("schema") != TEMPLATE_SCHEMA:
            diagnostics.append(
                _diagnostic(
                    "unsupported_template_schema",
                    f"模板 schema 不是 {TEMPLATE_SCHEMA}：{template.get('schema')!r}",
                )
            )
        if str(template.get("ability_id") or "") != ability:
            diagnostics.append(
                _diagnostic(
                    "template_ability_mismatch",
                    f"模板 ability_id={template.get('ability_id')!r} 与主技能 {ability!r} 不一致",
                )
            )
    return {
        "source_kind": SOURCE_DEFINITIONS_V2,
        "template": refs[0]["path"],
        "template_sha256": refs[0]["sha256"],
        "mechanisms": [ref["path"] for ref in refs if ref["role"] == "mechanism_library"],
        "rules": [ref["path"] for ref in refs if ref["role"] == "rule_file"],
        "input_refs": refs,
    }, diagnostics


def _resolve_definitions_v3_source(
    source: dict[str, Any], root: Path, case: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """解析 definitions_v3：template 引用 + schema/ability 预校验。"""
    from . import definitions_v3  # noqa: PLC0415

    diagnostics: list[dict[str, Any]] = []
    template_path = str(source.get("template") or "").strip()
    if not template_path:
        return {}, [_diagnostic("missing_template", "definitions_v3 来源缺少 template 路径")]
    absolute = root / template_path
    if not absolute.is_file():
        return {}, [_diagnostic("missing_template_file", f"v3 定义模板文件不存在：{template_path}")]
    try:
        template = json.loads(absolute.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [_diagnostic("invalid_template_json", f"v3 模板不可读：{template_path}（{exc}）")]
    if template.get("schema") != definitions_v3.SCHEMA:
        diagnostics.append(
            _diagnostic("unsupported_template_schema", f"模板 schema 不是 {definitions_v3.SCHEMA}")
        )
    if str(template.get("binding_version") or "") != definitions_v3.BINDING_VERSION:
        diagnostics.append(
            _diagnostic("unsupported_binding_version", f"binding_version 不支持：{template.get('binding_version')!r}")
        )
    scenario_path = str(source.get("scenario") or "").strip()
    scenarios_declared = source.get("scenarios")
    scenario_entries: list[dict[str, str]] = []
    if scenarios_declared is not None:
        if scenario_path:
            diagnostics.append(
                _diagnostic("conflicting_scenarios", "definitions_v3 不能同时声明 scenario 与 scenarios")
            )
        if not isinstance(scenarios_declared, list) or not scenarios_declared:
            diagnostics.append(_diagnostic("invalid_scenarios", "scenarios 必须是非空数组"))
        else:
            seen_ids: set[str] = set()
            seen_paths: set[str] = set()
            for index, item in enumerate(scenarios_declared):
                if not isinstance(item, dict):
                    diagnostics.append(_diagnostic("invalid_scenarios", f"scenarios[{index}] 必须是对象"))
                    continue
                evaluation_id = str(item.get("evaluation_id") or "").strip()
                path = str(item.get("path") or "").strip()
                if not evaluation_id or not path:
                    diagnostics.append(
                        _diagnostic("invalid_scenarios", f"scenarios[{index}] 缺少 evaluation_id/path")
                    )
                    continue
                if evaluation_id in seen_ids:
                    diagnostics.append(
                        _diagnostic("invalid_scenarios", f"scenario evaluation_id 重复：{evaluation_id}")
                    )
                    continue
                if path in seen_paths:
                    diagnostics.append(_diagnostic("invalid_scenarios", f"scenario 路径重复：{path}"))
                    continue
                seen_ids.add(evaluation_id)
                seen_paths.add(path)
                if not (root / path).is_file():
                    diagnostics.append(_diagnostic("missing_scenario_file", f"scenario 文件不存在：{path}"))
                    continue
                scenario_entries.append({"evaluation_id": evaluation_id, "path": path})
    elif not scenario_path:
        diagnostics.append(_diagnostic("missing_scenario", "definitions_v3 来源缺少 scenario 路径"))
    elif not (root / scenario_path).is_file():
        diagnostics.append(_diagnostic("missing_scenario_file", f"scenario 文件不存在：{scenario_path}"))
    conversion = ((template.get("definition") or {}).get("attribute_conversion") or {})
    rules_path = str(conversion.get("rules") or "") if isinstance(conversion, dict) else ""
    if rules_path and not (root / rules_path).is_file():
        diagnostics.append(
            _diagnostic("missing_attribute_conversion_rules", f"属性转换规则文件不存在：{rules_path}")
        )
    ability = _main_skill(case)
    if not ability:
        diagnostics.append(_diagnostic("missing_main_skill", "definitions_v3 需要 case.main_skill.ability_id"))
    elif str(template.get("ability_id") or "") != ability:
        diagnostics.append(
            _diagnostic(
                "template_ability_mismatch",
                f"模板 ability_id={template.get('ability_id')!r} 与主技能 {ability!r} 不一致",
            )
        )
    ref = _source_file_ref(template_path, root, role="template", schema_version=definitions_v3.SCHEMA)
    refs = [ref]
    buff_library = (template.get("definition") or {}).get("conditional_buff_library")
    if buff_library:
        refs.append(_source_file_ref(buff_library, root, role="rule_file", schema_version="le-conditional-node-buffs-v1"))
        buff_config=json.loads((root/buff_library).read_text(encoding="utf-8"))
        item_library=buff_config.get("item_buff_library")
        if item_library:
            if (root/item_library).is_file():
                refs.append(_source_file_ref(item_library, root, role="rule_file", schema_version="le-conditional-item-buffs-v1"))
            else:
                diagnostics.append(_diagnostic("missing_item_buff_library", f"装备条件 Buff 库不存在：{item_library}"))
    if scenario_path:
        refs.append(_source_file_ref(scenario_path, root, role="scenario", schema_version=definitions_v3.SCENARIO_SCHEMA))
    for entry in scenario_entries:
        refs.append(
            _source_file_ref(entry["path"], root, role="scenario", schema_version=definitions_v3.SCENARIO_SCHEMA)
        )
    if rules_path:
        refs.append(_source_file_ref(rules_path, root, role="attribute_conversion_rules"))
    return {
        "source_kind": SOURCE_DEFINITIONS_V3,
        "template": ref["path"],
        "sha256": ref["sha256"],
        "scenario": scenario_path or None,
        "scenarios": scenario_entries,
        "attribute_conversion_rules": rules_path,
        "input_refs": refs,
    }, diagnostics


def _resolve_profile_source(
    source: dict[str, Any], root: Path, *, origin: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = str(source.get("path") or "").strip()
    if not path:
        return {}, [_diagnostic("missing_profile_path", "profile_v2 来源缺少 path")]
    absolute = root / path
    if not absolute.is_file():
        return {}, [_diagnostic("missing_profile_file", f"profile 文件不存在：{path}")]
    ref = _source_file_ref(path, root, role="profile", schema_version="le-dps-profile-v2")
    return {
        "source_kind": SOURCE_PROFILE_V2,
        "path": ref["path"],
        "sha256": ref["sha256"],
        "input_refs": [ref],
        "origin": origin,
    }, []


def _definition_catalog_entries(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    path = root / DEFINITION_CATALOG
    if not path.is_file():
        return {}, {"code": "missing_definition_catalog", "message": "缺少 knowledge/definitions/index.json", "severity": "warning"}
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, {"code": "bad_definition_catalog", "message": f"定义目录不可解析：{exc}", "severity": "warning"}
    if catalog.get("schema") != "le-definition-catalog-v1" or not isinstance(catalog.get("entries"), list):
        return {}, {"code": "bad_definition_catalog", "message": "定义目录 schema 不正确", "severity": "warning"}
    entries = {
        entry["path"]: entry
        for entry in catalog["entries"]
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    return entries, None


def discover_definition_candidates(
    case: dict[str, Any], *, root: str | Path
) -> dict[str, Any]:
    """扫描共享 definitions 目录并做适用性判定；不按文件名/技能 ID 直接选模板。"""
    from . import definitions_v3  # noqa: PLC0415

    root_path = Path(root).resolve()
    ability = _main_skill(case)
    report: dict[str, Any] = {
        "schema": "le-definition-candidates-v1",
        "ability_id": ability or None,
        "status": "missing_main_skill",
        "scanned": 0,
        "candidates": [],
        "bad_templates": [],
        "other_ability": [],
        "diagnostics": [],
    }
    if not ability:
        return report
    catalog_by_path, catalog_diagnostic = _definition_catalog_entries(root_path)
    if catalog_diagnostic:
        report["diagnostics"].append(catalog_diagnostic)
    for path in sorted(root_path.glob(DEFINITIONS_GLOB)):
        if path == root_path / DEFINITION_CATALOG:
            continue
        rel = _relative(path, root_path)
        report["scanned"] += 1
        try:
            raw = path.read_bytes()
            document = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            report["bad_templates"].append(
                {"path": rel, "status": "bad_template", "reason": f"模板不可解析：{exc}"}
            )
            continue
        sha256 = hashlib.sha256(raw).hexdigest()
        if not isinstance(document, dict):
            report["bad_templates"].append(
                {"path": rel, "status": "bad_template", "reason": "模板必须是 JSON 对象"}
            )
            continue
        if document.get("schema") != definitions_v3.SCHEMA:
            report["bad_templates"].append(
                {
                    "path": rel,
                    "status": "bad_template",
                    "reason": f"模板 schema 不是 {definitions_v3.SCHEMA}：{document.get('schema')!r}",
                }
            )
            continue
        catalog_entry = catalog_by_path.get(rel) or {}
        metadata = dict(document)
        metadata["preferred"] = bool(catalog_entry.get("preferred"))
        metadata["capabilities"] = list(
            catalog_entry.get("capabilities") or document.get("capabilities") or []
        )
        metadata["applicability"] = dict(
            catalog_entry.get("applicability") or document.get("applicability") or {}
        )
        try:
            definitions_v3.validate_template(metadata)
        except definitions_v3.DefinitionBindingError as exc:
            report["bad_templates"].append(
                {"path": rel, "status": "bad_template", "reason": f"模板结构非法：{exc}"}
            )
            continue
        template_ability = str(document.get("ability_id") or "")
        if template_ability != ability:
            report["other_ability"].append(
                {"path": rel, "template_id": document.get("template_id"), "ability_id": template_ability}
            )
            continue
        definition = document.get("definition") or {}
        plan_version = str(definition.get("plan_version") or "le-calculation-plan-v1")
        declared = [str(item) for item in (metadata.get("capabilities") or [])]
        metadata_missing = not catalog_entry and not document.get("capabilities")
        unknown_capabilities = sorted(set(declared) - set(definitions_v3.KNOWN_CAPABILITIES))
        unsupported = sorted(
            set(declared) - set(definitions_v3.SUPPORTED_CAPABILITIES.get(plan_version, frozenset()))
        )
        applicability = definitions_v3.check_applicability(metadata, case)
        if metadata_missing:
            status = "undetermined"
            reasons = ["缺少候选目录元数据（capabilities/applicability）"]
        elif unknown_capabilities:
            status = "capability_gap"
            reasons = [f"未知能力 {unknown_capabilities}"]
        elif unsupported:
            status = "capability_gap"
            reasons = [f"能力 {unsupported} 不受 {plan_version} 支持"]
        elif applicability["status"] == "not_applicable":
            status = "not_applicable"
            reasons = list(applicability["reasons"])
        elif applicability["status"] == "undetermined":
            status = "undetermined"
            reasons = [f"缺少适用性事实：{', '.join(applicability['missing_facts'])}"]
        else:
            status = "applicable"
            reasons = []
        report["candidates"].append(
            {
                "path": rel,
                "template_id": document.get("template_id"),
                "schema": document.get("schema"),
                "binding_version": document.get("binding_version"),
                "ability_id": template_ability,
                "plan_version": plan_version,
                "revision": catalog_entry.get("revision") or f"sha256:{sha256[:16]}",
                "sha256": sha256,
                "catalog_path": DEFINITION_CATALOG if catalog_entry else None,
                "capabilities": declared,
                "supported_capabilities": sorted(
                    definitions_v3.SUPPORTED_CAPABILITIES.get(plan_version, frozenset())
                ),
                "applicability": applicability["applicability"],
                "missing_facts": applicability["missing_facts"],
                "status": status,
                "reasons": reasons,
                "preferred": bool(catalog_entry.get("preferred")),
            }
        )
    applicable = [item for item in report["candidates"] if item["status"] == "applicable"]
    undetermined = [item for item in report["candidates"] if item["status"] == "undetermined"]
    capability_gaps = [item for item in report["candidates"] if item["status"] == "capability_gap"]
    preferred = [item for item in applicable if item.get("preferred")]
    if len(preferred) == 1:
        report["status"] = "applicable_single"
        report["selected"] = preferred[0]
    elif len(applicable) == 1:
        report["status"] = "applicable_single"
        report["selected"] = applicable[0]
    elif len(applicable) > 1:
        report["status"] = "applicable_multiple"
    elif undetermined:
        report["status"] = "undetermined"
    elif capability_gaps:
        report["status"] = "capability_gap"
    elif report["candidates"]:
        report["status"] = "not_applicable"
    elif report["bad_templates"]:
        report["status"] = "bad_template"
    else:
        report["status"] = "no_candidate"
    if report["bad_templates"]:
        report["diagnostics"].append(
            {
                "code": "bad_definition_template",
                "message": f"{len(report['bad_templates'])} 份模板不可用",
                "severity": "warning",
                "paths": [item["path"] for item in report["bad_templates"]],
            }
        )
    if report["status"] == "applicable_multiple":
        report["diagnostics"].append(
            {
                "code": "multiple_definition_candidates",
                "message": f"主技能 {ability} 有 {len(applicable)} 份适用 v3 定义，需显式选择",
                "severity": "warning",
            }
        )
    elif report["status"] == "undetermined":
        report["diagnostics"].append(
            {
                "code": "definition_candidate_undetermined",
                "message": "候选定义存在，但缺少判定适用性的稳定事实",
                "severity": "warning",
            }
        )
    elif report["status"] == "capability_gap":
        report["diagnostics"].append(
            {
                "code": "capability_gap",
                "message": "候选定义声明当前引擎不支持的能力",
                "severity": "warning",
            }
        )
    elif report["status"] == "not_applicable":
        report["diagnostics"].append(
            {
                "code": "no_applicable_definition",
                "message": f"主技能 {ability} 的候选定义均不适用当前实例",
                "severity": "warning",
            }
        )
    return report


def resolve_calculation_route(
    case: dict[str, Any], *, root: str | Path, explicit: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """共享路由解析；返回 ``le-calculation-route-v1`` 纯 JSON。"""
    root_path = Path(root).resolve()
    explicit = dict(explicit or {})
    calculation = case.get("calculation")
    if calculation is not None and not isinstance(calculation, dict):
        return {
            "schema": SCHEMA,
            "engine": ENGINE_UNSUPPORTED,
            "status": "unsupported_route",
            "route_id": None,
            "source": None,
            "case_ref": str(case.get("_path") or ""),
            "diagnostics": [_diagnostic("invalid_calculation", "case.calculation 必须是对象")],
        }
    calculation = calculation or {}
    engine = str(calculation.get("engine") or explicit.get("engine") or "").strip()
    declared_source = calculation.get("source") if isinstance(calculation.get("source"), dict) else {}
    if explicit.get("source"):
        declared_source = {"kind": explicit.get("source_kind") or declared_source.get("kind"), **(explicit.get("source") or {})}
    if explicit.get("engine"):
        engine = str(explicit["engine"])
    model = _case_model(case)
    dp_profile = str(case.get("dps_profile") or "").strip()
    legacy_requested = engine == ENGINE_LEGACY or model in LEGACY_MODELS
    unified_declared = engine == ENGINE_UNIFIED or bool(declared_source)
    diagnostics: list[dict[str, Any]] = []

    # 未知 engine / source kind 不回落。
    if engine and engine not in ("", ENGINE_UNIFIED, ENGINE_LEGACY):
        return {
            "schema": SCHEMA,
            "engine": ENGINE_UNSUPPORTED,
            "status": "unsupported_route",
            "route_id": None,
            "source": None,
            "case_ref": str(case.get("_path") or ""),
            "diagnostics": [
                _diagnostic("unknown_engine", f"未知计算 engine：{engine!r}；不回落旧计算器")
            ],
        }
    source_kind = str(declared_source.get("kind") or "").strip()
    if source_kind and source_kind not in SOURCE_VALUES:
        return {
            "schema": SCHEMA,
            "engine": ENGINE_UNSUPPORTED,
            "status": "unsupported_route",
            "route_id": None,
            "source": {"kind": source_kind},
            "case_ref": str(case.get("_path") or ""),
            "diagnostics": [
                _diagnostic(
                    "unsupported_source_kind",
                    f"未知计算来源 kind：{source_kind!r}（支持 {', '.join(SOURCE_VALUES)}）",
                )
            ],
        }

    # 显式 unified 来源与显式旧 model 冲突；两份不同 unified 来源也冲突。
    if unified_declared and model in LEGACY_MODELS:
        return {
            "schema": SCHEMA,
            "engine": ENGINE_UNSUPPORTED,
            "status": "route_conflict",
            "route_id": None,
            "source": {"kind": source_kind or None},
            "case_ref": str(case.get("_path") or ""),
            "diagnostics": [
                _diagnostic(
                    "legacy_unified_conflict",
                    f"case.model={model!r} 是显式旧计算器声明，同时存在统一来源；"
                    "禁止静默选择其一，请删除冲突声明后重试",
                )
            ],
        }

    if unified_declared:
        if source_kind == SOURCE_PROFILE_V2:
            source_doc = {k: v for k, v in declared_source.items() if k != "kind"}
            resolved, problems = _resolve_profile_source(source_doc, root_path, origin="explicit_calculation")
            if dp_profile and str(resolved.get("path") or "") != dp_profile:
                problems.append(
                    _diagnostic(
                        "profile_source_conflict",
                        f"case.calculation.source.path={resolved.get('path')!r} 与 "
                        f"case.dps_profile={dp_profile!r} 不一致",
                    )
                )
            diagnostics.extend(problems)
            if diagnostics:
                return {
                    "schema": SCHEMA,
                    "engine": ENGINE_UNSUPPORTED,
                    "status": "route_conflict",
                    "route_id": None,
                    "source": {"kind": source_kind},
                    "case_ref": str(case.get("_path") or ""),
                    "diagnostics": diagnostics,
                }
            return {
                "schema": SCHEMA,
                "engine": ENGINE_UNIFIED,
                "status": "ready",
                "route_id": "unified:profile_v2",
                "source": resolved,
                "case_ref": str(case.get("_path") or ""),
                "origin": "explicit_calculation",
                "diagnostics": [],
            }
        if source_kind == SOURCE_DEFINITIONS_V2:
            resolved, problems = _resolve_definitions_source(declared_source, root_path, case)
            diagnostics.extend(problems)
            if diagnostics:
                status = "needs_input" if any(
                    item.get("code") in ("missing_template", "missing_template_file", "missing_main_skill")
                    for item in diagnostics
                ) else "route_conflict"
                return {
                    "schema": SCHEMA,
                    "engine": ENGINE_NEEDS_INPUT if status == "needs_input" else ENGINE_UNSUPPORTED,
                    "status": status,
                    "route_id": None,
                    "source": {"kind": source_kind},
                    "case_ref": str(case.get("_path") or ""),
                    "diagnostics": diagnostics,
                }
            return {
                "schema": SCHEMA,
                "engine": ENGINE_UNIFIED,
                "status": "ready",
                "route_id": "unified:definitions_v2",
                "source": resolved,
                "case_ref": str(case.get("_path") or ""),
                "origin": "explicit_calculation",
                "diagnostics": [],
            }
        if source_kind == SOURCE_DEFINITIONS_V3:
            resolved, problems = _resolve_definitions_v3_source(declared_source, root_path, case)
            diagnostics.extend(problems)
            if diagnostics:
                status = "needs_input" if any(
                    item.get("code") in ("missing_template", "missing_template_file", "missing_main_skill")
                    for item in diagnostics
                ) else "route_conflict"
                return {
                    "schema": SCHEMA,
                    "engine": ENGINE_NEEDS_INPUT if status == "needs_input" else ENGINE_UNSUPPORTED,
                    "status": status,
                    "route_id": None,
                    "source": {"kind": source_kind},
                    "case_ref": str(case.get("_path") or ""),
                    "diagnostics": diagnostics,
                }
            return {
                "schema": SCHEMA,
                "engine": ENGINE_UNIFIED,
                "status": "ready",
                "route_id": "unified:definitions_v3",
                "source": resolved,
                "case_ref": str(case.get("_path") or ""),
                "origin": "explicit_calculation",
                "diagnostics": [],
            }
        # engine=unified_dps 但缺 source：若仅旧 dps_profile 可按兼容别名归一。
        if dp_profile:
            resolved, problems = _resolve_profile_source(
                {"path": dp_profile}, root_path, origin="legacy_dps_profile_compat"
            )
            if problems:
                return {
                    "schema": SCHEMA,
                    "engine": ENGINE_UNSUPPORTED,
                    "status": "route_conflict",
                    "route_id": None,
                    "source": None,
                    "case_ref": str(case.get("_path") or ""),
                    "diagnostics": problems,
                }
            return {
                "schema": SCHEMA,
                "engine": ENGINE_UNIFIED,
                "status": "ready",
                "route_id": "unified:profile_v2",
                "source": resolved,
                "case_ref": str(case.get("_path") or ""),
                "origin": "legacy_dps_profile_compat",
                "diagnostics": [],
            }
        return {
            "schema": SCHEMA,
            "engine": ENGINE_NEEDS_INPUT,
            "status": "needs_input",
            "route_id": None,
            "source": None,
            "case_ref": str(case.get("_path") or ""),
            "diagnostics": [
                _diagnostic(
                    "missing_source",
                    "声明 unified_dps 但没有计算来源（profile_v2 或 definitions_v2）",
                    severity="warning",
                )
            ],
        }

    # 仅旧 dps_profile，model 为 research/未声明 → 兼容归一为 profile_v2。
    if dp_profile and model in UNIFIED_COMPATIBLE_MODELS:
        resolved, problems = _resolve_profile_source(
            {"path": dp_profile}, root_path, origin="legacy_dps_profile_compat"
        )
        if problems:
            return {
                "schema": SCHEMA,
                "engine": ENGINE_UNSUPPORTED,
                "status": "route_conflict",
                "route_id": None,
                "source": None,
                "case_ref": str(case.get("_path") or ""),
                "diagnostics": problems,
            }
        return {
            "schema": SCHEMA,
            "engine": ENGINE_UNIFIED,
            "status": "ready",
            "route_id": "unified:profile_v2",
            "source": resolved,
            "case_ref": str(case.get("_path") or ""),
            "origin": "legacy_dps_profile_compat",
            "diagnostics": [],
        }

    # 显式旧 model，且没有统一来源 → 隔离 legacy 分支。
    if model in LEGACY_MODELS:
        return {
            "schema": SCHEMA,
            "engine": ENGINE_LEGACY,
            "status": "ready",
            "route_id": f"legacy:{model}",
            "source": {"kind": "legacy_model", "model": model},
            "case_ref": str(case.get("_path") or ""),
            "origin": "explicit_legacy_model",
            "diagnostics": [],
        }

    # research/未声明且无统一来源：发现 v3 候选并做适用性判定（不回落 v2/profile/legacy）。
    if model in ("", "research") and not dp_profile:
        ability = _main_skill(case)
        if not ability:
            return {
                "schema": SCHEMA,
                "engine": ENGINE_NEEDS_INPUT,
                "status": "needs_input",
                "route_id": None,
                "source": None,
                "case_ref": str(case.get("_path") or ""),
                "diagnostics": [
                    _diagnostic("missing_main_skill", "案例未声明主技能，且没有统一计算来源", severity="warning")
                ],
            }
        candidates = discover_definition_candidates(case, root=root_path)
        if candidates["status"] == "applicable_single":
            selected = candidates["selected"]
            return {
                "schema": SCHEMA,
                "engine": ENGINE_UNIFIED,
                "status": "needs_input",
                "route_id": None,
                "source": {
                    "kind": SOURCE_DEFINITIONS_V3,
                    "template": selected["path"],
                    "candidate": selected,
                },
                "case_ref": str(case.get("_path") or ""),
                "origin": "research_definition_candidate",
                "diagnostics": [
                    _diagnostic(
                        "scenario_required",
                        "已发现唯一适用 v3 定义；需按当前输入生成场景后显式绑定，不能沿用历史案例场景",
                        severity="warning",
                    )
                ],
                "candidates": candidates,
            }
        needs_input = candidates["status"] in ("applicable_multiple", "undetermined")
        return {
            "schema": SCHEMA,
            "engine": ENGINE_NEEDS_INPUT if needs_input else ENGINE_NEEDS_RESEARCH,
            "status": "needs_input" if needs_input else "needs_research",
            "route_id": None,
            "source": {"kind": SOURCE_DEFINITIONS_V3, "candidates": candidates},
            "case_ref": str(case.get("_path") or ""),
            "origin": "research_definition_candidates",
            "diagnostics": list(candidates["diagnostics"]),
            "candidates": candidates,
        }

    return {
        "schema": SCHEMA,
        "engine": ENGINE_NEEDS_INPUT,
        "status": "needs_input",
        "route_id": None,
        "source": None,
        "case_ref": str(case.get("_path") or ""),
        "diagnostics": [
            _diagnostic(
                "unresolved_route",
                f"无法从 case 声明确定计算来源（model={model!r}, dps_profile={'有' if dp_profile else '无'}）",
                severity="warning",
            )
        ],
    }


def _load_json_document(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path} 必须是 JSON 对象")
    return document


def _canonical_document(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _merge_mechanism_documents(
    documents: list[tuple[str, dict[str, Any]]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """合并多份机制库：同 ID 同定义合并来源；同 ID 不同定义明确冲突。"""
    merged: dict[str, Any] = {}
    source_files: dict[str, list[str]] = {}
    diagnostics: list[dict[str, Any]] = []
    for path, document in documents:
        mechanisms = document.get("mechanisms")
        if not isinstance(mechanisms, dict):
            diagnostics.append(
                _diagnostic("invalid_mechanism_library", f"{path} 缺少 mechanisms 对象")
            )
            continue
        for mechanism_id, entry in mechanisms.items():
            mechanism_id = str(mechanism_id)
            if mechanism_id not in merged:
                merged[mechanism_id] = entry
                source_files[mechanism_id] = [path]
                continue
            if _canonical_document(merged[mechanism_id]) != _canonical_document(entry):
                diagnostics.append(
                    _diagnostic(
                        "mechanism_definition_conflict",
                        f"机制 {mechanism_id} 在多份机制库中定义不同："
                        f"{', '.join(source_files[mechanism_id])} vs {path}；"
                        "不能最后写入优先，请修正来源",
                    )
                )
                continue
            if path not in source_files[mechanism_id]:
                source_files[mechanism_id].append(path)
    return {"mechanisms": merged, "source_files": source_files}, diagnostics


def _merge_rule_documents(
    documents: list[tuple[str, dict[str, Any]]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """合并 rule 文件：同名键同值合并、异值冲突；不保留“末份覆盖”。"""
    merged: dict[str, Any] = {}
    sources: dict[str, list[str]] = {}
    diagnostics: list[dict[str, Any]] = []
    for path, document in documents:
        for key, value in document.items():
            if key in ("schema", "description", "$schema"):
                continue
            key = str(key)
            if key not in merged:
                merged[key] = value
                sources[key] = [path]
                continue
            if _canonical_document(merged[key]) != _canonical_document(value):
                diagnostics.append(
                    _diagnostic(
                        "rule_definition_conflict",
                        f"rule 键 {key!r} 在多份规则文件中定义不同："
                        f"{', '.join(sources[key])} vs {path}；不能最后写入优先",
                    )
                )
                continue
            if path not in sources[key]:
                sources[key].append(path)
    merged["_source_files"] = sources
    return merged, diagnostics


def load_compilation_inputs(
    route: dict[str, Any],
    *,
    root: str | Path,
    case: dict[str, Any],
    profile: Optional[dict[str, Any]] = None,
    input_overrides: Optional[dict[str, Any]] = None,
):
    """按路由加载输入；返回 ``(context, definition_inputs, diagnostics)``。"""
    root_path = Path(root).resolve()
    if route.get("engine") != ENGINE_UNIFIED or route.get("status") != "ready":
        return None, None, [
            _diagnostic("route_not_ready", f"路由不可编译：engine={route.get('engine')!r}, status={route.get('status')!r}")
        ]
    source = route.get("source") or {}
    diagnostics: list[dict[str, Any]] = []
    kind = source.get("source_kind")
    if kind == SOURCE_PROFILE_V2:
        case_for_context = copy_case_for_synthetic(case)
        if case.get("_path"):
            case_for_context["_path"] = case["_path"]
        source_path = str(source.get("path") or "")
        case_profile = str(case_for_context.get("dps_profile") or "")
        if case_profile and source_path and case_profile != source_path:
            return None, None, [
                _diagnostic(
                    "profile_source_conflict",
                    f"case.dps_profile={case_profile!r} 与 calculation.source.path={source_path!r} 不一致；"
                    "禁止静默选择其中一份",
                )
            ]
        if not case_profile:
            case_for_context["dps_profile"] = source_path
        try:
            context = plan_compiler.prepare_context(
                case_for_context,
                root_path,
                profile=profile,
                calculation_input_override=(input_overrides or {}).get('calculation_input'),
                build_spec_override=(input_overrides or {}).get('build_spec'),
                calculation_input_expected_sha256=(input_overrides or {}).get(
                    'calculation_input_sha256'
                ),
                build_spec_expected_sha256=(input_overrides or {}).get('build_spec_sha256'),
            )
        except (ValueError, OSError, json.JSONDecodeError, KeyError) as exc:
            return None, None, [_diagnostic("input_load_failed", str(exc))]
        return context, None, []
    if kind == SOURCE_DEFINITIONS_V3:
        from . import definitions_v3  # noqa: PLC0415

        template_rel = str(source.get("template") or "")
        template_path = root_path / template_rel
        try:
            template = _load_json_document(template_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return None, None, [_diagnostic("template_load_failed", str(exc))]
        catalog_entries, _catalog_diagnostic = _definition_catalog_entries(root_path)
        catalog_entry = catalog_entries.get(template_rel) if isinstance(template_rel, str) else None
        if catalog_entry:
            template = dict(template)
            if catalog_entry.get("capabilities") is not None:
                template["capabilities"] = catalog_entry["capabilities"]
            if catalog_entry.get("applicability") is not None:
                template["applicability"] = catalog_entry["applicability"]
        if template.get("schema") != definitions_v3.SCHEMA:
            return None, None, [
                _diagnostic("unsupported_template_schema", f"模板 schema 不是 {definitions_v3.SCHEMA}")
            ]
        scenario_rel = str(source.get("scenario") or "")
        scenario_entries = [dict(item) for item in source.get("scenarios") or []]
        scenario_docs: dict[str, dict[str, Any]] = {}
        loaded_scenarios: list[dict[str, Any]] = []
        if scenario_entries:
            for entry in scenario_entries:
                rel = str(entry.get("path") or "")
                try:
                    doc = _load_json_document(root_path / rel)
                except (OSError, json.JSONDecodeError, ValueError) as exc:
                    return None, None, [_diagnostic("scenario_load_failed", f"{rel}: {exc}")]
                scenario_docs[str(entry.get("evaluation_id"))] = doc
                loaded_scenarios.append(
                    {
                        "evaluation_id": str(entry.get("evaluation_id")),
                        "scenario_path": _relative(root_path / rel, root_path),
                        "scenario": doc,
                        "scenario_sha256": _sha256_file(root_path / rel),
                    }
                )
        scenario_doc: Optional[dict[str, Any]] = None
        if not scenario_entries:
            try:
                scenario_doc = _load_json_document(root_path / scenario_rel)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                return None, None, [_diagnostic("scenario_load_failed", str(exc))]
        else:
            declared_evaluations = [
                item
                for item in (template.get("definition") or {}).get("evaluations") or []
                if isinstance(item, dict)
            ]
            primary_id = next(
                (str(item.get("evaluation_id")) for item in declared_evaluations if item.get("primary")),
                None,
            )
            if primary_id is None or primary_id not in scenario_docs:
                return None, None, [
                    _diagnostic(
                        "scenario_evaluation_mismatch",
                        "模板 evaluations（primary）与 case scenarios 不一致",
                    )
                ]
            scenario_doc = scenario_docs[primary_id]
        try:
            case_for_context = copy_case_for_synthetic(case)
            if case.get("_path"):
                case_for_context["_path"] = case["_path"]
            context = plan_compiler.prepare_context(
                case_for_context,
                root_path,
                calculation_input_override=(input_overrides or {}).get("calculation_input"),
                build_spec_override=(input_overrides or {}).get("build_spec"),
                calculation_input_expected_sha256=(input_overrides or {}).get(
                    "calculation_input_sha256"
                ),
                build_spec_expected_sha256=(input_overrides or {}).get("build_spec_sha256"),
            )
            model = definitions_v3.bind_template(
                template,
                context=context,
                scenario=scenario_doc,
                scenarios=loaded_scenarios or None,
            )
        except (ValueError, OSError, json.JSONDecodeError, KeyError) as exc:
            return None, None, [_diagnostic("definitions_v3_binding_failed", str(exc))]
        template_ref = {
            "role": "template",
            "path": _relative(template_path, root_path),
            "sha256": _sha256_file(template_path),
        }
        extra_refs = [template_ref]
        if loaded_scenarios:
            for item in loaded_scenarios:
                extra_refs.append(
                    {
                        "role": "scenario",
                        "path": item["scenario_path"],
                        "sha256": item["scenario_sha256"],
                    }
                )
        else:
            extra_refs.append(
                {
                    "role": "scenario",
                    "path": _relative(root_path / scenario_rel, root_path),
                    "sha256": _sha256_file(root_path / scenario_rel),
                }
            )
        rule_ref = None
        conversion = ((template.get("definition") or {}).get("attribute_conversion") or {})
        rules_rel = str(conversion.get("rules") or "") if isinstance(conversion, dict) else ""
        if rules_rel:
            rule_ref = {
                "role": "attribute_conversion_rules",
                "path": _relative(root_path / rules_rel, root_path),
                "sha256": _sha256_file(root_path / rules_rel),
            }
        if rule_ref:
            extra_refs.append(rule_ref)
        context.input_refs = list(context.input_refs) + extra_refs
        context.input_refs.sort(key=lambda ref: (ref.get("role") or "", ref.get("path") or "", ref.get("sha256") or ""))
        return context, {"kind": "resolved_model_v3", "model": model, "template": template}, []
    if kind == SOURCE_DEFINITIONS_V2:
        template_path = root_path / str(source["template"])
        try:
            template = _load_json_document(template_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return None, None, [_diagnostic("template_load_failed", str(exc))]
        if template.get("schema") != TEMPLATE_SCHEMA:
            return None, None, [
                _diagnostic("unsupported_template_schema", f"模板 schema 不是 {TEMPLATE_SCHEMA}")
            ]
        if str(template.get("ability_id") or "") != _main_skill(case):
            return None, None, [
                _diagnostic("template_ability_mismatch", "模板 ability_id 与 case.main_skill 不一致")
            ]
        mechanisms: Optional[dict[str, Any]] = None
        loaded_mechanisms: list[tuple[str, dict[str, Any]]] = []
        for path in source.get("mechanisms") or []:
            try:
                loaded_mechanisms.append(
                    (str(path), _load_json_document(root_path / path))
                )
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                return None, None, [_diagnostic("mechanism_load_failed", f"{path}: {exc}")]
        if loaded_mechanisms:
            mechanisms, mechanism_diagnostics = _merge_mechanism_documents(loaded_mechanisms)
            diagnostics.extend(mechanism_diagnostics)
            if mechanism_diagnostics:
                return None, mechanisms, diagnostics
        rules: Optional[dict[str, Any]] = None
        loaded_rules: list[tuple[str, dict[str, Any]]] = []
        for path in source.get("rules") or []:
            try:
                loaded_rules.append((str(path), _load_json_document(root_path / path)))
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                return None, None, [_diagnostic("rule_load_failed", f"{path}: {exc}")]
        if loaded_rules:
            rules, rule_diagnostics = _merge_rule_documents(loaded_rules)
            diagnostics.extend(rule_diagnostics)
            if rule_diagnostics:
                return None, rules, diagnostics
        template_relative = _relative(template_path, root_path)
        template_for_compile = copy.deepcopy(template)
        if rules:
            inline_rules = dict(template_for_compile.get("rules") or {})
            for key, value in rules.items():
                if key == "_source_files":
                    continue
                if key in inline_rules and _canonical_document(inline_rules[key]) != _canonical_document(value):
                    diagnostics.append(
                        _diagnostic(
                            "rule_definition_conflict",
                            f"规则键 {key!r} 在模板内联规则与外部规则文件中定义不同；"
                            "禁止静默覆盖",
                        )
                    )
                else:
                    inline_rules[key] = copy.deepcopy(value)
            if diagnostics:
                return None, {"template": template, "mechanisms": mechanisms, "rules": rules}, diagnostics
            template_for_compile["rules"] = inline_rules
        input_refs: list[dict[str, Any]] = [
            {
                "role": "template",
                "path": template_relative,
                "sha256": _sha256_file(template_path),
            }
        ]
        for ref in source.get("input_refs") or []:
            if ref.get("role") == "template":
                continue
            input_refs.append({k: v for k, v in ref.items() if k in ("role", "path", "sha256")})
        compiler_refs = plan_compiler._compiler_source_refs(root_path)
        input_refs.extend(compiler_refs)
        input_refs.sort(key=lambda ref: (ref["role"], ref.get("path") or "", ref["sha256"]))
        context = plan_compiler.FrozenContext(
            root=root_path,
            case=copy_case_for_synthetic(case),
            calc={},
            dataset={},
            profile=None,
            catalog=None,
            input_refs=input_refs,
            synthetic=True,
        )
        return context, {
            "template": template_for_compile,
            "mechanisms": mechanisms,
            "rules": rules,
        }, []
    return None, None, [_diagnostic("unsupported_source_kind", f"未知来源 kind：{kind!r}")]


def copy_case_for_synthetic(case: dict[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in case.items() if not str(key).startswith("_")}
    return payload


def compile_route(
    *,
    root: str | Path,
    case: dict[str, Any],
    profile: Optional[dict[str, Any]] = None,
    route: Optional[dict[str, Any]] = None,
    input_overrides: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, Any], plan_compiler.CompileResult]:
    """共享编译入口：解析路由 → 加载文件闭包 → 编译 → 关闭 catalog。"""
    root_path = Path(root).resolve()
    resolved = route or resolve_calculation_route(case, root=root_path)
    if resolved.get("engine") != ENGINE_UNIFIED or resolved.get("status") != "ready":
        diagnostics = list(resolved.get("diagnostics") or [])
        if not diagnostics:
            diagnostics = [
                _diagnostic(
                    "route_not_compilable",
                    f"统一编译入口不接受路由 engine={resolved.get('engine')!r}, "
                    f"status={resolved.get('status')!r}",
                )
            ]
        return resolved, plan_compiler.CompileResult("rejected", "blocked", None, diagnostics)
    context, definition_inputs, load_diagnostics = load_compilation_inputs(
        resolved,
        root=root_path,
        case=case,
        profile=profile,
        input_overrides=input_overrides,
    )
    if context is None:
        return resolved, plan_compiler.CompileResult(
            "rejected", "blocked", None, load_diagnostics
        )
    try:
        if definition_inputs is not None and definition_inputs.get("kind") == "resolved_model_v3":
            result = plan_compiler.compile_from_resolved_model(
                context, definition_inputs["model"]
            )
        elif definition_inputs is not None:
            result = plan_compiler.compile_from_definitions(
                context,
                definition_inputs["template"],
                mechanisms=definition_inputs.get("mechanisms"),
                rules=definition_inputs.get("rules"),
            )
        else:
            profile_doc = context.profile
            if profile_doc is None:
                return resolved, plan_compiler.CompileResult(
                    "rejected",
                    "blocked",
                    None,
                    [_diagnostic("missing_profile", "profile 来源没有可编译的 profile 内容")],
                )
            result = plan_compiler.compile_from_profile_v2(context, profile_doc)
    except plan_compiler.PlanCompileError as exc:
        result = plan_compiler.CompileResult(
            "rejected",
            "blocked",
            None,
            [
                _diagnostic(
                    "definition_compile_rejected",
                    str(exc),
                )
            ],
        )
    finally:
        context.close()
    return resolved, result


def dependency_fingerprints(
    *,
    root: str | Path,
    route: dict[str, Any],
    plan: Optional[dict[str, Any]] = None,
) -> dict[str, str]:
    """统一路径四键绑定指纹（从计划 input_refs 与实现指纹投影）。"""
    root_path = Path(root).resolve()
    if plan is not None:
        refs = plan.get("input_refs") or []
    else:
        refs = (route.get("source") or {}).get("input_refs") or []
    by_role: dict[str, list[dict[str, Any]]] = {}
    for ref in refs:
        by_role.setdefault(str(ref.get("role")), []).append(ref)

    def first_sha(role: str) -> str:
        values = by_role.get(role) or []
        return str(values[0].get("sha256") or "") if values else ""

    def role_digest(*roles: str) -> str:
        for role in roles:
            value = first_sha(role)
            if value:
                return value
        relevant = [
            [ref.get("role"), ref.get("path"), ref.get("sha256")]
            for ref in refs
            if str(ref.get("role")) in roles
        ]
        return planmod.sha256_text(
            planmod.canonical_json({"roles": list(roles), "refs": relevant, "route": route.get("source")})
        )

    calc_input = role_digest("calculation_input", "profile", "template")
    snapshot = role_digest("build_spec", "template", "mechanism_library")
    dataset = role_digest("dataset_content", "dataset_pointer", "rule_file")
    compiler = plan_handlers.implementation_fingerprint()
    ruleset_payload = {
        "plan_schema": planmod.SCHEMA,
        "plan_sha256": (plan or {}).get("plan_sha256"),
        "sources": [
            [ref.get("role"), ref.get("path"), ref.get("sha256")]
            for ref in sorted(refs, key=lambda item: (str(item.get("role")), str(item.get("path"))))
        ],
        "compiler_fingerprint": compiler["compiler_fingerprint"],
        "executor_fingerprint": compiler["executor_fingerprint"],
    }
    ruleset = planmod.sha256_text(planmod.canonical_json(ruleset_payload))
    return {
        "calc_input": calc_input,
        "ruleset": ruleset,
        "snapshot": snapshot,
        "dataset": dataset,
    }


__all__ = [
    "ENGINE_LEGACY",
    "ENGINE_NEEDS_INPUT",
    "ENGINE_NEEDS_RESEARCH",
    "ENGINE_UNIFIED",
    "ENGINE_UNSUPPORTED",
    "SCHEMA",
    "SOURCE_DEFINITIONS_V2",
    "SOURCE_PROFILE_V2",
    "TEMPLATE_SCHEMA",
    "load_compilation_inputs",
    "resolve_calculation_route",
]
