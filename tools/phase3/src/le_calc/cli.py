"""Command line entry point for the phase-3 calculators.

Black Hole stage-1 calculator (``result-v1``)::

    python -m le_calc run --out reports/<case>/<run-id>
    python -m le_calc run --snapshot <input-snapshot.json> --out <dir>
    python -m le_calc check --snapshot <input-snapshot.json>

`--snapshot` consumes a pre-dumped input snapshot (same shape as
`input-snapshot.json`); without it the CLI loads the live build through the
input adapter (`le_calc.inputs` + `le_calc.stats`).

Flame Reave ``hit`` model extension (same pipeline; ``le-hit-result-v1``,
see DESIGN §5.4).  These are internal steps of ``le.ps1 run --case <hit case>``
and can be invoked directly for debugging::

    python -m le_calc hit-snapshot --case examples/<case>/case.json \
        --calc-input <calculation-input.json> --out <snapshot.json>
    python -m le_calc hit-run --case examples/<case>/case.json \
        --snapshot <snapshot.json> --ruleset <ruleset.json> --out <run-dir> \
        --run-id <id> [--scenario <scenario.json>]

The hit subcommands lazily import the fr-engine modules
(``hit_inputs/hit_ruleset/hit_damage/hit_simulate/hit_buffs``); they print an
actionable message and exit with code 2 when those modules are not available.
"""

from __future__ import annotations

import argparse
import copy
import datetime as _dt
import importlib
import inspect
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, Sequence

from . import ENGINE_ID, __version__, model
from .report import load_and_build, write_run
from .ruleset import load_ruleset


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _default_run_id() -> str:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-bh-stage1-v1"


def _load_snapshot(path: str) -> model.Snapshot:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return model.Snapshot.from_dict(data)


def _load_live_snapshot() -> model.Snapshot:
    try:
        from .inputs import load_bundle  # type: ignore
        from .stats import build_snapshot  # type: ignore
    except Exception as exc:  # pragma: no cover - adapter may be incomplete
        raise SystemExit(
            "input adapter not available (le_calc.inputs/le_calc.stats): "
            f"{exc}; use --snapshot <input-snapshot.json> instead"
        ) from exc
    bundle = load_bundle()
    return build_snapshot(bundle)


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    single = result["single_cast"]["totals"]
    out: dict[str, Any] = {
        "run_id": result["run"]["run_id"],
        "status": result["run"]["status"],
        "ruleset": result["run"]["ruleset"]["id"],
        "single_cast": {
            "total": single["single_cast_total"],
            "body": single["body_total"],
            "ignite": single["ignite_total"],
            "ignite_tail": single["ignite_tail_damage"],
            "observation_end_s": single["observation_end_s"],
            "expected_stacks": single["expected_total_stacks_applied"],
        },
        "loops": [
            {
                "policy_id": loop["policy_id"],
                "cast_count": loop["totals"]["cast_count"],
                "cycle_s": loop["totals"]["cycle_s"],
                "windows": [
                    {"window_s": w["window_s"], "dps": w["dps"], "total": w["total_damage"]}
                    for w in loop["windows"]
                ],
            }
            for loop in result["loops"]
        ],
        "consistency": result.get("consistency", {}),
        "issues": [
            {"id": i.get("issue_id"), "severity": i.get("severity"), "title": i.get("title")}
            for i in result.get("issues", [])
        ],
    }
    conditional = result.get("conditional") or {}
    if conditional.get("status"):
        evaluation = conditional.get("evaluation") or {}
        out["conditional"] = {
            "status": conditional.get("status"),
            "registry": (conditional.get("registry") or {}).get("id"),
            "summary": evaluation.get("summary") or {},
            "validation": evaluation.get("validation") or {},
            "changed_from_baseline": bool(evaluation.get("changed_from_baseline")),
            "dps_recomputed": bool(evaluation.get("dps_recomputed")),
            "recompute": {
                "status": ((conditional.get("recompute") or {}).get("status")),
                "applied": len(
                    ((((conditional.get("recompute") or {}).get("condition_application") or {}).get("applied")) or [])
                ),
                "baseline_single": (
                    (((conditional.get("recompute") or {}).get("baseline") or {}).get("single") or {}).get(
                        "single_cast_total"
                    )
                ),
                "scenario_single": (
                    (((conditional.get("recompute") or {}).get("scenario") or {}).get("single") or {}).get(
                        "single_cast_total"
                    )
                ),
            }
            if conditional.get("recompute")
            else {},
            "issues": [
                {"code": i.get("code"), "severity": i.get("severity"), "message": i.get("message")}
                for i in conditional.get("issues") or []
            ],
        }
    return out


def cmd_run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    run_id = args.run_id or out.name
    snapshot = _load_snapshot(args.snapshot) if args.snapshot else _load_live_snapshot()
    generated_at = args.generated_at or _utc_now()
    try:
        out_display = os.path.relpath(str(out.resolve()), str(Path.cwd().resolve()))
    except ValueError:  # pragma: no cover - different drive
        out_display = str(out)
    repro_parts = [
        "$env:PYTHONPATH='tools/phase3/src';",
        '& ".venv\\Scripts\\python.exe" -m le_calc run',
    ]
    if args.snapshot:
        repro_parts.append(f"--snapshot {args.snapshot}")
    ruleset = args.ruleset
    if not ruleset:
        from .ruleset import DEFAULT_RULESET_PATH

        ruleset = str(DEFAULT_RULESET_PATH)
    repro_parts.append(f"--ruleset {ruleset}")
    if args.monster_level is not None:
        repro_parts.append(f"--monster-level {args.monster_level}")
    if args.conditional_registry:
        repro_parts.append(f"--conditional-registry {args.conditional_registry}")
    if args.calc_input:
        repro_parts.append(f"--calc-input {args.calc_input}")
    if args.build:
        repro_parts.append(f"--build {args.build}")
    if args.scenario:
        repro_parts.append(f"--scenario {args.scenario}")
    if args.conditional_preset:
        repro_parts.append(f"--conditional-preset {args.conditional_preset}")
    if args.no_conditional:
        repro_parts.append("--no-conditional")
    if args.no_recompute:
        repro_parts.append("--no-recompute")
    repro_parts.append(f'--out "{out_display}"')
    repro_parts.append(f"--run-id {run_id}")
    repro = " ".join(repro_parts)
    result = load_and_build(
        snapshot,
        run_id=run_id,
        generated_at=generated_at,
        ruleset_path=args.ruleset,
        out_dir=out_display,
        repro_command=repro,
        monster_level=args.monster_level,
        conditional_registry=args.conditional_registry,
        calc_input_path=args.calc_input,
        build_path=args.build,
        conditional_scenario=args.scenario,
        conditional_preset=args.conditional_preset,
        enable_conditional=not args.no_conditional,
        enable_recompute=not args.no_recompute,
    )
    written = write_run(result, out, render_html=not args.no_html)
    summary = _summary(result)
    if args.json:
        print(json.dumps({"summary": summary, "written": written}, ensure_ascii=False, indent=2))
    else:
        print(f"run: {summary['run_id']}  status={summary['status']}  ruleset={summary['ruleset']}")
        s = summary["single_cast"]
        print(
            "single cast: total={:.6g}  body={:.6g}  ignite={:.6g}  tail={:.6g}  stacks={:.6g}  end={:.3f}s".format(
                s["total"], s["body"], s["ignite"], s["ignite_tail"], s["expected_stacks"], s["observation_end_s"]
            )
        )
        for loop in summary["loops"]:
            windows = "  ".join(f"{w['window_s']:g}s={w['dps']:.6g} dps" for w in loop["windows"])
            print(f"loop[{loop['policy_id']}]: casts={loop['cast_count']} cycle={loop['cycle_s']:.4g}s  {windows}")
        print(f"consistency: {summary['consistency'].get('all_pass')}")
        for issue in summary["issues"]:
            print(f"  [{issue['severity']}] {issue['title']}")
        conditional = summary.get("conditional")
        if conditional:
            counts = conditional.get("summary") or {}
            print(
                "conditional: status={} registry={} candidates={} relevant={} configured={} dps_recomputed={}".format(
                    conditional.get("status"),
                    conditional.get("registry"),
                    counts.get("candidates"),
                    counts.get("relevant"),
                    counts.get("configured"),
                    conditional.get("dps_recomputed"),
                )
            )
            validation = conditional.get("validation") or {}
            for item in validation.get("errors") or []:
                print(f"  [conditional-error] {item.get('message')}")
            for item in validation.get("warnings") or []:
                print(f"  [conditional-warning] {item.get('message')}")
            for item in conditional.get("issues") or []:
                print(f"  [conditional-{item.get('severity', 'info')}] {item.get('message')}")
            recompute = conditional.get("recompute") or {}
            if recompute:
                print(
                    "  recompute: status={} applied={} single={} -> {}".format(
                        recompute.get("status"),
                        recompute.get("applied"),
                        recompute.get("baseline_single"),
                        recompute.get("scenario_single"),
                    )
                )
        print("written:")
        for name, path in written.items():
            print(f"  {name}: {path}")
    if not summary["consistency"].get("all_pass", True):
        return 2
    conditional = summary.get("conditional") or {}
    if (conditional.get("validation") or {}).get("errors"):
        return 2
    return 0


# ---------------------------------------------------------------------------
# Flame Reave hit model (le-hit-result-v1) — separate from the BH commands
# ---------------------------------------------------------------------------

_HIT_MODULE_NAMES = (
    "hit_inputs",
    "hit_ruleset",
    "hit_damage",
    "hit_simulate",
    "hit_buffs",
    "hit_report",
)


def _hit_modules(names: Optional[Sequence[str]] = None) -> Optional[SimpleNamespace]:
    """Import the requested Flame Reave hit modules lazily.

    Returns ``None`` (after an actionable stderr message) when the fr-engine
    modules are not available yet, so the BH command surface never breaks.
    """

    requested = list(names or _HIT_MODULE_NAMES)
    modules: dict[str, Any] = {}
    for name in requested:
        try:
            modules[name] = importlib.import_module(f".{name}", __package__)
        except Exception as exc:  # pragma: no cover - depends on engine landing
            print(
                f"hit 模块不可用（le_calc.{name}）：{exc}（需要 fr-engine 的 hit 模块）",
                file=sys.stderr,
            )
            return None
    return SimpleNamespace(**modules)


def _read_document(path: str | os.PathLike[str], what: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"{what} 不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{what} 不是合法 JSON（{path}）：{exc}") from exc


def _supported_kwargs(func: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop keyword arguments a (possibly older) engine function does not accept."""
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins/adapters
        return dict(kwargs)
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return dict(kwargs)
    return {key: value for key, value in kwargs.items() if key in parameters}


def _call_hit_engine(func: Any, snapshot: Any, ruleset: Any, scenario: Optional[dict[str, Any]]) -> Any:
    """Call ``compute_single_hit``/``simulate``/``screen_buffs`` with an optional scenario."""
    return func(snapshot, ruleset, **_supported_kwargs(func, {"scenario": scenario}))


def _relative_display(path: Path) -> str:
    try:
        return os.path.relpath(str(path.resolve()), str(Path.cwd().resolve()))
    except ValueError:  # pragma: no cover - different drive
        return str(path)


def cmd_hit_snapshot(args: argparse.Namespace) -> int:
    """Build the Flame Reave hit input snapshot (``input-snapshot.hit.json``)."""

    engine = _hit_modules(("hit_inputs",))
    if engine is None:
        return 2
    build_snapshot = engine.hit_inputs.build_snapshot
    try:
        snapshot = build_snapshot(
            args.case,
            **_supported_kwargs(
                build_snapshot,
                {
                    "calc_input_path": args.calc_input,
                    "dataset_path": args.dataset,
                    "repo_root": args.repo_root,
                },
            ),
        )
    except SystemExit:
        raise
    except Exception as exc:
        print(f"hit-snapshot 失败：{exc}", file=sys.stderr)
        return 1
    try:
        from .hit_report import snapshot_to_document
    except Exception as exc:  # pragma: no cover - hit_report ships with this change
        print(f"hit_report 不可用：{exc}", file=sys.stderr)
        return 2
    document = snapshot_to_document(snapshot)
    if not isinstance(document, dict) or not document:
        print("hit-snapshot 失败：hit_inputs.build_snapshot 未返回快照数据", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    derived = document.get("derived") or {}
    build_meta = document.get("build_meta") or {}
    build = build_meta.get("build") or {}
    calc_input = build_meta.get("calc_input") or {}
    skill = derived.get("skill") or {}
    contributions = document.get("contributions") or []
    print(f"hit snapshot: {_relative_display(out)}")
    print(
        "  build={} revision={} calc_input={} mods={}".format(
            build.get("build_id"),
            build.get("revision_id"),
            str(calc_input.get("sha256"))[:16] + "…" if calc_input.get("sha256") else None,
            calc_input.get("mod_count"),
        )
    )
    print(
        "  skill={} contributions={} model={}".format(
            skill.get("ability_id"),
            len(contributions),
            derived.get("model"),
        )
    )
    statuses: dict[str, int] = {}
    for contribution in contributions:
        status = str((contribution or {}).get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
    print(f"  contribution status: {dict(sorted(statuses.items()))}")
    return 0


def _hit_repro_command(args: argparse.Namespace, out_display: str, run_id: str) -> str:
    parts = [
        "$env:PYTHONPATH='tools/phase3/src';",
        '& ".venv\\Scripts\\python.exe" -m le_calc hit-run',
        f"--case {args.case}",
        f"--snapshot {args.snapshot}",
        f"--ruleset {args.ruleset}",
    ]
    if args.scenario:
        parts.append(f"--scenario {args.scenario}")
    parts.append(f'--out "{out_display}"')
    parts.append(f"--run-id {run_id}")
    return " ".join(parts)


#: one-factor conditional-buff variants for the hit report sensitivity table
_HIT_SENSITIVITY_VARIANTS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("ignited", "目标 ignited（Scorching Tide +60% more / Chaos Flare crit ×1.2）", {"ignited": True}),
    ("frenzy_active", "Frenzy active（+45.6% increased attack speed / +8 added melee fire）", {"frenzy_active": True}),
    ("ambition_20", "Ambition 20 层（+40% more Fire）", {"ambition_stacks": 20}),
    (
        "blade_weaving_consume_8",
        "Blade Weaving 消耗 8 层（+120% more Elemental）",
        {"consume_blade_weaving": True, "blade_weaving_stacks": 8},
    ),
    (
        "rhythm_consume",
        "Rhythm of Fire 消耗单次（+130% more，单次命中口径）",
        {"consume_rhythm_of_fire": True},
    ),
)


def _hit_scenario_with_states(
    scenario: Optional[dict[str, Any]],
    extra_states: dict[str, Any],
) -> dict[str, Any]:
    """Scenario copy with extra states applied to both ``single_hit`` and ``dps``."""
    base = copy.deepcopy(scenario) if scenario else {}
    for section in ("single_hit", "dps"):
        block = base.setdefault(section, {})
        states = dict(block.get("states") or {})
        states.update(extra_states)
        block["states"] = states
    return base


def cmd_hit_run(args: argparse.Namespace) -> int:
    """Run the Flame Reave hit model and write the complete run directory."""

    engine = _hit_modules(("hit_ruleset", "hit_damage", "hit_simulate", "hit_buffs", "hit_report"))
    if engine is None:
        return 2
    try:
        case = _read_document(args.case, "案例配置")
        snapshot_raw = _read_document(args.snapshot, "hit 快照")
        scenario = _read_document(args.scenario, "场景配置") if args.scenario else None
        ruleset = engine.hit_ruleset.load_ruleset(args.ruleset)
        snapshot = model.Snapshot.from_dict(snapshot_raw)
        single_hit = _call_hit_engine(engine.hit_damage.compute_single_hit, snapshot, ruleset, scenario)
        simulation = _call_hit_engine(engine.hit_simulate.simulate, snapshot, ruleset, scenario)
        screening = _call_hit_engine(engine.hit_buffs.screen_buffs, snapshot, ruleset, scenario)
        # Baseline profiles + conditional-buff sensitivity are computed with the
        # same engine functions and stored in the run result so the offline report
        # can show BH-style dual baselines / per-buff deltas without recomputing.
        baselines: dict[str, Any] = {}
        sensitivity: list[dict[str, Any]] = []
        try:
            profiles = (scenario or {}).get("profiles") or {}
            for profile_id in profiles:
                profile_scenario = copy.deepcopy(scenario)
                profile_scenario["baseline_profile"] = profile_id
                baselines[profile_id] = {
                    "single_hit": _call_hit_engine(
                        engine.hit_damage.compute_single_hit, snapshot, ruleset, profile_scenario
                    ),
                    "simulate": _call_hit_engine(
                        engine.hit_simulate.simulate, snapshot, ruleset, profile_scenario
                    ),
                }
            for variant_id, label, states in _HIT_SENSITIVITY_VARIANTS:
                variant_scenario = _hit_scenario_with_states(scenario, states)
                sensitivity.append(
                    {
                        "id": variant_id,
                        "label": label,
                        "states": dict(states),
                        "single_hit": _call_hit_engine(
                            engine.hit_damage.compute_single_hit, snapshot, ruleset, variant_scenario
                        ),
                        "simulate": _call_hit_engine(
                            engine.hit_simulate.simulate, snapshot, ruleset, variant_scenario
                        ),
                    }
                )
        except Exception as exc:  # pragma: no cover - defensive: main run stays usable
            print(f"hit-run 附加基线/敏感性计算失败（主结果保留）：{exc}", file=sys.stderr)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"hit-run 计算失败：{exc}", file=sys.stderr)
        return 1

    out = Path(args.out)
    run_id = args.run_id or out.name
    generated_at = args.generated_at or _utc_now()
    out_display = _relative_display(out)
    result = engine.hit_report.build_hit_result(
        snapshot,
        ruleset,
        case,
        run_id=run_id,
        generated_at=generated_at,
        ruleset_path=args.ruleset,
        out_dir=out_display,
        scenario=scenario,
        scenario_path=args.scenario,
        engine_outputs={
            "single_hit": single_hit,
            "simulate": simulation,
            "buff_screening": screening,
            "baselines": baselines,
            "buff_sensitivity": sensitivity,
        },
        repro_command=_hit_repro_command(args, out_display, run_id),
    )
    written = engine.hit_report.write_hit_run(
        result,
        out,
        snapshot_document=snapshot_raw,
        ruleset_document=engine.hit_report.ruleset_to_document(ruleset),
        screening_document=screening,
        render_html=not args.no_html,
    )
    summary = engine.hit_report.result_summary(result)
    if args.json:
        print(json.dumps({"summary": summary, "written": written}, ensure_ascii=False, indent=2))
    else:
        print(f"hit run: {summary['run_id']}  status={summary['status']}  ruleset={summary['ruleset']}")
        print(
            "single hit: damage={:.6g}  avg_crit=×{:.6g}  hits/cast={}  aps={:.6g}".format(
                summary["single_hit"],
                summary["avg_crit"],
                summary["hits_per_cast"],
                summary["aps"],
            )
        )
        windows = "  ".join(
            f"{window['window_s']:g}s={window['dps']:.6g} dps" for window in summary["windows"]
        )
        print(f"dps windows: {windows or 'n/a'}")
        counts = summary["buff_screening"] or {}
        print(
            "buff screening: total={} modeled={} unresolved={}".format(
                counts.get("total"), counts.get("modeled"), counts.get("unresolved")
            )
        )
        print(f"consistency: {summary['consistency'].get('all_pass')}")
        for issue in summary["issues"]:
            print(f"  [{issue['severity']}] {issue['title']}")
        print("written:")
        for name, path in written.items():
            print(f"  {name}: {path}")
    if not summary["consistency"].get("all_pass", True):
        return 2
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the optional loopback one-click recompute service for the report."""

    from .serve import serve

    return int(serve(args))


def cmd_hit_serve(args: argparse.Namespace) -> int:
    """Start the FR loopback recompute service (same Python hit engine as CLI)."""

    from .hit_serve import serve as serve_hit

    return int(serve_hit(args))


def cmd_check(args: argparse.Namespace) -> int:
    ruleset = load_ruleset(args.ruleset)
    if args.snapshot:
        snapshot = _load_snapshot(args.snapshot)
    else:
        snapshot = _load_live_snapshot()
    result = load_and_build(
        snapshot,
        run_id="check",
        generated_at=_utc_now(),
        ruleset_path=args.ruleset,
    )
    print(json.dumps(_summary(result), ensure_ascii=False, indent=2))
    return 0 if result.get("consistency", {}).get("all_pass") else 2


def cmd_conditions(args: argparse.Namespace) -> int:
    """Evaluate a conditional-Buff scenario and export the handoff artefacts."""

    try:
        from .conditional import build_context
        from .report import write_conditional_artifacts
    except Exception as exc:  # pragma: no cover - package should exist
        print(f"conditional module unavailable: {exc}", file=sys.stderr)
        return 2
    snapshot = _load_snapshot(args.snapshot)
    ruleset = load_ruleset(args.ruleset) if args.ruleset else load_ruleset()
    context = build_context(
        snapshot,
        scenario=None,
        ruleset=ruleset,
        ruleset_path=args.ruleset or ruleset.path,
        calc_input_path=args.calc_input,
        build_path=args.build,
        registry_path=args.registry,
        scenario_path=args.scenario,
        preset_id=args.preset,
        repo_root=str(Path.cwd()),
    )
    out = Path(args.out)
    recompute = {}
    if getattr(args, "recompute", False):
        try:
            from .report import attach_recompute
        except Exception as exc:  # pragma: no cover - defensive
            print(f"conditional recompute unavailable: {exc}", file=sys.stderr)
            return 2
        attach_recompute(
            context,
            snapshot,
            ruleset,
            monster_level=getattr(args, "monster_level", None),
        )
        recompute = context.get("recompute") or {}
    written = write_conditional_artifacts(context, out)
    evaluation = context.get("evaluation") or {}
    validation = evaluation.get("validation") or {}
    summary = {
        "status": context.get("status"),
        "registry": context.get("registry") or {},
        "scenario": context.get("scenario") or {},
        "evaluation_summary": evaluation.get("summary") or {},
        "validation": validation,
        "recompute": {
            "status": recompute.get("status"),
            "applied": len(((recompute.get("condition_application") or {}).get("applied")) or []),
            "baseline_single": (((recompute.get("baseline") or {}).get("single") or {}).get("single_cast_total")),
            "scenario_single": (((recompute.get("scenario") or {}).get("single") or {}).get("single_cast_total")),
        }
        if recompute
        else {},
        "issues": context.get("issues") or [],
        "written": written,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"conditional: status={context.get('status')} registry={(context.get('registry') or {}).get('id')}")
        counts = evaluation.get("summary") or {}
        print(
            "  candidates={} relevant={} configured={} condition_met={} auto_active={}".format(
                counts.get("candidates"),
                counts.get("relevant"),
                counts.get("configured"),
                counts.get("condition_met"),
                counts.get("auto_active"),
            )
        )
        for item in validation.get("errors") or []:
            print(f"  [error] {item.get('message')}")
        for item in validation.get("warnings") or []:
            print(f"  [warning] {item.get('message')}")
        for item in context.get("issues") or []:
            print(f"  [{item.get('severity', 'info')}] {item.get('message')}")
        if recompute:
            print(
                "  recompute: status={} applied={} single={} -> {}".format(
                    recompute.get("status"),
                    summary["recompute"]["applied"],
                    summary["recompute"]["baseline_single"],
                    summary["recompute"]["scenario_single"],
                )
            )
        print("written:")
        for name, path in written.items():
            print(f"  {name}: {path}")
    if context.get("status") != "ready" or (validation.get("errors") or []):
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="le_calc",
        description="Phase-3 Black Hole stage-1 DPS calculator (v1)",
    )
    parser.add_argument("--version", action="version", version=f"{ENGINE_ID} {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="compute a run and write the report artefacts")
    run.add_argument("--out", required=True, help="output directory for the run")
    run.add_argument("--run-id", default=None, help="run id (default: timestamped)")
    run.add_argument("--snapshot", default=None, help="pre-dumped input snapshot JSON (bypasses live adapter)")
    run.add_argument("--ruleset", default=None, help="ruleset JSON path")
    run.add_argument(
        "--monster-level",
        type=int,
        default=None,
        help="monster level 1..100 for enemy damage reduction; 0/omit = no monster DR",
    )
    run.add_argument("--generated-at", default=None, help="ISO-8601 timestamp for deterministic runs")
    run.add_argument("--no-html", action="store_true", help="skip HTML rendering")
    run.add_argument("--json", action="store_true", help="print machine readable summary")
    run.add_argument(
        "--conditional-registry",
        default=None,
        help="conditional-effects registry JSON (default: tools/phase3/rules/conditional-effects-bh-stage1-v1.json)",
    )
    run.add_argument(
        "--calc-input",
        default=None,
        help="calculation-input JSON for conditional discovery (default: path recorded in the snapshot build_meta)",
    )
    run.add_argument("--build", default=None, help="raw bd.json for source cross-checking (optional)")
    run.add_argument("--scenario", default=None, help="conditional scenario JSON to evaluate for this run")
    run.add_argument("--conditional-preset", default=None, help="conditional scenario preset id from the registry")
    run.add_argument("--no-conditional", action="store_true", help="skip the conditional-Buff context/page data")
    run.add_argument(
        "--no-recompute",
        action="store_true",
        help="skip the cross-skill conditional recompute block (faster runs)",
    )
    run.set_defaults(func=cmd_run)

    conditions = sub.add_parser(
        "conditions",
        help="evaluate/export the conditional-Buff scenario without recomputing damage",
    )
    conditions.add_argument("--snapshot", required=True, help="input snapshot JSON")
    conditions.add_argument("--out", required=True, help="output directory for the conditional artefacts")
    conditions.add_argument("--ruleset", default=None, help="ruleset JSON path (scenario defaults)")
    conditions.add_argument("--scenario", default=None, help="conditional scenario JSON to evaluate")
    conditions.add_argument("--preset", default=None, help="conditional scenario preset id")
    conditions.add_argument("--registry", default=None, help="conditional-effects registry JSON")
    conditions.add_argument("--calc-input", default=None, help="calculation-input JSON override")
    conditions.add_argument("--build", default=None, help="raw bd.json for source cross-checking (optional)")
    conditions.add_argument(
        "--recompute",
        action="store_true",
        help="also run the cross-skill Buff channel and write conditional-recompute.json",
    )
    conditions.add_argument(
        "--monster-level",
        type=int,
        default=None,
        help="monster level for the recompute target side (default: ruleset default)",
    )
    conditions.add_argument("--json", action="store_true", help="print machine readable summary")
    conditions.set_defaults(func=cmd_conditions)

    check = sub.add_parser("check", help="compute without writing artefacts")
    check.add_argument("--snapshot", default=None)
    check.add_argument("--ruleset", default=None)
    check.set_defaults(func=cmd_check)

    serve = sub.add_parser(
        "serve",
        help="start a loopback-only service that renders a scenario-applied report on POST /recompute",
    )
    serve.add_argument("--snapshot", required=True, help="frozen input snapshot JSON")
    serve.add_argument("--ruleset", default=None, help="ruleset JSON path")
    serve.add_argument("--calc-input", default=None, help="calculation-input JSON")
    serve.add_argument("--build", default=None, help="raw bd.json for source cross-checking")
    serve.add_argument("--registry", default=None, help="conditional-effects registry JSON")
    serve.add_argument("--monster-level", type=int, default=None, help="monster level for target DR")
    serve.add_argument("--run-id", default="save-build-bh-affix-db", help="run id prefix for generated reports")
    serve.add_argument("--port", type=int, default=8765, help="loopback port (default 8765)")
    serve.set_defaults(func=cmd_serve)

    hit_serve = sub.add_parser(
        "hit-serve",
        help="loopback one-click recompute service for the Flame Reave player report",
    )
    hit_serve.add_argument("--case", default=None, help="hit case configuration")
    hit_serve.add_argument("--snapshot", required=True, help="hit input snapshot JSON")
    hit_serve.add_argument("--ruleset", required=True, help="hit ruleset JSON")
    hit_serve.add_argument("--scenario", default=None, help="base hit scenario JSON")
    hit_serve.add_argument("--run-id", default="serve", help="run id prefix")
    hit_serve.add_argument("--port", type=int, default=8765, help="loopback port")
    hit_serve.set_defaults(func=cmd_hit_serve)

    hit_snapshot = sub.add_parser(
        "hit-snapshot",
        help="build the Flame Reave hit input snapshot (input-snapshot.hit.json)",
    )
    hit_snapshot.add_argument("--case", required=True, help="hit case configuration (case.model == 'hit')")
    hit_snapshot.add_argument(
        "--calc-input",
        "--calculation-input",
        dest="calc_input",
        default=None,
        help="calculation-input JSON override (default: examples/<case>/inputs/calculation_input)",
    )
    hit_snapshot.add_argument("--dataset", default=None, help="dataset pointer override (default: case dataset)")
    hit_snapshot.add_argument(
        "--repo-root",
        default=None,
        help="repository root override (default: current working directory)",
    )
    hit_snapshot.add_argument("--out", required=True, help="output snapshot JSON path")
    hit_snapshot.set_defaults(func=cmd_hit_snapshot)

    hit_run = sub.add_parser(
        "hit-run",
        help="compute the Flame Reave hit model and write the offline report run directory",
    )
    hit_run.add_argument("--case", required=True, help="hit case configuration")
    hit_run.add_argument("--snapshot", required=True, help="hit input snapshot JSON")
    hit_run.add_argument("--ruleset", required=True, help="hit ruleset JSON")
    hit_run.add_argument("--out", required=True, help="output run directory")
    hit_run.add_argument("--run-id", default=None, help="run id (default: timestamped directory name)")
    hit_run.add_argument("--scenario", default=None, help="optional hit scenario JSON (states/effects/target)")
    hit_run.add_argument("--generated-at", default=None, help="ISO-8601 timestamp for deterministic runs")
    hit_run.add_argument("--no-html", action="store_true", help="skip offline HTML rendering")
    hit_run.add_argument("--json", action="store_true", help="print machine readable summary")
    hit_run.set_defaults(func=cmd_hit_run)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    try:  # keep Chinese output readable on Windows consoles
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover - best effort only
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
