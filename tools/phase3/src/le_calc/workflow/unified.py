"""统一计算计划的正式阶段实现（第三步共享模块）。

CLI 与 workflow 的 ``compile_plan`` / ``simulate`` / ``validate_result`` /
``render_report`` 阶段统一调用本模块；数值公式仍在 ``plan_executor``，本模块只做
输入交接、产物写入、校验和投影。
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any, Mapping, Optional

from .. import calculation_plan as planmod
from .. import calculation_route
from .. import dps_report
from .. import plan_executor
from .. import plan_handlers
from .. import recalc

SCHEMA_COMPILE_RESULT = "le-compile-result-v1"
SCHEMA_SCENARIO = "le-scenario-v1"
SCHEMA_RESULT = plan_executor.SCHEMA_RESULT
SCHEMA_RESULT_V4 = plan_executor.SCHEMA_RESULT_V4
UI_CONTRACT_VERSION = dps_report.UI_CONTRACT_VERSION
CAPABILITIES_VERSION = "dps-report-v3"
HTML_NAME = "dps-report.html"
REQUIRED_VALIDATION_CHECKS = (
    "result.schema",
    "result.case_id",
    "result.plan_ref",
    "result.scenario_sha256",
    "result.execution_fingerprint",
    "result.single_hit",
    "result.sustained_dps",
    "result.components",
    "result.ailments",
    "result.applications",
    "result.applications_structure",
    "result.application_summary",
    "result.contributions",
    "result.contributions_ids_order",
    "artifact.source-ledger",
    "result.coverage",
    "result.coverage_projection",
    "result.issues_preserved",
    "result.single_hit_consistency",
    "result.cast_tail_consistency",
    "result.stable_dps_consistency",
    "artifact.calculation-plan.json",
    "artifact.plan-diagnostics.json",
    "artifact.compile-result.json",
    "artifact.scenario.json",
    "artifact.result.json",
    "artifact.source-ledger.json",
    "artifact.input-snapshot.json",
)

REQUIRED_VALIDATION_CHECKS_V5 = (
    "result.schema",
    "result.case_id",
    "result.plan_ref",
    "result.evaluations",
    "result.evaluation_policies",
    "result.primary_evaluation",
    "result.primary_projection",
    "result.loops_primary_first",
    "result.scenario_sha256",
    "result.primary_scenario_sha256",
    "result.evaluation_scenarios",
    "result.execution_fingerprint",
    "result.loops",
    "result.loop_totals",
    "result.loops_windows",
    "result.evaluation_summary",
    "result.charge_schedule",
    "result.max_concurrent",
    "result.single_cast",
    "result.single_cast_tail",
    "result.body",
    "result.ignite",
    "result.consistency",
    "result.components",
    "result.ailments",
    "result.applications",
    "result.contributions",
    "result.contributions_ids_order",
    "result.applications_structure",
    "result.application_summary",
    "result.application_component_refs",
    "result.applications_ledger",
    "result.effect_ledger",
    "result.coverage",
    "result.coverage_projection",
    "result.coverage_issue",
    "result.issues_preserved",
    "result.assumptions_preserved",
    "artifact.source-ledger",
    "artifact.calculation-plan.json",
    "artifact.plan-diagnostics.json",
    "artifact.compile-result.json",
    "artifact.scenario.json",
    "artifact.result.json",
    "artifact.source-ledger.json",
    "artifact.input-snapshot.json",
)


REQUIRED_VALIDATION_CHECKS_V6 = (
    "result.schema",
    "result.plan_ref",
    "result.target_side",
    "result.presentation",
    "result.windows",
    "result.totals",
    "result.evaluations",
    "result.applications",
    "result.coverage",
    "result.recompute",
)


def required_validation_checks(plan_schema: Any) -> tuple[str, ...]:
    """按计划 schema 返回必需检查集合（v4/result-v6 独立契约）。"""
    if str(plan_schema) == "le-calculation-plan-v4":
        return REQUIRED_VALIDATION_CHECKS_V6
    if str(plan_schema) == planmod.SCHEMA_V3:
        return REQUIRED_VALIDATION_CHECKS_V5
    return REQUIRED_VALIDATION_CHECKS


def _root(root: str | Path) -> Path:
    return Path(root).resolve()


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _read(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def default_scenario(plan: Mapping[str, Any]) -> dict[str, Any]:
    controls = [
        control for control in plan.get("controls") or [] if isinstance(control, dict)
    ]
    return {
        "states": {
            control["key"]: control.get("default")
            for control in controls
            if control.get("namespace") == "state"
        },
        "target": {
            control["key"]: control.get("default")
            for control in controls
            if control.get("namespace") == "target"
        },
    }


def compile_stage(
    case: Mapping[str, Any],
    *,
    root: str | Path,
    run_dir: Path,
    input_overrides: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """执行 compile_plan：解析路由、加载文件闭包、编译并写入计划产物。"""
    root_path = _root(root)
    route = calculation_route.resolve_calculation_route(dict(case), root=root_path)
    diagnostics: list[dict[str, Any]] = list(route.get("diagnostics") or [])
    outputs: list[Path] = []
    plan = None
    execution = "failed"
    readiness = "blocked"
    if route.get("engine") == calculation_route.ENGINE_UNIFIED and route.get("status") == "ready":
        route, compiled = calculation_route.compile_route(
            root=root_path,
            case=dict(case),
            route=route,
            input_overrides=dict(input_overrides or {}),
        )
        plan = compiled.plan
        execution = (
            "completed"
            if compiled.execution_status == "completed"
            else "failed"
        )
        readiness = compiled.readiness
        diagnostics.extend(compiled.diagnostics)
        if plan is not None:
            _write(run_dir / "calculation-plan.json", plan)
            outputs.append(run_dir / "calculation-plan.json")
        _write(run_dir / "plan-diagnostics.json", diagnostics)
        outputs.append(run_dir / "plan-diagnostics.json")
    compile_result = {
        "schema": SCHEMA_COMPILE_RESULT,
        "case_id": case.get("case_id"),
        "route": route,
        "execution_status": execution,
        "readiness": readiness,
        "plan": planmod.plan_ref(plan) if plan else None,
        "plan_sha256": plan.get("plan_sha256") if plan else None,
        "diagnostics": diagnostics,
        "input_refs": plan.get("input_refs") if plan else route.get("source", {}).get("input_refs") or [],
        "compiler": plan.get("compiler") if plan else None,
        "executor_requirements": plan.get("executor_requirements") if plan else None,
    }
    _write(run_dir / "compile-result.json", compile_result)
    outputs.append(run_dir / "compile-result.json")
    issues = [
        {
            "code": item.get("code") or "compile_diagnostic",
            "message": item.get("message") or str(item),
            "severity": item.get("severity") or "error",
            "related_ids": list(item.get("related_ids") or []),
        }
        for item in diagnostics
        if item.get("severity") == "error"
    ]
    if route.get("engine") in (
        calculation_route.ENGINE_NEEDS_INPUT,
        calculation_route.ENGINE_NEEDS_RESEARCH,
    ):
        execution = "waiting"
    elif route.get("engine") == calculation_route.ENGINE_LEGACY:
        issues = [
            {
                "code": "legacy_route_not_compilable",
                "message": "legacy 计算器不使用统一计划编译阶段",
                "severity": "error",
                "related_ids": [],
            }
        ]
        execution = "failed"
    elif plan is None and execution == "completed":
        execution = "failed"
    return {
        "execution_status": execution,
        "readiness": readiness,
        "outputs": outputs,
        "issues": issues,
        "metrics": {
            "route_id": route.get("route_id"),
            "plan_sha256": plan.get("plan_sha256") if plan else None,
            "diagnostics": len(diagnostics),
        },
    }


def _evaluation_scenario_inputs(
    plan: Mapping[str, Any], *, root: Path
) -> list[dict[str, Any]]:
    """plan-v2/v3：按计划 evaluations 加载独立 scenario 文件并核对文件 sha。"""
    refs_by_path = {
        str(ref.get("path")): str(ref.get("sha256"))
        for ref in plan.get("input_refs") or []
        if isinstance(ref, dict) and ref.get("role") == "scenario"
    }
    inputs: list[dict[str, Any]] = []
    for evaluation in plan.get("evaluations") or []:
        if not isinstance(evaluation, dict):
            raise ValueError("plan evaluations 条目必须是对象")
        path = str(evaluation.get("scenario_path") or "")
        if not path:
            raise ValueError("plan evaluation 缺少 scenario_path")
        absolute = root / path
        if not absolute.is_file():
            raise ValueError(f"评估场景文件不存在：{path}")
        actual_sha = planmod.sha256_file(absolute)
        if refs_by_path.get(path) and refs_by_path[path] != actual_sha:
            raise ValueError(f"评估场景文件内容与计划输入引用不一致：{path}")
        document = _read(absolute)
        states = document.get("controls")
        if not isinstance(states, dict):
            raise ValueError(f"评估场景缺少 controls：{path}")
        target_defaults = {
            str(control.get("key")): control.get("default")
            for control in plan.get("controls") or []
            if isinstance(control, dict) and control.get("namespace") == "target"
        }
        target = dict(target_defaults)
        if document.get("monster_level") is not None:
            target["monster_level"] = document.get("monster_level")
        if document.get("resistance") is not None:
            target["resistance"] = document.get("resistance")
        if not target:
            raise ValueError(f"评估场景缺少 target：{path}")
        digest = planmod.scenario_sha256({"states": states, "target": target})
        if digest != str(evaluation.get("scenario_sha256")):
            raise ValueError(f"评估场景内容与计划声明的场景哈希不一致：{path}")
        inputs.append(
            {
                "evaluation_id": str(evaluation.get("evaluation_id")),
                "scenario": {"states": states, "target": target},
                "scenario_path": path,
                "scenario_sha256": digest,
            }
        )
    if not inputs:
        raise ValueError("plan 没有 evaluations 场景")
    return inputs


def simulate_stage(
    case: Mapping[str, Any],
    *,
    root: str | Path,
    run_dir: Path,
    plan_ref: Optional[Mapping[str, Any]] = None,
    scenario: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """执行 simulate：只读取已编译计划与场景，写出数值产物（不渲染 HTML）。"""
    root_path = _root(root)
    expected_sha = (plan_ref or {}).get("plan_sha256")
    plan = load_plan(run_dir, expected_sha256=expected_sha)
    plan_schema = str(plan.get("schema") or "")
    evaluations: list[dict[str, Any]] = []
    if plan_schema == "le-calculation-plan-v4":
        from .. import plan_v4  # noqa: PLC0415

        result = plan_v4.execute_plan_v4(plan, scenario)
        scenario_doc = {
            "schema": SCHEMA_SCENARIO,
            **copy.deepcopy(result.get("scenario") or {}),
            "primary_evaluation": plan.get("primary_evaluation"),
        }
    elif plan_schema in (planmod.SCHEMA_V2, planmod.SCHEMA_V3) and scenario is None:
        evaluation_inputs = _evaluation_scenario_inputs(plan, root=root_path)
        result = plan_executor.execute_plan(plan, {"evaluations": evaluation_inputs})
        primary_id = result.get("primary_evaluation")
        primary_eval = next(
            item for item in result.get("evaluations") or []
            if str(item.get("evaluation_id")) == str(primary_id)
        )
        plan_paths = {
            str(item.get("evaluation_id")): item.get("scenario_path")
            for item in plan.get("evaluations") or []
        }
        result_scenarios = {
            str(item.get("evaluation_id")): item for item in result.get("evaluations") or []
        }
        evaluation_rows = [
            {
                "evaluation_id": item.get("evaluation_id"),
                "scenario_sha256": item.get("scenario_sha256"),
                "scenario_path": plan_paths.get(str(item.get("evaluation_id"))),
                "states": (result_scenarios.get(str(item.get("evaluation_id"))) or {}).get(
                    "scenario", {}
                ).get("states")
                or (result.get("scenario") or {}).get("states"),
                "target": (result_scenarios.get(str(item.get("evaluation_id"))) or {}).get(
                    "scenario", {}
                ).get("target")
                or (result.get("scenario") or {}).get("target"),
            }
            for item in result.get("evaluations") or []
        ]
        scenario_doc = {
            "schema": SCHEMA_SCENARIO,
            "states": copy.deepcopy(primary_eval.get("scenario", {}).get("states") or {}),
            "target": copy.deepcopy(primary_eval.get("scenario", {}).get("target") or {}),
            "primary_evaluation": primary_id,
            "evaluations": evaluation_rows,
        }
    else:
        actual_scenario = dict(scenario) if scenario is not None else default_scenario(plan)
        result = plan_executor.execute_plan(plan, actual_scenario)
        scenario_doc = {"schema": SCHEMA_SCENARIO, **actual_scenario}
    _write(run_dir / "scenario.json", scenario_doc)
    _write(run_dir / "result.json", result)
    _write(run_dir / "source-ledger.json", result.get("contributions") or [])
    route = calculation_route.resolve_calculation_route(dict(case), root=root_path)
    _write(
        run_dir / "input-snapshot.json",
        {
            "schema": "le-input-snapshot-v2",
            "case_id": case.get("case_id"),
            "route": route,
            "plan": planmod.plan_ref(plan),
            "input_refs": plan.get("input_refs") or route.get("source", {}).get("input_refs") or [],
        },
    )
    coverage = result.get("coverage") or {}
    readiness = "partial" if str(coverage.get("status")) == "partial" else "ready"
    return {
        "execution_status": "completed",
        "readiness": readiness,
        "outputs": [
            run_dir / "scenario.json",
            run_dir / "result.json",
            run_dir / "source-ledger.json",
            run_dir / "input-snapshot.json",
        ],
        "issues": [],
        "metrics": {
            "plan_sha256": plan.get("plan_sha256"),
            "coverage": coverage.get("status"),
        },
    }


def load_plan(run_dir: Path, *, expected_sha256: Optional[str] = None) -> dict[str, Any]:
    plan_path = run_dir / "calculation-plan.json"
    if not plan_path.is_file():
        raise ValueError("缺少 calculation-plan.json（统一路径需要先完成 compile_plan）")
    plan = _read(plan_path)
    planmod.verify_plan_integrity(plan)
    if plan.get("schema") == "le-calculation-plan-v4":
        from .. import plan_v4  # noqa: PLC0415

        plan_v4.validate_plan_v4(plan)
    else:
        issues = planmod.validate_plan_structure(plan)
        if issues:
            codes = ", ".join(str(item.get("code")) for item in issues[:4])
            raise ValueError(f"计划结构校验失败：{codes}")
    if expected_sha256 is not None and str(plan.get("plan_sha256")) != str(expected_sha256):
        raise ValueError("plan_ref/plan_sha256 与当前计算计划不一致")
    return plan


def _summary(result: Mapping[str, Any]) -> dict[str, Any]:
    """验证摘要：只投影结果中真实存在的核心数值，不重算。"""
    payload: dict[str, Any] = {}
    for key in (
        "case_id", "single_hit", "hit_dps", "dot_dps", "sustained_dps",
        "single_cast_total", "horizon_damage", "horizon_dps", "casts", "interval_s",
    ):
        if key in result:
            payload[key] = result[key]
    totals = result.get("totals")
    if isinstance(totals, Mapping):
        payload["totals"] = {key: totals[key] for key in sorted(totals)}
    return payload


def _check(name: str, ok: bool, detail: str, **extra: Any) -> dict[str, Any]:
    payload = {"id": name, "status": "PASS" if ok else "FAIL", "detail": detail}
    payload.update(extra)
    return payload


def validate_stage(
    case: Mapping[str, Any],
    *,
    root: str | Path,
    run_dir: Path,
    write: bool = True,
) -> dict[str, Any]:
    """validate_result：结果/计划/场景/绑定的一致性门禁（不重算数值）。"""
    root_path = _root(root)
    checks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    fatal = False
    try:
        plan = load_plan(run_dir)
        result = _read(run_dir / "result.json")
        scenario_doc = _read(run_dir / "scenario.json")
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        payload = {
            "schema": "le-validation-result-v1",
            "case_id": case.get("case_id"),
            "passed": False,
            "checks": [],
            "errors": [{"id": "artifact_load", "message": str(exc)}],
            "accepted": [],
        }
        if write:
            _write(run_dir / "validation-result.json", payload)
        return {
            "execution_status": "failed",
            "readiness": "blocked",
            "outputs": [run_dir / "validation-result.json"] if write else [],
            "issues": [{"code": "validation_failed", "message": str(exc), "severity": "error", "related_ids": []}],
            "passed": False,
            "payload": payload,
        }

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append(_check(name, ok, detail))
        if not ok:
            errors.append({"id": name, "message": detail})

    if plan.get("schema") == "le-calculation-plan-v4":
        from .. import plan_v4  # noqa: PLC0415

        check_map = plan_v4.stage_validation_checks(plan, result, root=root_path, case=case)
        for check_id in REQUIRED_VALIDATION_CHECKS_V6:
            ok, detail = check_map.get(check_id, (False, "check 未实现"))
            add(check_id, ok, detail)
        passed = all(item["status"] == "PASS" for item in checks)
        result_sha256 = planmod.sha256_file(run_dir / "result.json")
        payload = {
            "schema": "le-validation-result-v1",
            "case_id": case.get("case_id"),
            "passed": passed,
            "checks": checks,
            "errors": errors,
            "accepted": [
                {"kind": "result", "path": _relative(run_dir / "result.json", root_path), "sha256": result_sha256}
            ]
            if passed
            else [],
            "plan_sha256": plan.get("plan_sha256"),
            "result_sha256": result_sha256,
        }
        if write:
            _write(run_dir / "validation-result.json", payload)
        return {
            "execution_status": "completed" if passed else "failed",
            "readiness": "ready" if passed else "blocked",
            "outputs": [run_dir / "validation-result.json"] if write else [],
            "issues": [] if passed else [{"code": "validation_failed", "message": "; ".join(item["message"] for item in errors), "severity": "error", "related_ids": []}],
            "passed": passed,
            "payload": payload,
        }

    is_v3 = plan.get("schema") == planmod.SCHEMA_V3
    is_v2 = plan.get("schema") == planmod.SCHEMA_V2
    expected_result_schema = (
        "le-dps-result-v5"
        if is_v3
        else (SCHEMA_RESULT_V4 if is_v2 else SCHEMA_RESULT)
    )
    add("result.schema", result.get("schema") == expected_result_schema, str(result.get("schema")))
    add("result.case_id", result.get("case_id") == case.get("case_id"), str(result.get("case_id")))
    add(
        "result.plan_ref",
        result.get("plan_sha256") == plan.get("plan_sha256")
        and (result.get("plan_ref") or {}).get("plan_sha256") == plan.get("plan_sha256"),
        "result.plan_ref 必须绑定当前 calculation-plan",
    )
    scenario = {
        "states": scenario_doc.get("states") or {},
        "target": scenario_doc.get("target") or {},
    }
    primary_scenario_sha = planmod.scenario_sha256(scenario)
    aggregate_scenario_sha: Optional[str] = None
    if is_v2 or is_v3:
        plan_evaluations = {
            str(item.get("evaluation_id")): item for item in plan.get("evaluations") or []
        }
        result_evaluations = {
            str(item.get("evaluation_id")): item for item in result.get("evaluations") or []
        }
        scenario_entries = {
            str(item.get("evaluation_id")): item
            for item in scenario_doc.get("evaluations") or []
            if isinstance(item, dict)
        }
        add(
            "result.evaluations",
            bool(plan_evaluations)
            and set(plan_evaluations) == set(result_evaluations) == set(scenario_entries),
            f"plan/result/scenario evaluations={len(plan_evaluations)}/{len(result_evaluations)}/{len(scenario_entries)}",
        )
        scenario_hashes: list[dict[str, str]] = []
        for evaluation_id, plan_evaluation in plan_evaluations.items():
            entry = scenario_entries.get(evaluation_id) or {}
            expected_sha = planmod.scenario_sha256(
                {
                    "states": entry.get("states") or {},
                    "target": entry.get("target") or {},
                }
            )
            result_evaluation = result_evaluations.get(evaluation_id) or {}
            add(
                f"result.evaluation_sha256.{evaluation_id}",
                result_evaluation.get("scenario_sha256") == expected_sha
                == plan_evaluation.get("scenario_sha256"),
                "每个具名评估必须独立绑定场景 states/target 哈希",
            )
            add(
                f"result.evaluation_scenario_path.{evaluation_id}",
                str(entry.get("scenario_path") or "") == str(plan_evaluation.get("scenario_path") or ""),
                "评估场景路径必须与计划一致",
            )
            scenario_hashes.append(
                {"evaluation_id": evaluation_id, "scenario_sha256": expected_sha}
            )
        aggregate_scenario_sha = planmod.aggregate_scenario_sha256(scenario_hashes)
        add(
            "result.scenario_sha256",
            result.get("scenario_sha256") == aggregate_scenario_sha,
            "plan-v2 顶层 scenario_sha256 必须按 evaluation_id 聚合全部评估场景哈希",
        )
        add(
            "result.primary_scenario_sha256",
            result.get("primary_scenario_sha256") == primary_scenario_sha,
            "primary_scenario_sha256 必须等于主评估（scenario.json states/target）哈希",
        )
        add(
            "result.evaluation_scenarios",
            sorted(
                (dict(item) for item in result.get("evaluation_scenarios") or []),
                key=lambda item: str(item.get("evaluation_id")),
            )
            == sorted(scenario_hashes, key=lambda item: item["evaluation_id"]),
            "result.evaluation_scenarios 必须列出按 evaluation_id 排序的全部评估场景哈希",
        )
    else:
        add(
            "result.scenario_sha256",
            result.get("scenario_sha256") == primary_scenario_sha,
            "result.scenario_sha256 必须与 scenario.json 一致",
        )
    execution_scenario_sha = aggregate_scenario_sha if (is_v2 or is_v3) else primary_scenario_sha
    add(
        "result.execution_fingerprint",
        result.get("execution_fingerprint")
        == planmod.execution_fingerprint(
            plan,
            execution_scenario_sha,
            (plan.get("executor_requirements") or {}).get("executor_fingerprint", ""),
        ),
        "execution_fingerprint 必须由计划、场景与执行实现共同决定（v2 使用聚合场景身份）",
    )
    numbers = {}
    number_keys = ("single_hit", "sustained_dps", "hit_dps", "dot_dps", "interval_s")
    if is_v2:
        number_keys = number_keys + ("damage_per_second", "damage_per_cast", "casts_per_second", "hits_per_second")
    if is_v3:
        number_keys = ()
    for key in number_keys:
        value = result.get(key)
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
        add(f"result.{key}", ok, repr(value))
        numbers[key] = value
    components = result.get("components") or []
    ailments = result.get("ailments") or []
    applications = result.get("applications") or []
    contributions = result.get("contributions") or []
    add("result.components", isinstance(components, list), f"count={len(components)}")
    add("result.ailments", isinstance(ailments, list), f"count={len(ailments)}")
    add("result.applications", isinstance(applications, list), f"count={len(applications)}")
    add("result.contributions", isinstance(contributions, list), f"count={len(contributions)}")
    add(
        "result.contributions_ids_order",
        [row.get("contribution_id") for row in contributions]
        == [effect.get("contribution_id") for effect in plan.get("effects") or [] if effect.get("emits_contribution")],
        "贡献账本顺序必须与计划 effects 一致",
    )
    if is_v3:
        from .. import plan_executor_v3  # noqa: PLC0415

        v5_validation = plan_executor_v3.validate_result_v5(dict(plan), result)
        for item in v5_validation["checks"]:
            add(str(item["id"]), item["status"] == "PASS", str(item.get("detail") or ""))
    # 结果内部一致性：单次/窗口/稳定态必须能由组件与账本复算（不重跑公式）
    try:
        component_total = sum(float(item.get("damage") or 0.0) for item in components)
        if is_v2:
            first_hit_total = sum(
                float(item.get("damage") or 0.0) * (float(item.get("damage_weights")[0]))
                for item in components
            )
            per_cast_hit = sum(
                float(item.get("damage") or 0.0)
                * sum(float(value) for value in item.get("damage_weights") or [1.0])
                for item in components
            )
            add(
                "result.single_hit_consistency",
                abs(float(result.get("single_hit") or 0.0) - first_hit_total) <= 1e-6,
                f"single_hit={result.get('single_hit')!r}, first_hit_sum={first_hit_total!r}",
            )
            hits_block = result.get("hits_per_cast") or {}
            add(
                "result.hits_per_cast",
                int(hits_block.get("per_cast") or 0) == sum(int(item.get("hits_per_cast") or 1) for item in components)
                and abs(
                    float(hits_block.get("effective_hits_per_cast") or 0.0)
                    - sum(
                        sum(float(value) for value in item.get("damage_weights") or [])
                        for item in components
                    )
                )
                <= 1e-9,
                f"hits_per_cast={hits_block!r}",
            )
            interval = float(result.get("interval_s") or 0.0)
            rotation = float(result.get("rotation_more_factor") or 1.0)
            expected_per_cast = per_cast_hit * rotation
            expected_dps = expected_per_cast / interval if interval > 0 else 0.0
            expected_hits_per_second = (
                float(hits_block.get("per_cast") or 0.0) / interval
            )
            add(
                "result.hit_count_separation",
                abs(float(result.get("hits_per_second") or 0.0) - expected_hits_per_second) <= 1e-6
                and abs(
                    float(result.get("casts_per_second") or 0.0) - (1.0 / interval if interval > 0 else 0.0)
                )
                <= 1e-9,
                "每秒命中数与每秒施放数必须分开且可复算",
            )
            add(
                "result.stable_dps_consistency",
                abs(float(result.get("damage_per_cast") or 0.0) - expected_per_cast) <= 1e-6
                and abs(float(result.get("damage_per_second") or 0.0) - expected_dps) <= 1e-6
                and abs(float(result.get("hit_dps") or 0.0) - expected_dps) <= 1e-6
                and abs(float(result.get("sustained_dps") or 0.0) - expected_dps) <= 1e-6
                and abs(float(result.get("dot_dps") or 0.0)) <= 1e-9,
                "plan-v2 damage_per_second 必须由逐击权重、周期因子与间隔复算一致",
            )
            for window in result.get("windows") or []:
                seconds = float(window.get("seconds") or 0.0)
                casts = float(window.get("casts") or 0.0)
                total = float(window.get("hit_damage") or 0.0)
                if seconds <= 0:
                    raise ValueError("窗口秒数无效")
                if (
                    abs(casts - seconds / interval) > 1e-6
                    or abs(total - casts * expected_per_cast) > 1e-4
                    or abs(float(window.get("dps") or 0.0) - expected_dps) > 1e-4
                ):
                    raise ValueError(f"窗口 {window.get('window_id')!r} 与稳定态不一致")
            add("result.window_consistency", True, "窗口施放数/总伤害/DPS 由期望施放数复算一致")
            ailment_tail = sum(float(item.get("damage_per_cast") or 0.0) for item in ailments)
            add(
                "result.cast_tail_consistency",
                abs(
                    float(result.get("single_cast_with_tail") or 0.0)
                    - (float(result.get("single_hit") or 0.0) + ailment_tail)
                )
                <= 1e-6,
                "single_cast_with_tail 必须等于首击 + 异常每次施放伤害",
            )
        else:
            add(
                "result.single_hit_consistency",
                abs(float(result.get("single_hit") or 0.0) - component_total) <= 1e-6,
                f"single_hit={result.get('single_hit')!r}, components_sum={component_total!r}",
            )
            ailment_tail = sum(float(item.get("damage_per_cast") or 0.0) for item in ailments)
            add(
                "result.cast_tail_consistency",
                abs(
                    float(result.get("single_cast_with_tail") or 0.0)
                    - (float(result.get("single_hit") or 0.0) + ailment_tail)
                )
                <= 1e-6,
                "single_cast_with_tail 必须等于单次命中 + 异常每次施放伤害",
            )
            every_nth = (plan.get("schedule") or {}).get("every_nth_hit")
            interval = float(result.get("interval_s") or 0.0)
            expected_hit = 0.0
            if interval > 0:
                extra = 0.0
                if every_nth:
                    extra = float(every_nth.get("extra_more") or 0.0) / int(every_nth.get("n") or 1)
                expected_hit = float(result.get("single_hit") or 0.0) / interval * (1.0 + extra)
            expected_dot = ailment_tail / interval if interval > 0 else 0.0
            add(
                "result.stable_dps_consistency",
                abs(float(result.get("hit_dps") or 0.0) - expected_hit) <= 1e-6
                and abs(float(result.get("dot_dps") or 0.0) - expected_dot) <= 1e-6
                and abs(
                    float(result.get("sustained_dps") or 0.0)
                    - (float(result.get("hit_dps") or 0.0) + float(result.get("dot_dps") or 0.0))
                )
                <= 1e-6,
                "hit_dps/dot_dps/sustained_dps 必须由单次伤害、异常与间隔复算一致",
            )
    except (TypeError, ValueError, ZeroDivisionError, IndexError) as exc:
        add("result.consistency", False, f"结果一致性复算失败：{exc}")
    # 来源/修订/场景计划绑定：计划 subject 必须与当前 case 一致
    subject = plan.get("subject") or {}
    case_build = case.get("build") or {}
    if isinstance(case_build, dict) and case_build.get("build_id"):
        add(
            "plan.subject.build_id",
            subject.get("build_id") == case_build.get("build_id"),
            f"plan={subject.get('build_id')!r}, case={case_build.get('build_id')!r}",
        )
        add(
            "plan.subject.revision_id",
            subject.get("revision_id") == case_build.get("revision_id"),
            f"plan={subject.get('revision_id')!r}, case={case_build.get('revision_id')!r}",
        )
    # expected 缺省=未配置；显式声明但文件缺失必须失败，显式存在则按共享 expected 口径核对
    expected_value = case.get("expected")
    if expected_value:
        from . import expected_check  # noqa: PLC0415

        try:
            expected_checks, expected_ok = expected_check.compare_expected(
                case, run_dir, root=root_path
            )
        except Exception as exc:  # noqa: BLE001 - 显式 expected 失败必须明确
            add("expected.declared", False, f"声明 expected 但无法核对：{exc}")
        else:
            for item in expected_checks:
                add(
                    f"expected.{item.get('name')}",
                    item.get("status") == "PASS",
                    str(item.get("detail") or ""),
                )
            add("expected.config", True, "expected 已声明并完成核对")
    # 应用记录：结构与摘要必须可由 applications 复算
    from .. import dps_result  # noqa: PLC0415

    application_rows_ok = all(
        isinstance(item, dict)
        and item.get("contribution_id")
        and item.get("component_id")
        and item.get("status")
        for item in applications
    )
    add("result.applications_structure", application_rows_ok, f"count={len(applications)}")
    expected_application_summary = dps_result.application_summary(applications)
    add(
        "result.application_summary",
        result.get("contribution_application_summary") == expected_application_summary,
        "contribution_application_summary 必须由 applications 复算一致",
    )
    coverage = result.get("coverage") or {}
    from .. import plan_executor  # noqa: PLC0415

    if is_v3:
        from .. import plan_executor_v3  # noqa: PLC0415

        coverage_records = plan_executor_v3.coverage_applications(applications)
    else:
        coverage_records = applications
    projected_coverage = plan_executor.project_coverage(
        plan=plan, dot_applications=coverage_records
    )
    add(
        "result.coverage_projection",
        coverage.get("status") == projected_coverage.get("status")
        and int(coverage.get("gap_count") or 0) == projected_coverage.get("gap_count")
        and list(coverage.get("gaps") or []) == list(projected_coverage.get("gaps") or []),
        "coverage 必须等于计划基线缺口 + 实际应用记录运行时缺口",
    )
    # issues/scope：计划声明和覆盖缺口不得在投影中丢失
    facts = plan.get("facts") or {}
    limitations = list((plan.get("scope") or {}).get("limitations") or [])
    required_issue_fragments = (
        [str(item) for item in limitations]
        + [str(item) for item in facts.get("early_issues") or []]
        + [str(item) for item in facts.get("post_assembly_issues") or []]
        + [str(item) for item in facts.get("duplicate_issues") or []]
    )
    issues = result.get("issues") or []
    add(
        "result.issues_preserved",
        isinstance(issues, list)
        and all(fragment in issues for fragment in required_issue_fragments)
        and isinstance(result.get("scope"), str)
        and bool(result.get("scope")),
        "计划 limitations/静态 issues 与 scope 必须保留在结果中",
    )
    if coverage.get("status") == "partial":
        add(
            "result.coverage_issue",
            any(str(item).startswith("覆盖状态 partial") for item in issues),
            "partial 结果必须保留覆盖缺口摘要",
        )
    add(
        "result.coverage",
        coverage.get("status") in ("complete", "partial")
        and int(coverage.get("gap_count") or 0) == len(coverage.get("gaps") or []),
        f"status={coverage.get('status')!r}, gaps={coverage.get('gap_count')!r}",
    )
    add(
        "result.assumptions_preserved",
        isinstance(result.get("issues"), list) and isinstance(result.get("scope"), str),
        "issues/scope 必须保留",
    )
    component_refs = {
        str(item.get("component_id"))
        for item in components
        if isinstance(item, dict)
    } | {
        str(item.get("component_id"))
        for item in ailments
        if isinstance(item, dict)
    }
    add(
        "result.application_component_refs",
        bool(component_refs)
        and all(
            isinstance(item, dict) and str(item.get("component_id")) in component_refs
            for item in applications
        ),
        "applications[].component_id 必须引用结果中实际存在的命中/持续组件",
    )
    # report-binding 是 render_report 之后的产物：validate_result 阶段（写模式）不检查；
    # 只读复验/commit 门禁（write=False）必须核对报告绑定与 result 的场景执行身份。
    # v2 报告必须携带 scenario_binding；v1 历史签收 run（如 VC dps-step4-vc-8）没有该字段时
    # 保持兼容不判失败，但新生成 v1 报告一旦携带就必须一致。
    binding_path = run_dir / "report-binding.json"
    if binding_path.is_file() and not write:
        try:
            report_binding = _read(binding_path)
        except (OSError, json.JSONDecodeError):
            report_binding = {}
        expected_scenario_binding = _scenario_binding(plan, result)
        if is_v2 or is_v3 or "scenario_binding" in report_binding:
            add(
                "result.report_scenario_binding",
                report_binding.get("scenario_binding") == expected_scenario_binding,
                "report-binding 的场景执行身份必须与 result 一致（v2 为全部评估聚合哈希）",
            )
    ledger_ok = False
    try:
        ledger = _read(run_dir / 'source-ledger.json')
        ledger_ok = ledger == contributions
    except (OSError, json.JSONDecodeError):
        ledger_ok = False
    add(
        'artifact.source-ledger',
        ledger_ok,
        'source-ledger.json 必须与 result.contributions 逐项一致',
    )
    artifacts = {
        name: (run_dir / name).is_file()
        for name in (
            "calculation-plan.json",
            "plan-diagnostics.json",
            "compile-result.json",
            "scenario.json",
            "result.json",
            "source-ledger.json",
            "input-snapshot.json",
        )
    }
    for name, present in artifacts.items():
        add(f"artifact.{name}", present, "存在" if present else "缺失")
    if is_v3:
        # v1/v2 的命中/稳定态一致性检查对纯 DoT v5 结果只会 0==0 空真，不纳入 v3 检查集；
        # v3 的真实一致性由 plan_executor_v3.validate_result_v5 提供。
        vacuous = {
            "result.single_hit_consistency",
            "result.cast_tail_consistency",
            "result.stable_dps_consistency",
            "result.window_consistency",
        }
        checks = [item for item in checks if item["id"] not in vacuous]
    passed = not errors
    payload = {
        "schema": "le-validation-result-v1",
        "case_id": case.get("case_id"),
        "passed": passed,
        "checks": checks,
        "errors": errors,
        "accepted": [item["id"] for item in checks if item["status"] == "PASS"],
        "summary": _summary(result),
        "coverage": coverage.get("status"),
        "plan_sha256": plan.get("plan_sha256"),
        "scenario_sha256": execution_scenario_sha,
        "primary_scenario_sha256": primary_scenario_sha if (is_v2 or is_v3) else execution_scenario_sha,
        "execution_fingerprint": result.get("execution_fingerprint"),
        "result_sha256": planmod.sha256_file(run_dir / "result.json"),
    }
    payload["required_checks"] = list(required_validation_checks(plan.get("schema")))
    if write:
        if write:
            _write(run_dir / "validation-result.json", payload)
    issues = [
        {
            "code": "validation_failed",
            "message": f"{item['id']}: {item['message']}",
            "severity": "error",
            "related_ids": [item["id"]],
        }
        for item in errors
    ]
    return {
        "execution_status": "completed" if passed else "failed",
        "readiness": "ready" if passed else "partial",
        "outputs": [run_dir / "validation-result.json"],
        "issues": issues,
        "passed": passed,
        "payload": payload,
    }


def _first_text(value: Any, keys: tuple[str, ...] = ("readable_text", "readable", "display_name", "label", "name")) -> str:
    if isinstance(value, dict):
        for key in keys:
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip()
        for child in value.values():
            text = _first_text(child, keys)
            if text:
                return text
    elif isinstance(value, list):
        for child in value:
            text = _first_text(child, keys)
            if text:
                return text
    return ""


def _affix_rows(instance: Mapping[str, Any]) -> list[dict[str, Any]]:
    affix_root = instance.get("affixes") or {}
    iterable: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(affix_root, dict):
        for channel, items in affix_root.items():
            if isinstance(items, list):
                iterable.extend((channel, item) for item in items if isinstance(item, dict))
            elif isinstance(items, dict):
                iterable.append((channel, items))
    elif isinstance(affix_root, list):
        iterable.extend(("affix", item) for item in affix_root if isinstance(item, dict))
    rows: list[dict[str, Any]] = []
    for channel, item in iterable:
        affix = item.get("affix") or {}
        ref = affix.get("ref") or {}
        value_texts: list[str] = []
        for value in (item.get("value") or {}).get("values") or []:
            if isinstance(value, dict):
                display = value.get("display_value")
                stat = value.get("stat_name")
                if display and stat:
                    value_texts.append(f"{display} {stat}")
                elif stat:
                    value_texts.append(str(stat))
        text = _first_text(item)
        name = str(ref.get("name") or affix.get("name") or text or "")
        tier = item.get("source_tier") or item.get("tier") or affix.get("tier")
        readable = " / ".join(value_texts) if value_texts else (text or name)
        if tier is not None:
            readable = f"{readable} · T{tier}"
        roll = item.get("roll") or {}
        rows.append({
            "channel": str(channel),
            "name": name,
            "tier": tier,
            "raw": roll.get("raw") if isinstance(roll, dict) else None,
            "readable_text": readable,
            "source_path": item.get("source_path"),
        })
    return rows


def _project_item(instance: Mapping[str, Any], *, slot: str, role: str) -> dict[str, Any]:
    definition = instance.get("definition") or {}
    base = instance.get("base") or {}
    return {
        "slot": slot,
        "role": role,
        "instance_id": instance.get("instance_id"),
        "name": str(definition.get("name") or instance.get("display_name") or base.get("name") or ""),
        "base": str(base.get("name") or ""),
        "rarity": str(instance.get("rarity") or ""),
        "affixes": _affix_rows(instance),
        "source_path": instance.get("source_path"),
    }


def _project_equipment(
    case: Mapping[str, Any], root: Path, plan: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """以计划绑定的完整 calculation-input 为装备清单；BuildSpec 仅兼容补充。"""
    refs = [ref for ref in (plan or {}).get("input_refs") or []
            if ref.get("role") == "calculation_input"]
    if len(refs) > 1:
        raise ValueError("装备投影存在多个 calculation_input 权威来源")
    ref = refs[0] if refs else None
    path_text = ref.get("path") if ref else (case.get("inputs") or {}).get("calculation_input")
    if path_text:
        path = root / str(path_text)
        expected_sha = ref.get("sha256") if ref else (case.get("input_sha256") or {}).get("calculation_input")
        if not path.is_file():
            raise ValueError(f"装备明细输入不存在：{path_text}")
        if expected_sha and planmod.sha256_file(path) != expected_sha:
            raise ValueError(f"装备明细输入与绑定 SHA 不一致：{path_text}")
        calc = _read(path)
        items = calc.get("items")
        if not isinstance(items, list):
            raise ValueError("装备明细 calculation-input.items 必须是数组")
        rows = []
        seen = set()
        for item in items:
            iid = str(item.get("instance_id") or "")
            if not iid or iid in seen:
                raise ValueError(f"装备明细实例身份缺失或重复：{iid!r}")
            seen.add(iid)
            row = copy.deepcopy(item)
            if not isinstance(row.get("mods"), list):
                raise ValueError(f"装备明细 {iid} 缺少完整 mods 清单")
            mod_ids = [mod.get("mod_id") for mod in row["mods"]]
            if any(not key for key in mod_ids) or len(mod_ids) != len(set(mod_ids)):
                raise ValueError(f"装备明细 {iid} 词条身份缺失或重复")
            row["mods_complete"] = True
            rows.append(row)
        return rows
    # 旧输入未提供完整解码清单时，仅保留兼容摘要，不称为完整词条。
    return _project_equipment_summary(case, root)


def _project_equipment_summary(case: Mapping[str, Any], root: Path) -> list[dict[str, Any]]:
    """Legacy BuildSpec 元数据/affix 摘要；不能覆盖已有完整 mods。"""
    build_spec = case.get("build", {}).get("build_spec") if isinstance(case.get("build"), dict) else None
    if not build_spec:
        return []
    path = root / str(build_spec)
    if not path.is_file():
        return []
    spec = _read(path)
    rows: list[dict[str, Any]] = []
    for entry in (spec.get("equipment") or {}).get("slots") or []:
        if not isinstance(entry, dict):
            continue
        instance = entry.get("instance") or {}
        if instance:
            rows.append(_project_item(instance, slot=str(entry.get("slot") or ""), role="equipment"))
    for entry in (spec.get("idols") or {}).get("entries") or []:
        instance = (entry or {}).get("instance") or {}
        if instance:
            rows.append(_project_item(instance, slot="idol", role="idol"))
    for entry in (spec.get("blessings") or {}).get("entries") or []:
        instance = (entry or {}).get("instance") or {}
        if instance:
            rows.append(_project_item(instance, slot="blessing", role="blessing"))
    return rows


def _target_controls(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    generic = (plan.get("execution") or {}).get("execution_v1") or {}
    controls: list[dict[str, Any]] = []
    for control in (generic.get("target") or {}).get("controls") or []:
        if not isinstance(control, dict):
            continue
        namespace = str(control.get("namespace") or ("target" if str(control.get("key") or "").startswith("target.") else "state"))
        if namespace != "target":
            continue
        controls.append({
            "key": control.get("key"),
            "label": control.get("label") or control.get("key"),
            "type": control.get("type") or "boolean",
            "default": control.get("default"),
            "source": "目标条件",
            "readonly": bool(control.get("readonly")),
            "min": control.get("min"),
            "max": control.get("max"),
            "effect": control.get("effect"),
        })
    return controls


def _state_controls(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把 plan 声明的 state 控件投影为报告可编辑控件（场景假设/Buff），target 控件另走 _target_controls。"""
    controls: list[dict[str, Any]] = []
    for control in (plan.get("execution") or {}).get("controls") or []:
        if not isinstance(control, dict):
            continue
        key = str(control.get("key") or "")
        namespace = str(
            control.get("namespace") or ("target" if key.startswith("target.") else "state")
        )
        if namespace != "state":
            continue
        controls.append(
            {
                "key": key,
                "label": control.get("label") or key,
                "source": control.get("source"),
                "type": control.get("type") or "boolean",
                "default": control.get("default"),
                "min": control.get("min"),
                "max": control.get("max"),
                "readonly": bool(control.get("readonly")),
                "effect": control.get("effect"),
                "single_cast_only": bool(control.get("single_cast_only")),
                "requires_target": control.get("requires_target"),
            }
        )
    return controls


def _report_details(plan: Mapping[str, Any], result: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, Any]:
    """Return disk result.report_details verbatim; build a fallback only when absent."""
    existing = result.get("report_details")
    if isinstance(existing, dict) and existing:
        details = copy.deepcopy(existing)
        current = {str(item.get("instance_id")): item for item in details.get("equipment") or []}
        for item in binding.get("equipment") or []:
            iid = str(item.get("instance_id"))
            # 已绑定完整清单优先；兼容摘要不能覆盖原来已有的 mods。
            if item.get("mods_complete") or iid not in current:
                current[iid] = copy.deepcopy(item)
        details["equipment"] = list(current.values())
        return details
    generic = (plan.get("execution") or {}).get("execution_v1") or {}
    return {
        "components": list(generic.get("components") or []),
        "loops": result.get("policy_results") or result.get("evaluations") or [],
        "single_cast": result.get("single_cast") or {},
        "ignite": {},
        "cast": {},
        "cooldown": {},
        "equipment": list(binding.get("equipment") or []),
        "ailments": [],
        "attributes": {},
        "controls": [],
    }


def _scenario_binding(plan: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """场景执行身份（v1 单场景 / v2/v3 按 evaluation_id / v4 单场景+primary）。"""
    if plan.get("schema") == "le-calculation-plan-v4":
        return {
            "schema": "le-scenario-binding-v4",
            "primary_evaluation": plan.get("primary_evaluation"),
            "scenario": copy.deepcopy(plan.get("scenario") or {}),
        }
    if plan.get("schema") in (planmod.SCHEMA_V2, planmod.SCHEMA_V3):
        return {
            "schema": "le-scenario-binding-v2",
            "aggregation": "by_evaluation_id",
            "scenario_sha256": result.get("scenario_sha256"),
            "primary_scenario_sha256": result.get("primary_scenario_sha256"),
            "evaluations": copy.deepcopy(result.get("evaluation_scenarios") or []),
        }
    return {
        "schema": "le-scenario-binding-v1",
        "scenario_sha256": result.get("scenario_sha256"),
    }


def render_stage(
    case: Mapping[str, Any],
    *,
    root: str | Path,
    run_dir: Path,
    endpoint: str,
    display_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """render_report：由 result + plan 生成 HTML/binding/capabilities。"""
    root_path = _root(root)
    plan = load_plan(run_dir)
    result = _read(run_dir / "result.json")
    target_dir = display_dir or run_dir
    try:
        run_display = _relative(target_dir, root_path)
    except ValueError:
        run_display = str(target_dir)
    fingerprints = calculation_route.dependency_fingerprints(
        root=root_path,
        route=calculation_route.resolve_calculation_route(dict(case), root=root_path),
        plan=None,
    )
    renderer = dps_report.renderer_fingerprint()
    report_ref = recalc.build_report_ref(
        case_id=str(case.get("case_id")),
        model_id="dps",
        run_id=run_dir.name,
        run_dir=run_display,
        report_path=f"{run_display}/{HTML_NAME}",
        input_fingerprints=fingerprints,
        recompute_endpoint=endpoint,
        ui_contract_version=UI_CONTRACT_VERSION,
        renderer_fingerprint=renderer,
    )
    binding = recalc.build_recalc_bindings(
        case_id=str(case.get("case_id")),
        model_id="dps",
        capabilities_version=CAPABILITIES_VERSION,
        ui_contract_version=UI_CONTRACT_VERSION,
        renderer_fingerprint=renderer,
        report_ref=report_ref,
        input_fingerprints=fingerprints,
        recompute_endpoint=endpoint,
    )
    scenario_binding = _scenario_binding(plan, result)
    binding["scenario_binding"] = scenario_binding
    equipment_rows = _project_equipment(case, root_path, plan)
    binding["equipment"] = equipment_rows
    # 明确装备投影来源/完整度：完整解码清单一律带 mods_complete；旧输入只能回退摘要，
    # 用 mode 字段如实标注，避免摘要被误认为完整词条（报告契约 §6.9 / UR-15）。
    complete = bool(equipment_rows) and all(bool(item.get("mods_complete")) for item in equipment_rows)
    role_counts: dict[str, int] = {}
    for item in equipment_rows:
        role = str(item.get("role") or "equipment")
        role_counts[role] = role_counts.get(role, 0) + 1
    binding["equipment_projection"] = {
        "mode": "complete" if complete else ("summary" if equipment_rows else "empty"),
        "item_count": len(equipment_rows),
        "mod_count": sum(
            len(item.get("mods") or item.get("affixes") or []) for item in equipment_rows
        ),
        "roles": role_counts,
    }
    display_result = dict(result)
    display_result["report_details"] = _report_details(plan, result, binding)
    report_details = display_result["report_details"]
    if not report_details.get("controls"):
        projected_controls = _state_controls(plan)
        if projected_controls:
            report_details["controls"] = projected_controls
    binding["target_controls"] = _target_controls(plan)
    html = dps_report.render(display_result, binding)
    (run_dir / HTML_NAME).write_text(html, encoding="utf-8")
    _write(run_dir / "report-binding.json", binding)
    details = display_result.get("report_details") or {}
    controls = [
        {
            "key": control.get("key"),
            "label": control.get("label"),
            "source": control.get("source"),
            "type": control.get("type"),
            "default": control.get("default"),
            "min": control.get("min"),
            "max": control.get("max"),
            "readonly": control.get("readonly"),
            "effect": control.get("effect"),
            "single_cast_only": control.get("single_cast_only"),
            "requires_target": control.get("requires_target"),
        }
        for control in details.get("controls") or []
        if isinstance(control, dict)
    ]
    target_controls = [dict(item) for item in binding.get("target_controls") or [] if isinstance(item, dict)]
    _write(
        run_dir / "report-capabilities.json",
        {
            "schema": "report-capabilities-v1",
            "model": "dps",
            "pages": ["overview", "damage", "windows", "sources", "equipment", "buffs"],
            "target_controls": target_controls,
            "recalculation": {
                "backend": "python",
                "same_calculate_entry_as_cli": True,
                "fixed_conditions": True,
                "save_load_config": True,
                "session_revision_guard": True,
            },
            "controls": controls,
            "scope": result.get("scope"),
            "coverage": (result.get("coverage") or {}).get("status"),
            "calculation_plan": planmod.plan_ref(plan),
            "scenario_binding": scenario_binding,
            "equipment_projection": binding.get("equipment_projection"),
        },
    )
    outputs = [
        run_dir / HTML_NAME,
        run_dir / "report-binding.json",
        run_dir / "report-capabilities.json",
    ]
    return {
        "execution_status": "completed",
        "readiness": "partial"
        if ((result.get("coverage") or {}).get("status") == "partial")
        else "ready",
        "outputs": outputs,
        "issues": [],
        "metrics": {
            "endpoint": endpoint,
            "renderer_fingerprint": renderer,
            "report_key": report_ref.get("report_key"),
        },
        "binding": binding,
    }


def write_manifest(
    *,
    case: Mapping[str, Any],
    root: str | Path,
    run_dir: Path,
    result: Optional[Mapping[str, Any]] = None,
    display_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """写 run-manifest（文件清单与哈希；不自哈希）。"""
    root_path = _root(root)
    if result is None and (run_dir / "result.json").is_file():
        result = _read(run_dir / "result.json")
    plan = _read(run_dir / "calculation-plan.json") if (run_dir / "calculation-plan.json").is_file() else {}
    files: dict[str, str] = {}
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        name = path.relative_to(run_dir).as_posix()
        if name in ("run-manifest.json", "pipeline-state.json"):
            continue
        files[name] = planmod.sha256_file(path)
    target = display_dir or run_dir
    run_display = _relative(target, root_path)
    manifest = {
        "schema": "le-run-manifest-v1",
        "case_id": case.get("case_id"),
        "run_id": run_dir.name,
        "run_dir": run_display,
        "status": result.get("status") if result else None,
        "coverage_status": ((result or {}).get("coverage") or {}).get("status"),
        "calculation_plan": planmod.plan_ref(plan) if plan else None,
        "plan_sha256": plan.get("plan_sha256") if plan else None,
        "scenario_sha256": (result or {}).get("scenario_sha256"),
        "primary_scenario_sha256": (result or {}).get("primary_scenario_sha256"),
        "evaluation_scenarios": copy.deepcopy((result or {}).get("evaluation_scenarios") or []),
        "execution_fingerprint": (result or {}).get("execution_fingerprint"),
        "executor_fingerprint": (plan.get("executor_requirements") or {}).get("executor_fingerprint") if plan else None,
        "result_sha256": files.get("result.json"),
        "html_sha256": files.get(HTML_NAME),
        "files": files,
    }
    _write(run_dir / "run-manifest.json", manifest)
    return manifest


__all__ = [
    "CAPABILITIES_VERSION",
    "HTML_NAME",
    "SCHEMA_COMPILE_RESULT",
    "SCHEMA_SCENARIO",
    "compile_stage",
    "default_scenario",
    "load_plan",
    "render_stage",
    "simulate_stage",
    "validate_stage",
    "write_manifest",
]
