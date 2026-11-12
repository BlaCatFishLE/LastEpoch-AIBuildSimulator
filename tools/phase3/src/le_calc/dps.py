"""统一 DPS 计算入口（第二步：统一计算计划契约）。

对外入口保持兼容：``calculate`` / ``run`` 的调用方不需要知道计划层。内部只有一条
数值执行链：

    当前输入 + profile-v2（或原生技能模板）
        → prepare_context（加载/校验/冻结）
        → compile_from_profile_v2 / compile_from_definitions（生成 calculation-plan）
        → execute_plan(plan, scenario, runtime)（唯一执行器）

``run`` 会把真正参与计算的 ``calculation-plan.json`` 与编译诊断写入运行目录，
result/manifest 记录计划指纹。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Optional

from . import calculation_plan as planmod
from . import plan_compiler
from . import plan_executor
from . import plan_handlers
from . import dps_components

DAMAGE_TYPES = dps_components.DAMAGE_TYPES
SCHEMA_RESULT = plan_executor.SCHEMA_RESULT
TARGET_KEYS = ("monster_level", "resistance", "high_health", "boss", "blinded", "slowed")
DEFAULT_TARGET = {
    "monster_level": 100,
    "resistance": 0.0,
    "high_health": False,
    "boss": True,
    "blinded": False,
    "slowed": False,
}


def read(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path: Path, payload: Any) -> None:
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _ownership_key(row: Any) -> Optional[tuple[Any, ...]]:
    """第一步兼容：同源同输出的去重键（供既有测试/外部调用）。"""
    raw = row.raw or {}
    tags = tuple(sorted(row.damage_tags or []))
    if raw.get("mod_id"):
        return ("mod", str(raw.get("mod_id")), row.stat_key, row.operation, tags)
    if raw.get("tree") is not None and raw.get("node") is not None:
        return (
            "node",
            f"{raw.get('tree')}:{raw.get('node')}:{raw.get('stat_index')}",
            row.stat_key,
            row.operation,
            tags,
        )
    return None


def _append_owned(rows: list[Any], owned: dict[Any, str], row: Any) -> Optional[str]:
    """第一步兼容：重复贡献返回已有 contribution_id，新增返回 None。"""
    key = _ownership_key(row)
    if key is not None and key in owned:
        return owned[key]
    if key is not None:
        owned[key] = row.contribution_id
    rows.append(row)
    return None


def build_coverage(
    *,
    contributions: list[Any],
    claimed_mod_ids: set[str],
    effect_results: list[Any],
    unmodeled_chance_records: list[dict[str, Any]],
    declared_inactive_checks: list[dict[str, Any]],
    skill_constants: dict[str, Any],
    per_attribute_problems: list[str],
    extra_gaps: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """第一步兼容入口：复用统一覆盖口径。"""
    from . import dps_result

    statuses = [
        {
            "effect_id": getattr(effect, "effect_id", ""),
            "label": getattr(effect, "label", ""),
            "status": getattr(effect, "status", ""),
            "reason": getattr(effect, "reason", None),
        }
        for effect in effect_results
    ]
    base = dps_result.static_coverage_gaps(
        contributions=contributions,
        claimed_mod_ids=claimed_mod_ids,
        effect_statuses=statuses,
        declared_inactive_checks=declared_inactive_checks,
        skill_constants=skill_constants,
        per_attribute_problems=per_attribute_problems,
        extra_gaps=extra_gaps,
    )
    return dps_result.merge_runtime_gaps(base, unmodeled_chance_records=unmodeled_chance_records)


def defaults(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "states": {control["key"]: control["default"] for control in profile["controls"]},
        "target": dict(DEFAULT_TARGET),
    }


def validate_scenario(profile: dict[str, Any], scenario: Any) -> dict[str, Any]:
    """profile 侧的快速场景校验（兼容旧调用）；执行器会再按计划校验一次。"""
    if not isinstance(scenario, dict) or set(scenario) != {"states", "target"}:
        raise ValueError("场景格式不完整")
    declared = {control["key"]: control for control in profile["controls"]}
    if set(scenario["states"]) != set(declared):
        raise ValueError("Buff 配置包含缺失或不支持的项目")
    for key, control in declared.items():
        value = scenario["states"][key]
        if control["type"] == "boolean":
            if type(value) is not bool:
                raise ValueError("开关值无效：" + control["label"])
        elif type(value) is not int or not control["min"] <= value <= control["max"]:
            raise ValueError("层数超出范围：" + control["label"])
        if control.get("readonly") and value != control["default"]:
            raise ValueError("常驻状态不可关闭")
    target = scenario["target"]
    if set(target) != set(TARGET_KEYS):
        raise ValueError("目标配置无效")
    if type(target["monster_level"]) is not int or not 1 <= target["monster_level"] <= 100:
        raise ValueError("目标等级应为 1–100")
    if type(target["resistance"]) not in (float, int) or not math.isfinite(target["resistance"]) or not -1 <= target["resistance"] <= 0.75:
        raise ValueError("目标抗性应为 -100%–75%")
    if any(type(target[key]) is not bool for key in ("high_health", "boss", "blinded", "slowed")):
        raise ValueError("目标状态无效")
    return json.loads(json.dumps(scenario))


def compile_case(
    root: str | Path,
    case: str | Path | dict[str, Any],
    profile: Optional[dict[str, Any]] = None,
) -> plan_compiler.CompileResult:
    """打开上下文、编译当前案例的计划（供 CLI/重算/测试共享）。"""
    context = plan_compiler.prepare_context(case, root, profile=profile)
    try:
        profile_doc = context.profile
        if profile_doc is None:
            raise ValueError("当前案例没有 dps_profile，无法编译计算计划")
        return plan_compiler.compile_from_profile_v2(context, profile_doc)
    finally:
        context.close()


def calculate(
    root: Path,
    case: dict[str, Any],
    profile: dict[str, Any],
    states: dict[str, Any],
    target: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """对外计算入口：编译计划并交给唯一执行器。"""
    from . import effect_binding as binding

    root = Path(root)
    profile = binding.validate_profile(profile)
    compiled = compile_case(root, case, profile=profile)
    if not compiled.ok or compiled.plan is None:
        reasons = "; ".join(str(item.get("message")) for item in compiled.diagnostics[:5])
        raise ValueError(f"计算计划被拒绝或阻塞（{compiled.execution_status}/{compiled.readiness}）：{reasons}")
    scenario = {"states": states, "target": target or defaults(profile)["target"]}
    result = plan_executor.execute_plan(compiled.plan, scenario)
    return result


def dependency_fingerprints(root: Path, case: dict[str, Any], profile: dict[str, Any]) -> dict[str, str]:
    from . import calculation_route
    try:
        route = calculation_route.resolve_calculation_route(dict(case), root=root)
    except Exception:
        route = None
    if route and route.get("engine") == calculation_route.ENGINE_UNIFIED:
        return calculation_route.dependency_fingerprints(root=root, route=route, plan=None)

    """报告/重算绑定指纹：覆盖计划依赖（profile、规则、编译器/执行器实现）。"""
    from . import recalc

    root = Path(root)
    profile_path = case.get("dps_profile")
    profile_sha = (
        sha(root / profile_path)
        if profile_path and (root / profile_path).is_file()
        else hashlib.sha256(planmod.canonical_json(profile).encode("utf-8")).hexdigest()
    )
    conversion_rules = ""
    conversion = profile.get("attribute_conversion") or {}
    if isinstance(conversion, dict):
        conversion_rules = str(conversion.get("rules") or "")
    conversion_sha = sha(root / conversion_rules) if conversion_rules and (root / conversion_rules).is_file() else ""
    fingerprints = plan_handlers.implementation_fingerprint()
    ruleset = recalc.digest_document(
        {
            "plan_schema": planmod.SCHEMA,
            "profile": profile_sha,
            "attribute_conversion": conversion_sha,
            "compiler_fingerprint": fingerprints["compiler_fingerprint"],
            "executor_fingerprint": fingerprints["executor_fingerprint"],
        }
    )
    dataset = read(root / case["dataset"])["file_sha256"]
    return {
        "calc_input": sha(root / case["inputs"]["calculation_input"]),
        "ruleset": ruleset,
        "snapshot": sha(root / case["build"]["build_spec"]),
        "dataset": dataset,
    }


def run(root: Path, case: dict[str, Any], out: Path, port: int = 8765) -> dict[str, Any]:
    """兼容入口：委托统一 workflow 阶段，不再自行渲染/发布第二套产物。"""
    from . import calculation_route
    from .workflow import contract, stage, unified

    root = Path(root).resolve()
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise ValueError("拒绝覆盖历史输出")
    route = calculation_route.resolve_calculation_route(dict(case), root=root)
    if route.get("engine") != calculation_route.ENGINE_UNIFIED:
        raise ValueError(
            f"dps.run 只委托统一计划阶段；当前路由 engine={route.get('engine')!r} "
            "（legacy 计算请使用既有 CLI legacy 路径）"
        )
    case_document = {
        key: value for key, value in dict(case).items() if not str(key).startswith("_")
    }
    refs = [
        {
            "artifact_id": f"route:{ref.get('role') or 'source'}",
            "path": ref.get("path"),
            "sha256": ref.get("sha256"),
        }
        for ref in (route.get("source") or {}).get("input_refs") or []
        if ref.get("path") and ref.get("sha256")
    ]
    context = contract.StageContext(repo_root=root, output_root=out, adapters={})

    def request(stage_id: str, run_id: str, **params) -> contract.StageRequest:
        return contract.StageRequest(
            stage_id=stage_id,
            case_id=str(case.get("case_id")),
            run_id=run_id,
            input_refs=[dict(ref) for ref in refs],
            parameters={"case_document": case_document, **params},
        )

    for stage_id in ("compile_plan", "min_validate"):
        result = stage.execute_stage(request(stage_id, out.name), context)
        if result.execution_status != "completed":
            raise ValueError(f"统一计划阶段 {stage_id} 失败：{result.issues}")
    plan = unified.load_plan(out)
    endpoint = f"http://127.0.0.1:{int(port)}/recompute"
    for stage_id, params in (
        (
            "simulate",
            {
                "render_html": False,
                "plan_ref": {
                    "schema": plan.get("schema"),
                    "plan_id": plan.get("plan_id"),
                    "plan_sha256": plan.get("plan_sha256"),
                },
            },
        ),
        ("validate_result", {}),
        (
            "render_report",
            {
                "recalc_endpoint": endpoint,
                "target_dir": out.resolve().relative_to(root).as_posix(),
            },
        ),
    ):
        result = stage.execute_stage(request(stage_id, out.name, **params), context)
        if result.execution_status != "completed":
            raise ValueError(f"统一计划阶段 {stage_id} 失败：{result.issues}")
    validation = json.loads((out / "validation-result.json").read_text(encoding="utf-8"))
    if not validation.get("passed"):
        raise ValueError("统一计划结果校验未通过")
    unified.write_manifest(case=case, root=root, run_dir=out)
    return json.loads((out / "result.json").read_text(encoding="utf-8"))


__all__ = [
    "DAMAGE_TYPES",
    "DEFAULT_TARGET",
    "SCHEMA_RESULT",
    "calculate",
    "compile_case",
    "defaults",
    "dependency_fingerprints",
    "read",
    "run",
    "sha",
    "validate_scenario",
    "write",
]
