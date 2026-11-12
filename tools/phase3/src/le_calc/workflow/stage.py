"""Stage execution contract (``le-workflow-v1`` / P3).

``execute_stage(request, context)`` runs exactly one stage from the frozen
``stage_id`` list (workflow-interface §4.4) and returns a
:class:`le_calc.workflow.contract.StageResult`.

Design rules:

* stages are thin adapters over the existing pipeline steps — the same child
  commands as ``scripts/le_commands.py::reproduce`` (save import, ``le_build``
  export, affix-db pipeline, corrections, snapshot) and the existing
  ``domain.resolve_build`` / ``domain.simulate`` / ``domain.validate_result``
  functions; no calculation is re-implemented here;
* business functions never ``print`` or ``sys.exit`` — child output is captured
  and summarised in ``issues``/``metrics``, the CLI prints;
* a stage that cannot run returns ``failed``/``waiting`` with a structured issue
  (never silently skipped);
* P4: when ``context.adapters["stage_cache"]`` provides a
  :class:`le_calc.workflow.stage_cache.StageCache`, the stage is fingerprinted,
  looked up in the cache, and materialised on hit without executing the handler;
  without that adapter caching stays ``disabled`` (unit/P3 compatibility).
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from . import contract, domain, portable, stage_cache

#: Stage ids frozen by workflow-interface §4.4 (order is the pipeline order).
STAGE_IDS: tuple[str, ...] = (
    "freeze_input",
    "parse_input",
    "normalize_build",
    "export_calc_input",
    "apply_affix_ranges",
    "resolve_skills",
    "prepare_snapshot",
    "compile_plan",
    "min_validate",
    "simulate",
    "validate_result",
    "render_report",
    "commit",
)

#: child-module paths (mirrors le_commands.MODULE_PATHS without importing scripts).
REPO_ROOT = Path(__file__).resolve().parents[5]
MODULE_PATHS = {
    "le_calc": REPO_ROOT / "tools" / "phase3" / "src",
    "le_build": REPO_ROOT / "tools" / "build_import" / "src",
    "le_save_import": REPO_ROOT / "tools" / "save_import" / "src",
    "knowledge": REPO_ROOT / "tools" / "knowledge" / "src",
}

P3_UNIMPLEMENTED_STAGES = frozenset()


class StageError(RuntimeError):
    """Internal helper: a stage failure with a machine-readable code."""

    def __init__(self, code: str, message: str, *, required_input: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.required_input = required_input


# ---------------------------------------------------------------------------
# helpers shared by stage handlers
# ---------------------------------------------------------------------------
def _root(ctx: contract.StageContext) -> Path:
    return Path(ctx.repo_root).resolve()


def _run_dir(ctx: contract.StageContext) -> Path:
    return Path(ctx.output_root).resolve()


def _display(ctx: contract.StageContext, path: Path) -> str:
    try:
        return path.resolve().relative_to(_root(ctx)).as_posix()
    except ValueError:
        return "<external-path>"


def stable_result_digest(path: str | Path) -> str:
    """Semantic result digest for validation/commit binding.

    Excludes non-semantic run location/repro fields so the same numeric result
    remains accepted when cached artifacts are materialized under another run
    directory (P4-G1 render-only invalidation).
    """
    payload = _read_json(Path(path))
    data = dict(payload)
    run = dict(data.get("run") or {})
    for volatile in ("out_dir", "repro_command", "run_id", "generated_at"):
        run.pop(volatile, None)
    data["run"] = run
    return contract.fingerprint(data)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _case(request: contract.StageRequest, ctx: contract.StageContext) -> dict[str, Any]:
    params = request.parameters or {}
    if isinstance(params.get("case_document"), Mapping):
        document = copy.deepcopy(dict(params["case_document"]))
        document.pop("_root", None)
        document.pop("_dir", None)
        document.pop("_path", None)
        return document
    case_ref = params.get("case_ref")
    if not case_ref:
        raise StageError("missing_case_ref", "StageRequest.parameters.case_ref 未提供", required_input={"kind": "case", "artifact_hint": "case.json"})
    path = Path(str(case_ref))
    if not path.is_absolute():
        path = _root(ctx) / path
    if not path.is_file():
        raise StageError("case_not_found", f"案例配置不存在：{case_ref}",
                         required_input={"kind": "case", "path": case_ref})
    document = _read_json(path)
    document["_path"] = _display(ctx, path)
    return document


def _case_model(case: Mapping[str, Any]) -> str:
    return str(case.get("model") or "black-hole").strip().lower()


def _case_path(case: Mapping[str, Any], dotted: str, ctx: contract.StageContext) -> Optional[Path]:
    node: Any = case
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    if not isinstance(node, (str, Path)) or not str(node):
        return None
    path = Path(str(node))
    return path if path.is_absolute() else (_root(ctx) / path)


def _relative_arg(path: Path, ctx: contract.StageContext) -> str:
    display = _display(ctx, path)
    return str(path.resolve()) if display == "<external-path>" else display


def _default_run_child(
    cmd: Sequence[str], *, pythonpath: Sequence[Path] = (), cwd: Optional[Path] = None
) -> dict[str, Any]:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    if pythonpath:
        env["PYTHONPATH"] = os.pathsep.join(str(Path(item).resolve()) for item in pythonpath)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    completed = subprocess.run(
        [str(part) for part in cmd],
        cwd=str(cwd or REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
    }


def _runner(ctx: contract.StageContext) -> Callable[..., dict[str, Any]]:
    adapters = getattr(ctx, "adapters", None) or {}
    runner = adapters.get("run_child")
    return runner if callable(runner) else _default_run_child


def _run_step(
    ctx: contract.StageContext,
    label: str,
    cmd: Sequence[str],
    *,
    pythonpath: Sequence[Path] = (),
) -> dict[str, Any]:
    result = _runner(ctx)(cmd, pythonpath=pythonpath, cwd=_root(ctx))
    if int(result.get("returncode", 0)) != 0:
        tail = (result.get("stderr") or result.get("stdout") or "").strip().splitlines()[-4:]
        raise StageError(
            "child_command_failed",
            f"{label} 失败（退出码 {result.get('returncode')}）：{' | '.join(tail)}",
        )
    return result


def _python() -> str:
    return sys.executable


def _steps_dir(ctx: contract.StageContext) -> Path:
    return _run_dir(ctx) / "steps"


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, payload: Any) -> Path:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def _require_file(path: Optional[Path], code: str, message: str) -> Path:
    if path is None or not path.is_file():
        raise StageError(code, message)
    return path


def _params(request: contract.StageRequest) -> dict[str, Any]:
    return dict(request.parameters or {})


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------
def stage_freeze_input(request: contract.StageRequest, ctx: contract.StageContext) -> dict[str, Any]:
    """Freeze the declared input files with sha256 (no computation)."""

    case = _case(request, ctx)
    inputs: dict[str, Any] = {}
    declared = {
        "case": Path(case["_path"]),
        "bd": _case_path(case, "inputs.bd", ctx),
        "calculation_input": _case_path(case, "inputs.calculation_input", ctx),
        "snapshot": _case_path(case, "inputs.snapshot", ctx),
        "scenario": _case_path(case, "inputs.scenario", ctx),
        "build_spec": _case_path(case, "build.build_spec", ctx),
        "ruleset": _case_path(case, "ruleset.path", ctx),
        "conditional_registry": _case_path(case, "conditional_registry.path", ctx),
        "character_save": _case_path(case, "source.character_save", ctx),
        "stash": _case_path(case, "source.stash", ctx),
        "planner_build_response": _case_path(case, "source.planner_build_response", ctx),
        "tool_native_wire": _case_path(case, "source.tool_native_wire", ctx),
    }
    params = _params(request)
    for key in ("save", "planner", "stash"):
        if params.get(key):
            path = Path(str(params[key]))
            if not path.is_absolute():
                path = _root(ctx) / path
            declared[key] = path
    for role, path in declared.items():
        if path is None:
            inputs[role] = None
            continue
        path = Path(path)
        if not path.is_absolute():
            path = _root(ctx) / path
        inputs[role] = {
            "path": _display(ctx, path),
            "exists": path.is_file(),
            "sha256": contract.sha256_file(path) if path.is_file() else None,
        }
    missing = [role for role, item in inputs.items() if item is not None and not item["exists"]]
    freeze = {
        "schema_version": "le-input-freeze-v1",
        "case_id": case.get("case_id"),
        "case_ref": case["_path"],
        "inputs": inputs,
        "input_fingerprint": contract.fingerprint(inputs),
        "missing": missing,
    }
    out = _write_json(_run_dir(ctx) / "input-freeze.json", freeze)
    issues: list[dict[str, Any]] = []
    status = contract.EXECUTION_COMPLETED
    readiness = contract.READY
    if missing:
        readiness = contract.NEEDS_INPUT
        issues.append(
            contract.make_issue(
                "missing_input_files",
                f"输入冻结缺少文件：{', '.join(missing)}",
                severity=contract.SEVERITY_ERROR,
                related_ids=missing,
                required_input={"kind": "input_files", "artifact_hint": "case.json inputs/source"},
            )
        )
    return {
        "execution_status": status,
        "readiness": readiness,
        "outputs": [out],
        "issues": issues,
        "input_fingerprint": freeze["input_fingerprint"],
        "metrics": {"inputs": len(inputs)},
    }


def stage_parse_input(request: contract.StageRequest, ctx: contract.StageContext) -> dict[str, Any]:
    """Raw save (+ planner/stash) → ``bd.json`` via ``le_save_import build``."""

    case = _case(request, ctx)
    params = _params(request)
    steps = _ensure_dir(_steps_dir(ctx) / "01-save-import")
    raw_save = params.get("save") or _case_path(case, "source.character_save", ctx)
    planner = params.get("planner") or _case_path(case, "source.planner_build_response", ctx)
    stash = params.get("stash") or _case_path(case, "source.stash", ctx)
    affix_db = _case_path(case, "affix_db", ctx)
    if raw_save is None:
        # planner-only route: publish the BuildSpec revision from the archived payload.
        if planner is None:
            raise StageError(
                "missing_raw_input",
                "prepare 需要 --save 或案例 source.character_save / planner_build_response",
                required_input={"kind": "save_or_planner", "artifact_hint": "data/raw/saves/ 或 planner payload"},
            )
        _run_step(
            ctx,
            "le_build import（planner payload）",
            [
                _python(),
                "-m",
                "le_build",
                "import",
                "--file",
                _relative_arg(Path(planner), ctx),
                "--offline",
            ],
            pythonpath=[MODULE_PATHS["le_build"]],
        )
        out = _write_json(
            _run_dir(ctx) / "input-freeze.json",
            {
                "schema_version": "le-input-freeze-v1",
                "case_id": case.get("case_id"),
                "route": "planner",
                "planner": _display(ctx, Path(planner)),
            },
        )
        return {
            "execution_status": contract.EXECUTION_COMPLETED,
            "readiness": contract.PARTIAL,
            "outputs": [out],
            "issues": [
                contract.make_issue(
                    "planner_route_partial",
                    "planner-only 路线已发布 BuildSpec revision；请确认案例 build 声明后再继续导出",
                    severity=contract.SEVERITY_WARNING,
                )
            ],
            "metrics": {},
        }
    save_path = Path(raw_save)
    if not save_path.is_absolute():
        save_path = _root(ctx) / save_path
    cmd = [
        _python(),
        "-m",
        "le_save_import",
        "build",
        "--save",
        _relative_arg(save_path, ctx),
        "--out",
        _relative_arg(steps, ctx),
    ]
    for flag, value in (("--planner", planner), ("--stash", stash), ("--affix-db", affix_db)):
        if value is not None:
            cmd += [flag, _relative_arg(Path(value), ctx)]
    _run_step(ctx, "le_save_import build", cmd, pythonpath=[MODULE_PATHS["le_save_import"]])
    bd_path = _require_file(steps / "bd.json", "parse_output_missing", "le_save_import 未生成 bd.json")
    # Same portability normalisation as reproduce: generated step artefacts must
    # not leak machine absolute paths (drive letters / UNC).  The implementation
    # is shared with reproduce (workflow.portable -> pipeline _common helpers).
    sanitized = portable.sanitize_directory(steps, _root(ctx))
    return {
        "execution_status": contract.EXECUTION_COMPLETED,
        "readiness": contract.READY,
        "outputs": [bd_path],
        "issues": [],
        "metrics": {
            "portable_replacements": sum(int(item["replacements"]) for item in sanitized),
            "portable_files": len(sanitized),
        },
    }


def stage_normalize_build(request: contract.StageRequest, ctx: contract.StageContext) -> dict[str, Any]:
    """Verify the pinned BuildSpec revision and record the frozen build identity."""

    case = _case(request, ctx)
    build = case.get("build") or {}
    build_spec = _case_path(case, "build.build_spec", ctx)
    if build_spec is None or not build_spec.is_file():
        raise StageError("missing_build_spec", "案例缺少 build.build_spec 或文件不存在")
    revision = _read_json(build_spec)
    meta = ((revision.get("meta") or {}).get("revision") or {})
    problems: list[str] = []
    if meta.get("revision_id") != build.get("revision_id"):
        problems.append("revision_id")
    if meta.get("content_sha256") != build.get("content_sha256"):
        problems.append("content_sha256")
    if contract.sha256_file(build_spec) != (build.get("build_spec_sha256") or ""):
        problems.append("build_spec_sha256")
    out = _write_json(
        _run_dir(ctx) / "steps" / "00-build-selection.json",
        {
            "schema_version": "le-build-selection-v1",
            "build_id": build.get("build_id"),
            "revision_id": build.get("revision_id"),
            "content_sha256": build.get("content_sha256"),
            "build_spec": _display(ctx, build_spec),
            "build_spec_sha256": contract.sha256_file(build_spec),
        },
    )
    if problems:
        raise StageError(
            "build_binding_mismatch", f"BuildSpec revision 与案例声明不一致：{problems}"
        )
    return {
        "execution_status": contract.EXECUTION_COMPLETED,
        "readiness": contract.READY,
        "outputs": [out],
        "issues": [],
        "metrics": {},
    }


def stage_export_calc_input(request: contract.StageRequest, ctx: contract.StageContext) -> dict[str, Any]:
    """Pinned BuildSpec revision → ``calculation-input-v1`` via ``le_build export``."""

    case = _case(request, ctx)
    build = case.get("build") or {}
    out = _steps_dir(ctx) / "02-calculation-input.json"
    _ensure_dir(out.parent)
    _run_step(
        ctx,
        "le_build export",
        [
            _python(),
            "-m",
            "le_build",
            "export",
            "--build",
            str(build.get("build_id")),
            "--revision",
            str(build.get("revision_id")),
            "--out",
            str(out),
        ],
        pythonpath=[MODULE_PATHS["le_build"]],
    )
    _require_file(out, "export_output_missing", "le_build export 未生成 calculation-input")
    return {
        "execution_status": contract.EXECUTION_COMPLETED,
        "readiness": contract.READY,
        "outputs": [out],
        "issues": [],
        "metrics": {},
    }


def stage_apply_affix_ranges(request: contract.StageRequest, ctx: contract.StageContext) -> dict[str, Any]:
    """Per-base affix range recalculation via ``run_affix_db_pipeline.py``."""

    case = _case(request, ctx)
    calc_input = _steps_dir(ctx) / "02-calculation-input.json"
    _require_file(calc_input, "missing_export", "缺少 02-calculation-input.json（先运行 export_calc_input）")
    out = _steps_dir(ctx) / "03-calculation-input.affix-db.json"
    report = _steps_dir(ctx) / "03-affix-db-recompute.json"
    case_path = _root(ctx) / str(case["_path"])
    _run_step(
        ctx,
        "run_affix_db_pipeline.py",
        [
            _python(),
            str(_root(ctx) / "tools" / "save_import" / "scripts" / "pipeline" / "run_affix_db_pipeline.py"),
            "--case",
            str(case_path),
            "--calc-input",
            str(calc_input),
            "--out",
            str(out),
            "--report",
            str(report),
        ],
        pythonpath=[MODULE_PATHS["le_calc"], MODULE_PATHS["le_save_import"]],
    )
    _require_file(out, "affix_output_missing", "affix-db 重算未生成计算输入")
    return {
        "execution_status": contract.EXECUTION_COMPLETED,
        "readiness": contract.READY,
        "outputs": [out, report],
        "issues": [],
        "metrics": {},
    }


def _knowledge_lookup_factory(ctx: contract.StageContext) -> Callable[[dict], dict]:
    knowledge_src = str(MODULE_PATHS["knowledge"])
    if knowledge_src not in sys.path:
        sys.path.insert(0, knowledge_src)
    from le_knowledge import search  # noqa: PLC0415

    # Keep the generated knowledge index in the repository cache (it is a
    # rebuildable cache, never a run artifact); ``le_knowledge`` validates it.
    index_path = _root(ctx) / "data" / "cache" / "knowledge.sqlite"

    def lookup(query: dict[str, Any]) -> dict[str, Any]:
        return search(
            query,
            knowledge_dir=str(_root(ctx) / "knowledge"),
            index_path=str(index_path),
            root=str(_root(ctx)),
        )

    return lookup


def stage_resolve_skills(request: contract.StageRequest, ctx: contract.StageContext) -> dict[str, Any]:
    """Mechanism resolution + analysis-plan / coverage / research-queue artefacts."""

    from . import artifacts  # noqa: PLC0415

    case = _case(request, ctx)
    params = _params(request)
    main_skill = str(params.get("main_skill") or "").strip()
    if not main_skill:
        ability = params.get("ability_id")
        if ability:
            main_skill = str(ability)
        else:
            raise StageError(
                "missing_main_skill",
                "resolve_skills 需要 parameters.main_skill（或 ability_id）",
                required_input={"kind": "main_skill", "artifact_hint": "--main-skill <ability-id>"},
            )
    # calc input for scope: prefer the corrected/affix-db step when present.
    steps = _steps_dir(ctx)
    calc_input = steps / "04-calculation-input.corrected.json"
    if not calc_input.is_file():
        calc_input = steps / "03-calculation-input.affix-db.json"
    if not calc_input.is_file():
        calc_input = steps / "02-calculation-input.json"
    calc_rel = _display(ctx, calc_input) if calc_input.is_file() else None
    knowledge = {
        "knowledge_dir": "knowledge",
        "index_path": "data/cache/knowledge.sqlite",
        "lookup": _knowledge_lookup_factory(ctx),
    }
    request_payload = {
        "schema_version": contract.SCHEMA_RESOLVE_BUILD_REQUEST,
        "case_id": case.get("case_id"),
        "case_ref": case["_path"],
        "main_skill": {"ability_id": main_skill},
        "knowledge": knowledge,
    }
    if calc_rel:
        request_payload["input_refs"] = [
            {"role": "calculation_input", "path": calc_rel, "sha256": contract.sha256_file(calc_input)}
        ]
    try:
        resolved = domain.resolve_build(request_payload, root=_root(ctx))
    except contract.ContractError as exc:
        raise StageError("resolve_build_failed", f"resolve_build 失败：{exc}") from exc
    manifest = artifacts.write_analysis_artifacts(
        resolved, _run_dir(ctx), root=_root(ctx), overwrite=True
    )
    outputs = [_root(ctx) / path for path in manifest["files"].values()]
    readiness_map = {
        contract.READY: contract.READY,
        contract.PARTIAL: contract.PARTIAL,
        contract.NEEDS_RESEARCH: contract.NEEDS_RESEARCH,
        contract.NEEDS_INPUT: contract.NEEDS_INPUT,
    }
    readiness = readiness_map.get(resolved.get("readiness"), contract.PARTIAL)
    issues = [
        contract.make_issue(
            "unknown_skill_mapping" if not resolved.get("analysis_plan", {}).get("skill_template")
            else "analysis_plan_partial",
            (
                f"主技能 {main_skill} 未绑定技能模板：只完成数据准备，需要映射/研究"
                if not resolved.get("analysis_plan", {}).get("skill_template")
                else f"技能映射已完成，存在 {len(resolved.get('gaps') or [])} 个非阻塞缺口"
            ),
            severity=(
                contract.SEVERITY_WARNING
            ),
            related_ids=[main_skill],
            required_input=(
                {"kind": "skill_template", "artifact_hint": "knowledge/skills/"}
                if not resolved.get("analysis_plan", {}).get("skill_template")
                else None
            ),
        )
    ]
    return {
        "execution_status": contract.EXECUTION_COMPLETED,
        "readiness": readiness,
        "outputs": outputs,
        "issues": issues,
        "metrics": {
            "bound_mechanisms": len(resolved.get("bound_mechanisms") or []),
            "gaps": len(resolved.get("gaps") or []),
        },
    }


def _route(request: contract.StageRequest, ctx: contract.StageContext) -> dict[str, Any]:
    from .. import calculation_route  # noqa: PLC0415 - lazy to keep workflow import light

    return calculation_route.resolve_calculation_route(_case(request, ctx), root=_root(ctx))


def _is_unified(request: contract.StageRequest, ctx: contract.StageContext) -> bool:
    from .. import calculation_route  # noqa: PLC0415

    return _route(request, ctx).get("engine") == calculation_route.ENGINE_UNIFIED


def stage_compile_plan(request: contract.StageRequest, ctx: contract.StageContext) -> dict[str, Any]:
    """compile_plan：统一路径的计划编译阶段（legacy 路由明确拒绝）。"""
    from .. import calculation_route  # noqa: PLC0415
    from . import unified  # noqa: PLC0415

    case = _case(request, ctx)
    params = _params(request)
    if params.get("main_skill"):
        case = dict(case)
        case["main_skill"] = dict(case.get("main_skill") or {}, ability_id=str(params["main_skill"]))
    route = _route(request, ctx)
    if route.get("engine") != calculation_route.ENGINE_UNIFIED:
        return {
            "execution_status": contract.EXECUTION_FAILED,
            "readiness": contract.PARTIAL,
            "outputs": [],
            "issues": [
                contract.make_issue(
                    "unified_route_required",
                    f"compile_plan 只服务统一计划路由；当前路由 engine={route.get('engine')!r}",
                    severity=contract.SEVERITY_ERROR,
                )
            ],
            "metrics": {"route_engine": route.get("engine")},
        }
    params = _params(request)
    from ..calculation_plan import is_sha256  # noqa: PLC0415 - lazy, mirrors calculation_route import

    overrides: dict[str, Any] = {}
    for key, target in (
        ("calc_input_ref", "calculation_input"),
        ("build_ref", "build_spec"),
    ):
        value = params.get(key)
        if not value:
            continue
        sha_key = f"{key.replace('_ref', '')}_sha256"
        sha_value = params.get(sha_key)
        # 交接 override 必须自带合法 sha256：缺失/空串/格式非法一律拒绝，
        # 不得静默降级为“只按路径覆盖”（那会绕过 prepare_context 的交接校验）。
        if not is_sha256(sha_value):
            return {
                "execution_status": contract.EXECUTION_FAILED,
                "readiness": contract.PARTIAL,
                "outputs": [],
                "issues": [
                    contract.make_issue(
                        "override_sha256_required",
                        f"{key} 已声明，但 {sha_key} 缺失或不是合法的 64 位小写 SHA-256；"
                        "交接 override 必须同时提供 artifact sha256",
                        severity=contract.SEVERITY_ERROR,
                    )
                ],
                "metrics": {"override": target},
            }
        overrides[target] = str(value)
        overrides[f"{target}_sha256"] = str(sha_value)
    result = unified.compile_stage(
        case,
        root=_root(ctx),
        run_dir=_run_dir(ctx),
        input_overrides=overrides,
    )
    outputs = [Path(path) for path in result.get("outputs") or []]
    return {
        "execution_status": result["execution_status"],
        "readiness": result["readiness"],
        "outputs": outputs,
        "issues": result["issues"],
        "metrics": result.get("metrics") or {},
    }


def stage_prepare_snapshot(request: contract.StageRequest, ctx: contract.StageContext) -> dict[str, Any]:
    """Corrections + frozen input snapshot (the snapshot stage of reproduce)."""

    case = _case(request, ctx)
    model = _case_model(case)
    steps = _steps_dir(ctx)
    if model == "research":
        ability = str(_params(request).get("main_skill") or (case.get("main_skill") or {}).get("ability_id") or "")
        return {"execution_status": contract.EXECUTION_WAITING,
                "readiness": contract.NEEDS_RESEARCH, "outputs": [],
                "issues": [contract.make_issue("skill_model_required",
                    f"主技能 {ability} 的计算适配器尚未实现；按当前案例 research-queue 研究，不回退旧模型",
                    severity=contract.SEVERITY_WARNING, related_ids=[ability],
                    required_input={"kind": "skill_model", "ability_id": ability,
                                    "artifact_hint": "knowledge/skills/"})],
                "metrics": {"short_circuit": "research_model"}}
    if model == "hit":
        # ``hit-snapshot`` requires the main-skill specialization.  When
        # resolve_skills reported an unbound skill template this is a known
        # mapping gap, not a snapshot failure: return waiting + required_input
        # so earlier data preparation stays valid and the caller can continue
        # with needs_research.  Event-model snapshots do not need the
        # specialization, so they keep their existing behaviour.
        plan_path = _run_dir(ctx) / "analysis-plan.json"
        if plan_path.is_file():
            plan = _read_json(plan_path)
            if plan.get("skill_template") is None:
                ability = str(
                    ((plan.get("main_skill") or {}).get("ability_id"))
                    or (_params(request).get("main_skill"))
                    or ""
                )
                return {
                    "execution_status": contract.EXECUTION_WAITING,
                    "readiness": contract.NEEDS_RESEARCH,
                    "outputs": [],
                    "issues": [
                        contract.make_issue(
                            "skill_mapping_required",
                            f"主技能 {ability or '<unknown>'} 未绑定技能模板："
                            "hit 快照需要 specialization；先完成技能映射/研究后再生成快照",
                            severity=contract.SEVERITY_WARNING,
                            related_ids=[ability] if ability else [],
                            required_input={
                                "kind": "skill_mapping",
                                "ability_id": ability,
                                "artifact_hint": "knowledge/skills/",
                            },
                        )
                    ],
                    "metrics": {"short_circuit": "skill_template_unbound"},
                }
    affix_input = steps / "03-calculation-input.affix-db.json"
    _require_file(affix_input, "missing_affix_output", "缺少 03 affix-db 计算输入")
    case_path = _root(ctx) / str(case["_path"])
    corrected = steps / "04-calculation-input.corrected.json"
    corrections_report = steps / "04-save-build-corrections.json"
    _run_step(
        ctx,
        "apply_corrections.py",
        [
            _python(),
            str(_root(ctx) / "tools" / "save_import" / "scripts" / "pipeline" / "apply_corrections.py"),
            "--case",
            str(case_path),
            "--input",
            str(affix_input),
            "--out",
            str(corrected),
            "--report",
            str(corrections_report),
        ],
        pythonpath=[MODULE_PATHS["le_calc"], MODULE_PATHS["le_save_import"]],
    )
    _require_file(corrected, "corrections_output_missing", "apply_corrections 未生成 corrected 计算输入")
    if model == "hit":
        snapshot_out = steps / "05-input-snapshot.hit.json"
        _run_step(
            ctx,
            "le_calc hit-snapshot",
            [
                _python(),
                "-m",
                "le_calc",
                "hit-snapshot",
                "--case",
                str(case_path),
                "--calc-input",
                str(corrected),
                "--out",
                str(snapshot_out),
            ],
            pythonpath=[MODULE_PATHS["le_calc"]],
        )
    else:
        snapshot_out = steps / "05-input-snapshot.json"
        shipped = _case_path(case, "inputs.snapshot", ctx)
        cmd = [
            _python(),
            str(_root(ctx) / "tools" / "save_import" / "scripts" / "pipeline" / "make_snapshot.py"),
            "--case",
            str(case_path),
            "--calc-input",
            str(corrected),
            "--out",
            str(snapshot_out),
        ]
        if shipped is not None:
            cmd += ["--compare", str(shipped)]
        _run_step(
            ctx,
            "make_snapshot.py",
            cmd,
            pythonpath=[MODULE_PATHS["le_calc"], MODULE_PATHS["le_save_import"]],
        )
    _require_file(snapshot_out, "snapshot_output_missing", "快照阶段未生成 input-snapshot")
    return {
        "execution_status": contract.EXECUTION_COMPLETED,
        "readiness": contract.READY,
        "outputs": [corrected, corrections_report, snapshot_out],
        "issues": [],
        "metrics": {},
    }


def stage_min_validate(request: contract.StageRequest, ctx: contract.StageContext) -> dict[str, Any]:
    """Structural/binding validation before any calculation (no DPS claims)."""

    case = _case(request, ctx)
    run_dir = _run_dir(ctx)
    if _is_unified(request, ctx):
        route = _route(request, ctx)
        checks: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []

        def add_unified(name: str, ok: bool, detail: str) -> None:
            checks.append({"id": name, "status": "PASS" if ok else "FAIL", "detail": detail})
            if not ok:
                issues.append(
                    contract.make_issue(
                        "min_validate_failed", f"{name}: {detail}", related_ids=[name]
                    )
                )

        add_unified("case.schema", case.get("schema") == "le-case-v1", str(case.get("schema")))
        add_unified("route.engine", route.get("engine") == "unified_dps", str(route.get("engine")))
        add_unified("route.status", route.get("status") == "ready", str(route.get("status")))
        plan_path = run_dir / "calculation-plan.json"
        add_unified("artifact.calculation-plan", plan_path.is_file(), _display(ctx, plan_path))
        add_unified(
            "artifact.plan-diagnostics",
            (run_dir / "plan-diagnostics.json").is_file(),
            _display(ctx, run_dir / "plan-diagnostics.json"),
        )
        add_unified(
            "artifact.compile-result",
            (run_dir / "compile-result.json").is_file(),
            _display(ctx, run_dir / "compile-result.json"),
        )
        if plan_path.is_file():
            from .. import calculation_plan as planmod  # noqa: PLC0415

            try:
                plan = _read_json(plan_path)
                planmod.verify_plan_integrity(plan)
                if str(plan.get("schema")) == "le-calculation-plan-v4":
                    from .. import plan_v4  # noqa: PLC0415
                    plan_v4.validate_plan_v4(plan)
                    structural = []
                else:
                    structural = planmod.validate_plan_structure(plan)
                add_unified(
                    "plan.integrity",
                    True,
                    str(plan.get("plan_sha256")),
                )
                add_unified(
                    "plan.structure",
                    not structural,
                    f"issues={len(structural)}",
                )
            except Exception as exc:  # noqa: BLE001 - explicit gate failure
                add_unified("plan.integrity", False, str(exc))
        payload = {
            "schema_version": "le-min-validate-v2",
            "mode": "unified",
            "route": route,
            "checks": checks,
        }
        out = _write_json(run_dir / "min-validate.json", payload)
        return {
            "execution_status": contract.EXECUTION_COMPLETED if not issues else contract.EXECUTION_FAILED,
            "readiness": contract.READY if not issues else contract.PARTIAL,
            "outputs": [out],
            "issues": issues,
            "metrics": {"checks": len(checks), "mode": "unified"},
        }
    checks: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"id": name, "status": "PASS" if ok else "FAIL", "detail": detail})
        if not ok:
            issues.append(
                contract.make_issue("min_validate_failed", f"{name}: {detail}",
                                    related_ids=[name])
            )

    add("case.schema", case.get("schema") == "le-case-v1", str(case.get("schema")))
    expected = _case_path(case, "expected", ctx)
    add("case.expected", expected is not None and expected.is_file(),
        _display(ctx, expected) if expected else "未声明 expected")
    for role, dotted in (
        ("snapshot", "inputs.snapshot"),
        ("calculation_input", "inputs.calculation_input"),
        ("build_spec", "build.build_spec"),
        ("ruleset", "ruleset.path"),
    ):
        path = _case_path(case, dotted, ctx)
        add(f"case.{role}", path is not None and path.is_file(),
            _display(ctx, path) if path else "缺失")
    for name in ("analysis-plan.json", "coverage.json", "research-queue.json"):
        path = run_dir / name
        add(f"artifacts.{name}", path.is_file(), _display(ctx, path))
    payload = {"schema_version": "le-min-validate-v1", "checks": checks}
    out = _write_json(run_dir / "min-validate.json", payload)
    return {
        "execution_status": contract.EXECUTION_COMPLETED if not issues else contract.EXECUTION_FAILED,
        "readiness": contract.READY if not issues else contract.PARTIAL,
        "outputs": [out],
        "issues": issues,
        "metrics": {"checks": len(checks)},
    }


def stage_simulate(request: contract.StageRequest, ctx: contract.StageContext) -> dict[str, Any]:
    """Thin wrapper over ``domain.simulate`` (same engine path as ``le run``)."""

    case = _case(request, ctx)
    params = _params(request)
    if _is_unified(request, ctx):
        from . import unified  # noqa: PLC0415

        plan_ref = params.get("plan_ref") or {}
        scenario = params.get("scenario")
        result = unified.simulate_stage(
            case,
            root=_root(ctx),
            run_dir=_run_dir(ctx),
            plan_ref=plan_ref,
            scenario=scenario,
        )
        outputs = [Path(path) for path in result.get("outputs") or []]
        return {
            "execution_status": result["execution_status"],
            "readiness": result["readiness"],
            "outputs": outputs,
            "issues": result["issues"],
            "metrics": result.get("metrics") or {},
        }
    render_html = bool(params.get("render_html", True))
    payload = {
        "schema_version": contract.SCHEMA_SIMULATE_REQUEST,
        "case_id": case.get("case_id"),
        "case_ref": case["_path"],
        "run_id": request.run_id,
        "output_dir": _display(ctx, _run_dir(ctx)),
        "output_dir_display": str(
            (ctx.adapters or {}).get("run_output_display")
            or params.get("output_dir_display")
            or _display(ctx, _run_dir(ctx))
        ),
        "render_html": render_html,
        "generated_at": params.get("generated_at"),
    }
    for key in ("snapshot_ref", "ruleset_ref", "scenario_ref", "calc_input_ref", "build_ref"):
        if params.get(key):
            payload.setdefault("inputs", {})[key] = params[key]
    result = domain.simulate(payload, root=_root(ctx))
    outputs = [
        _root(ctx) / path for path in (result.get("artifacts") or {}).values() if path
    ]
    issues = [dict(issue) for issue in result.get("issues") or []]
    status = (
        contract.EXECUTION_COMPLETED
        if result.get("readiness") in (contract.READY, contract.PARTIAL)
        else contract.EXECUTION_FAILED
    )
    return {
        "execution_status": status,
        "readiness": result.get("readiness"),
        "outputs": outputs,
        "issues": issues,
        "metrics": {"run_id": result.get("run_id"), "model_id": result.get("model_id")},
    }


def stage_validate_result(request: contract.StageRequest, ctx: contract.StageContext) -> dict[str, Any]:
    """Thin wrapper over ``domain.validate_result`` (shared expected comparison)."""

    case = _case(request, ctx)
    params = _params(request)
    if _is_unified(request, ctx):
        from . import unified  # noqa: PLC0415

        result = unified.validate_stage(case, root=_root(ctx), run_dir=_run_dir(ctx))
        return {
            "execution_status": result["execution_status"],
            "readiness": result["readiness"],
            "outputs": [Path(path) for path in result["outputs"]],
            "issues": result["issues"],
            "metrics": {"checks": len((result.get("payload") or {}).get("checks") or [])},
        }
    payload = {
        "case_id": case.get("case_id"),
        "case_ref": case["_path"],
        "run_dir": _display(ctx, _run_dir(ctx)),
    }
    if params.get("checks"):
        payload["checks"] = list(params["checks"])
    validation = domain.validate_result(payload, root=_root(ctx))
    payload_doc = validation.to_dict()
    run_dir = _run_dir(ctx)
    result_path = run_dir / "result.json"
    if result_path.is_file():
        payload_doc["result_ref"] = str(
            (ctx.adapters or {}).get("run_result_display")
            or params.get("result_ref_display")
            or _display(ctx, result_path)
        )
        payload_doc["result_sha256"] = stable_result_digest(result_path)
    out = _write_json(run_dir / "validation-result.json", payload_doc)
    issues = [
        contract.make_issue(
            "validation_failed",
            f"{error.get('id')}: {error.get('message')}",
            severity=contract.SEVERITY_ERROR,
            related_ids=[str(error.get("id"))],
        )
        for error in payload_doc.get("errors") or []
    ]
    return {
        "execution_status": (
            contract.EXECUTION_COMPLETED if validation.passed else contract.EXECUTION_FAILED
        ),
        "readiness": contract.READY if validation.passed else contract.PARTIAL,
        "outputs": [out],
        "issues": issues,
        "metrics": {"checks": len(payload_doc.get("checks") or [])},
    }


def stage_render_report(request: contract.StageRequest, ctx: contract.StageContext) -> dict[str, Any]:
    """Render the offline report independently from simulate (P4-G1).

    When the CLI/run orchestration supplies a ``render_report`` adapter, it owns
    the shared renderer/endpoint/capabilities wiring; otherwise this stage keeps
    the P3 verification fallback (report already rendered by the caller).
    """

    adapter = (ctx.adapters or {}).get("render_report")
    if callable(adapter):
        return adapter(request, ctx)
    case = _case(request, ctx)
    if _is_unified(request, ctx):
        from . import unified  # noqa: PLC0415

        params = _params(request)
        target = params.get("target_dir")
        display_dir = (_root(ctx) / str(target)) if target else None
        result = unified.render_stage(
            case,
            root=_root(ctx),
            run_dir=_run_dir(ctx),
            endpoint=str(params.get("recalc_endpoint") or "http://127.0.0.1:8765"),
            display_dir=display_dir,
        )
        return {
            "execution_status": result["execution_status"],
            "readiness": result["readiness"],
            "outputs": [Path(path) for path in result["outputs"]],
            "issues": result["issues"],
            "metrics": result.get("metrics") or {},
        }
    model = _case_model(case)
    candidates = (
        ["flame-reave-damage.html"] if model == "hit" else ["black-hole-damage.html"]
    )
    found = [path for name in candidates if (path := _run_dir(ctx) / name).is_file()]
    if not found:
        return {
            "execution_status": contract.EXECUTION_WAITING,
            "readiness": contract.PARTIAL,
            "outputs": [],
            "issues": [
                contract.make_issue(
                    "report_not_rendered",
                    "run 目录没有离线 HTML 报告；simulate 需 render_html=true 或先运行 le run",
                    required_input={"kind": "simulate_render_html", "artifact_hint": "render_html: true"},
                )
            ],
            "metrics": {},
        }
    return {
        "execution_status": contract.EXECUTION_COMPLETED,
        "readiness": contract.READY,
        "outputs": found,
        "issues": [],
        "metrics": {},
    }


def stage_commit(request: contract.StageRequest, ctx: contract.StageContext) -> dict[str, Any]:
    """P4 stage: gated idempotent commit of accepted results.

    The orchestration layer supplies a ``commit`` adapter because the target
    layout/candidate lifecycle belongs to the CLI/run integration; the adapter
    must verify ``validation-result.json`` (passed + result sha binding) before
    invoking ``commit_operation``/``publish_run_dir``.
    """

    adapter = (ctx.adapters or {}).get("commit")
    if callable(adapter):
        return adapter(request, ctx)
    raise StageError(
        "commit_adapter_missing",
        "commit 阶段需要 run 编排提供 commit adapter（验证门禁 + 幂等发布）",
        required_input={"kind": "commit_adapter", "artifact_hint": "le_commands run staged"},
    )


STAGES: dict[str, dict[str, Any]] = {
    "freeze_input": {
        "handler": stage_freeze_input,
        "description": "冻结输入指纹（case/输入/快照/ruleset/source），无计算",
    },
    "parse_input": {
        "handler": stage_parse_input,
        "description": "原始存档/planner → bd.json（le_save_import build / le_build import）",
    },
    "normalize_build": {
        "handler": stage_normalize_build,
        "description": "校验固定 BuildSpec revision 与案例绑定",
    },
    "export_calc_input": {
        "handler": stage_export_calc_input,
        "description": "BuildSpec revision → calculation-input-v1（le_build export）",
    },
    "apply_affix_ranges": {
        "handler": stage_apply_affix_ranges,
        "description": "按 affix-db 每基底范围重算（run_affix_db_pipeline.py）",
    },
    "resolve_skills": {
        "handler": stage_resolve_skills,
        "description": "机制解析 + analysis-plan/coverage/research-queue（resolve_build）",
    },
    "prepare_snapshot": {
        "handler": stage_prepare_snapshot,
        "description": "人工补值 + 静态输入快照（apply_corrections + make_snapshot/hit-snapshot）",
    },
    "compile_plan": {
        "handler": stage_compile_plan,
        "description": "统一计划编译：共享路由 + 文件闭包 + calculation-plan/诊断（legacy 路由拒绝）",
    },
    "min_validate": {
        "handler": stage_min_validate,
        "description": "结构/绑定最小校验（不做 DPS 数值断言）",
    },
    "simulate": {
        "handler": stage_simulate,
        "description": "薄封装 domain.simulate（与 le run 同一引擎路径）",
    },
    "validate_result": {
        "handler": stage_validate_result,
        "description": "薄封装 domain.validate_result（共享 expected 比对）",
    },
    "render_report": {
        "handler": stage_render_report,
        "description": "确认离线 HTML 报告存在（渲染由 simulate/run 完成）",
    },
    "commit": {
        "handler": stage_commit,
        "description": "P4：验证门禁 + 幂等提交/原子发布（不缓存）",
        "cacheable": False,
    },
}

UNIMPLEMENTED_STAGES = frozenset()


def stage_info(stage_id: str) -> dict[str, Any]:
    if stage_id not in STAGES:
        raise KeyError(stage_id)
    return {"stage_id": stage_id, **STAGES[stage_id], "implemented": stage_id not in UNIMPLEMENTED_STAGES}


def _as_request(request: Any) -> contract.StageRequest:
    if isinstance(request, contract.StageRequest):
        return request
    if isinstance(request, Mapping):
        return contract.StageRequest.from_dict(dict(request))
    raise contract.ContractError("StageRequest must be a mapping or StageRequest")


def _as_context(context: Any) -> contract.StageContext:
    if isinstance(context, contract.StageContext):
        return context
    if isinstance(context, Mapping):
        return contract.StageContext(
            repo_root=Path(context.get("repo_root") or REPO_ROOT),
            output_root=Path(context.get("output_root") or "."),
            adapters=dict(context.get("adapters") or {}),
        )
    raise contract.ContractError("StageContext must be a mapping or StageContext")


def stage_fingerprints(request: Any, context: Any) -> dict[str, Any]:
    """Compute (input/implementation/parameters/dependencies/cache_key) for a request."""
    stage_request = _as_request(request)
    stage_context = _as_context(context)
    handler = (STAGES.get(stage_request.stage_id) or {}).get("handler")
    return stage_cache.request_fingerprints(
        stage_request, _root(stage_context), handler=handler
    )


def _cache_info(
    status: str,
    *,
    key: Optional[str] = None,
    reason: str = "",
    fingerprints: Optional[Mapping[str, Any]] = None,
    written: Optional[bool] = None,
) -> dict[str, Any]:
    info = contract.make_cache_info(status, key=key, reason=reason)
    if fingerprints:
        info["input_fingerprint"] = fingerprints.get("input_fingerprint")
        info["implementation_fingerprint"] = fingerprints.get("implementation_fingerprint")
        info["parameters_fingerprint"] = fingerprints.get("parameters_fingerprint")
        info["dependencies"] = dict(fingerprints.get("dependencies") or {})
    if written is not None:
        info["written"] = bool(written)
    return info


def execute_stage(request: Any, context: Any) -> contract.StageResult:
    """Run exactly one stage and return a :class:`StageResult` (cache-aware in P4)."""

    stage_request = _as_request(request)
    stage_context = _as_context(context)
    started = time.perf_counter()
    stage_id = stage_request.stage_id
    if stage_id not in STAGES:
        return contract.StageResult(
            stage_id=stage_id,
            execution_status=contract.EXECUTION_FAILED,
            readiness=contract.NEEDS_INPUT,
            issues=[
                contract.make_issue(
                    "unknown_stage_id",
                    f"未知 stage_id：{stage_id!r}（可用：{', '.join(STAGE_IDS)}）",
                    related_ids=[stage_id],
                )
            ],
            metrics={"elapsed_ms": 0.0},
            cache={"status": contract.CACHE_DISABLED, "reason": "stage_not_registered"},
        )
    handler = STAGES[stage_id]["handler"]
    repo_root = _root(stage_context)
    output_root = _run_dir(stage_context)
    cache = stage_context.adapters.get("stage_cache")
    if not isinstance(cache, stage_cache.StageCache):
        cache = None
    cacheable = bool(STAGES[stage_id].get("cacheable", True))
    fingerprints: Optional[dict[str, Any]] = None
    fingerprint_error: Optional[str] = None
    if cache is not None:
        try:
            fingerprints = stage_cache.request_fingerprints(
                stage_request, repo_root, handler=handler
            )
        except Exception as exc:  # noqa: BLE001 - fingerprinting is advisory, not fatal
            fingerprints = None
            fingerprint_error = f"{type(exc).__name__}: {exc}"

    cache_info: dict[str, Any]
    if cache is not None and fingerprints is not None and cacheable:
        cache_key = str(fingerprints["cache_key"])
        entry = cache.load(stage_id, cache_key)
        if entry is not None:
            try:
                output_refs = cache.materialize(
                    entry,
                    output_root,
                    artifact_ref=lambda path, artifact_id=None: contract.make_artifact_ref(
                        path, root=repo_root, artifact_id=artifact_id or f"{stage_id}:{Path(path).name}"
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - corrupt payload => miss + rerun
                cache.last_error = f"cache_materialize_failed: {exc}"
                entry = None
            else:
                elapsed = round((time.perf_counter() - started) * 1000.0, 3)
                metrics = {
                    "elapsed_ms": elapsed,
                    "input_fingerprint": fingerprints["input_fingerprint"],
                    "implementation_fingerprint": fingerprints["implementation_fingerprint"],
                    "parameters_fingerprint": fingerprints["parameters_fingerprint"],
                    "cache_key": cache_key,
                    "cache_status": contract.CACHE_HIT,
                    "cache_hit": True,
                    "output_count": len(output_refs),
                    "handler_executed": False,
                }
                if fingerprint_error:
                    metrics["fingerprint_warning"] = fingerprint_error
                return contract.StageResult(
                    stage_id=stage_id,
                    execution_status=contract.EXECUTION_COMPLETED,
                    readiness=entry.get("readiness"),
                    output_refs=output_refs,
                    issues=[dict(issue) for issue in entry.get("issues") or []],
                    metrics=metrics,
                    cache=_cache_info(
                        contract.CACHE_HIT,
                        key=cache_key,
                        reason="validated_cached_outputs",
                        fingerprints=fingerprints,
                    ),
                )
    if cache is not None and fingerprints is not None:
        cache_key = str(fingerprints["cache_key"])
        if not cacheable:
            cache_info = _cache_info(
                contract.CACHE_DISABLED,
                key=cache_key,
                reason="stage_not_cacheable",
                fingerprints=fingerprints,
            )
        elif cache.enabled:
            cache_info = _cache_info(
                contract.CACHE_MISS, key=cache_key, reason="cache_miss", fingerprints=fingerprints
            )
        else:
            cache_info = _cache_info(
                contract.CACHE_DISABLED,
                key=cache_key,
                reason="no_cache_requested",
                fingerprints=fingerprints,
            )
    else:
        cache_info = {
            "status": contract.CACHE_DISABLED,
            "reason": "stage_cache_adapter_missing" if stage_context.adapters else "p3_stage_cache_disabled",
        }

    output_snapshot_before = (
        stage_cache.snapshot_tree(output_root)
        if cache is not None and fingerprints is not None and cacheable
        else None
    )
    try:
        outcome = handler(stage_request, stage_context) or {}
    except StageError as exc:
        return _failed_result(
            stage_id, exc.code, exc.message, started, exc.required_input,
            fingerprints=fingerprints, cache_info=cache_info,
        )
    except contract.ContractError as exc:
        return _failed_result(
            stage_id, "contract_error", str(exc), started,
            fingerprints=fingerprints, cache_info=cache_info,
        )
    except Exception as exc:  # noqa: BLE001 - structured failure, never print/exit
        return _failed_result(
            stage_id, "stage_exception", f"{type(exc).__name__}: {exc}", started,
            fingerprints=fingerprints, cache_info=cache_info,
        )
    produced_paths: list[Path] = []
    if output_snapshot_before is not None:
        output_snapshot_after = stage_cache.snapshot_tree(output_root)
        for relative, meta in sorted(output_snapshot_after.items()):
            if output_snapshot_before.get(relative) != meta:
                produced_paths.append(output_root / relative)
        # declared outputs outside the run dir are still reported (not cacheable)
        for output in outcome.get("outputs") or []:
            path = Path(str(output))
            if not path.is_absolute():
                path = repo_root / path
            if not path.is_file():
                continue
            try:
                path.resolve().relative_to(output_root.resolve())
            except ValueError:
                produced_paths.append(path)
    else:
        for output in outcome.get("outputs") or []:
            path = Path(str(output))
            if not path.is_absolute():
                path = repo_root / path
            if path.is_file():
                produced_paths.append(path)
    output_refs = [
        contract.make_artifact_ref(path, root=repo_root,
                                   artifact_id=f"{stage_id}:{path.name}")
        for path in produced_paths
        if path.is_file()
    ]
    metrics = dict(outcome.get("metrics") or {})
    metrics["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    if outcome.get("input_fingerprint"):
        metrics["handler_input_fingerprint"] = outcome["input_fingerprint"]
    if fingerprints is not None:
        metrics["input_fingerprint"] = fingerprints["input_fingerprint"]
        metrics["implementation_fingerprint"] = fingerprints["implementation_fingerprint"]
        metrics["parameters_fingerprint"] = fingerprints["parameters_fingerprint"]
        metrics["cache_key"] = fingerprints["cache_key"]
        metrics["cache_status"] = cache_info.get("status")
        metrics["cache_hit"] = False
        metrics["handler_executed"] = True
    if fingerprint_error:
        metrics["fingerprint_warning"] = fingerprint_error
    metrics["output_count"] = len(output_refs)
    execution_status = outcome.get("execution_status") or contract.EXECUTION_COMPLETED
    readiness = outcome.get("readiness")
    issues = [dict(issue) for issue in outcome.get("issues") or []]

    if (cache is not None and fingerprints is not None and cacheable
            and execution_status == contract.EXECUTION_COMPLETED):
        stored = cache.store(
            stage_id=stage_id,
            cache_key=str(fingerprints["cache_key"]),
            fingerprints=fingerprints,
            outputs=produced_paths,
            output_root=output_root,
            artifact_ref=lambda path, artifact_id=None: contract.make_artifact_ref(
                path, root=repo_root, artifact_id=artifact_id or f"{stage_id}:{Path(path).name}"
            ),
            readiness=readiness,
            issues=issues,
            metrics={"elapsed_ms": metrics["elapsed_ms"]},
        )
        cache_info["written"] = bool(stored.get("ok"))
        if not stored.get("ok"):
            metrics["cache_write_warning"] = str(stored.get("reason") or "cache_store_failed")
    return contract.StageResult(
        stage_id=stage_id,
        execution_status=execution_status,
        readiness=readiness,
        output_refs=output_refs,
        issues=issues,
        metrics=metrics,
        cache=cache_info,
    )


def _failed_result(stage_id: str, code: str, message: str, started: float,
                   required_input: Optional[dict] = None, *,
                   fingerprints: Optional[Mapping[str, Any]] = None,
                   cache_info: Optional[Mapping[str, Any]] = None) -> contract.StageResult:
    metrics: dict[str, Any] = {"elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3)}
    if fingerprints is not None:
        metrics["input_fingerprint"] = fingerprints.get("input_fingerprint")
        metrics["implementation_fingerprint"] = fingerprints.get("implementation_fingerprint")
        metrics["parameters_fingerprint"] = fingerprints.get("parameters_fingerprint")
        metrics["cache_key"] = fingerprints.get("cache_key")
    if cache_info is not None:
        metrics["cache_status"] = cache_info.get("status")
    return contract.StageResult(
        stage_id=stage_id,
        execution_status=contract.EXECUTION_FAILED,
        readiness=contract.NEEDS_INPUT if required_input else contract.PARTIAL,
        issues=[contract.make_issue(code, message, required_input=required_input)],
        metrics=metrics,
        cache=dict(cache_info) if cache_info is not None else {
            "status": contract.CACHE_DISABLED, "reason": "stage_failed"
        },
    )


__all__ = [
    "MODULE_PATHS",
    "REPO_ROOT",
    "STAGES",
    "STAGE_IDS",
    "StageError",
    "UNIMPLEMENTED_STAGES",
    "execute_stage",
    "stable_result_digest",
    "stage_fingerprints",
    "stage_info",
]
