"""Result assembly for the Black Hole stage-1 calculator (v1).

Produces the single `result-v1` JSON document that is the only business data
source for the offline HTML report, plus the separate ledger/manifest files.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from . import ENGINE_ID, SCHEMA_VERSION, __version__, model, monster_dr
from .cooldown import compute_cast_time, compute_cooldown
from .ruleset import Ruleset, load_ruleset
from .simulate import run_loop, run_single_cast

#: standard scope note (kept in one place so the HTML and JSON never diverge)
SCOPE_NOTE = (
    "限定来源：仅 Black Hole 本体及其自然施加的 Ignite；不是完整 BD 实战 DPS。"
)
DEFAULT_SCENARIO_NOTE = (
    "合成 Boss 靶（0 抗性），双星全覆盖与中心 1 米条件同时满足，属于理论最优场景。"
)


def _derived_rule(
    rule_id: str,
    title: str,
    statement: str,
    group: str,
    affects: list[str],
    evidence: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "title": title,
        "statement": statement,
        "group": group,
        "status": "derived",
        "evidence": evidence or [{"kind": "derivation", "detail": "本项目由已确认输入推导"}],
        "affects": affects,
    }


def build_derived_rules(
    snapshot: model.Snapshot,
    ruleset: Ruleset,
    cd: dict[str, Any],
    cast: dict[str, Any],
    duration_s: float,
    body: dict[str, Any],
    ignite: dict[str, Any],
) -> list[dict[str, Any]]:
    fire = snapshot.resistances.get("fire", {})
    cold = snapshot.resistances.get("cold", {})
    int_value = snapshot.attribute("intelligence")
    hw = ignite.get("application", {})
    rows: list[dict[str, Any]] = [
        _derived_rule(
            "rule-derived-fire-resistance",
            "未封顶火焰抗性",
            f"未封顶火抗 = {float(fire.get('total', 0.0)):.6g}%（上限 {float(fire.get('cap', 75.0)):.6g}% 不截断），"
            f"来自 {len([c for c in snapshot.of(model.STAT_RESISTANCE) if (c.target or '').lower() == 'fire' and c.operation == 'added'])} 条 resistance contribution。",
            "inputs",
            ["single_cast.ignite.application"],
            [{"kind": "derivation", "detail": "见 source_ledger 中 stat_key=resistance / target=fire 的行"}],
        ),
        _derived_rule(
            "rule-derived-overcapped-cold",
            "超上限冰冷抗性",
            f"冰冷抗性总值 {float(cold.get('total', 0.0)):.6g}%，超上限 {float(cold.get('overcapped', 0.0)):.6g}%；"
            "用于手套的 cast speed / cooldown recovery 派生词条。",
            "inputs",
            ["single_cast.body", "loops"],
        ),
        _derived_rule(
            "rule-derived-intelligence",
            "Intelligence 属性总额",
            f"Intelligence = {int_value:.6g}；Black Hole 每点 +4% increased damage（inc 加算池），合计 +{4.0 * int_value:.6g}%。",
            "body",
            ["single_cast.body"],
            [{"kind": "game_data", "detail": "bh2 attributeScaling[2]=4%"}],
        ),
        _derived_rule(
            "rule-derived-heat-wave-chance",
            "Heat Wave 每秒期望层数",
            f"每秒期望层数 = 2% × {hw.get('points')} 点 × {float(hw.get('uncapped_fire_resistance', 0.0)):.6g}% 未封顶火抗 "
            f"= {float(hw.get('expected_stacks_per_roll', 0.0)):.6g} 层/次；共 {hw.get('roll_count')} 次判定，"
            f"期望累计 {float(hw.get('expected_stacks_applied', 0.0)):.6g} 层。v1 不计入其他 ignite chance 来源。",
            "ignite",
            ["single_cast.ignite.application"],
        ),
        _derived_rule(
            "rule-derived-ignite-damage",
            "Ignite 单层与总伤害",
            f"Ignite 持续时间乘区 {float(ignite.get('duration_multiplier', 1.0)):.6g}（{float(ignite.get('effective_duration_s', 0.0)):.6g}s），"
            f"inc 合计 {float(ignite.get('increased_total', 0.0)):.6g}（含 Intelligence 属性缩放），more 乘积 {float(ignite.get('more_product', 1.0)):.6g}，"
            f"目标侧 ×{float((ignite.get('target') or {}).get('multiplier', 1.0)):.6g}；"
            f"每层总伤害 {float(ignite.get('per_stack_total', 0.0)):.6g}，全部期望层数合计 {float(ignite.get('totals', {}).get('damage', 0.0)):.6g}。",
            "ignite",
            ["single_cast.ignite"],
        ),
        _derived_rule(
            "rule-derived-ignite-penetration",
            "Ignite 目标侧穿透",
            f"Ignite 火焰穿透合计 {float((ignite.get('penetration') or {}).get('total', 0.0)):.6g}"
            f"（玩家火焰穿透 + Increased Ignite Effect 转穿透），目标有效抗性 "
            f"{float((ignite.get('target') or {}).get('effective_resistance', 0.0)):.6g}，"
            f"倍率 ×{float((ignite.get('target') or {}).get('multiplier', 1.0)):.6g}。",
            "ignite",
            ["single_cast.ignite"],
            [{"kind": "public_doc", "detail": "tunklab Penetration：作用于所有伤害（含持续伤害），可把有效抗性压到 0 以下"}, {"kind": "game_data", "detail": "Ignite additionalPenetrationDamageType=1(Fire)"}],
        ),
        _derived_rule(
            "rule-derived-duration",
            "Black Hole 实际持续时间",
            f"基础 2.75s × (1 + 加算 duration 总量) = {duration_s:.6g}s；跳数按 0.3s 周期离散、末跳吞并余量。",
            "duration",
            ["single_cast.body"],
        ),
        _derived_rule(
            "rule-derived-cast-time",
            "施法时间",
            f"参考公式：cast = 1 / (as {float(cast.get('animation_speed', 0.0)):.7g} × speedMultiplier "
            f"{float(cast.get('speed_multiplier', 1.0)):.4g} × (1 + increased cast speed {float(cast.get('increased_cast_speed', 0.0)):.6g})) "
            f"= {float(cast.get('cast_time_s', 0.0)):.6g}s（v1 assumption；旧口径 0.6/(1+cast speed) 为另一分支）。",
            "cast",
            ["loops", "single_cast.cast"],
        ),
        _derived_rule(
            "rule-derived-cooldown",
            "有效充能恢复时间",
            f"({float(cd.get('base_recharge_s', 0.0)):.6g} + {float(cd.get('flat_total_s', 0.0)):.6g}) "
            f"/ (1 + ICR {float(cd.get('icr_total', 0.0)):.6g}) = {float(cd.get('effective_recharge_s', 0.0)):.6g}s；"
            f"最大充能 {float(cd.get('max_charges', 1)):.6g}。Recurrence 的 60% 在 v1 归入 ICR 池（映射未确认）。",
            "cooldown",
            ["loops"],
        ),
    ]
    return rows


def consistency_checks(
    single: dict[str, Any], loops: list[dict[str, Any]]
) -> dict[str, Any]:
    body = single["body"]
    tick_sum = sum(
        tick["damage"] for comp in body["components"] for tick in comp["ticks"]
    )
    ignite = single["ignite"]
    cohort_sum = sum(c["damage_total"] for c in ignite.get("cohorts", []))
    totals = single["totals"]
    checks = {
        "body_ticks_sum_equals_component_total": abs(tick_sum - body["totals"]["damage"]) <= 1e-6 * max(1.0, tick_sum),
        "ignite_cohorts_sum_equals_total": abs(cohort_sum - ignite["totals"]["damage"]) <= 1e-6 * max(1.0, cohort_sum),
        "single_total_equals_body_plus_ignite": abs(
            totals["single_cast_total"] - (totals["body_total"] + totals["ignite_total"])
        ) <= 1e-6 * max(1.0, totals["single_cast_total"]),
        "window_plus_tail_equals_ignite_total": abs(
            totals["window_damage_until_black_hole_end"]
            + totals["ignite_tail_damage"]
            - totals["single_cast_total"]
        ) <= 1e-6 * max(1.0, totals["single_cast_total"]),
        "loop_windows_monotone": all(
            loops[0]["windows"][i]["total_damage"]
            <= loops[0]["windows"][i + 1]["total_damage"] + 1e-6
            for i in range(len(loops[0]["windows"]) - 1)
        )
        if loops and loops[0]["windows"]
        else True,
        "loop_policies_ordered": (
            loops[0]["totals"]["horizon_damage"]
            >= loops[1]["totals"]["horizon_damage"] - 1e-6
            if len(loops) >= 2
            else True
        ),
        "loop_concurrency_expected": (
            loops[0]["totals"].get("max_concurrent_black_holes", 0)
            > loops[1]["totals"].get("max_concurrent_black_holes", 0)
            if len(loops) >= 2
            else True
        ),
    }
    checks["all_pass"] = all(checks.values())
    return checks


def build_modifier_graph(
    snapshot: model.Snapshot,
    ruleset: Ruleset,
    single: dict[str, Any],
    cooldown: Optional[dict[str, Any]] = None,
    loops: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Display-only relationship summary, generated from the current result.

    The graph is metadata for the report (not a damage input).  Labels are
    derived from the values computed in this run so no stale rule text (such as
    an old "one judge per second" or "single black hole limit") can survive a
    ruleset change.
    """

    fire = snapshot.resistances.get("fire", {})
    ignite = single.get("ignite") or {}
    hw = ignite.get("application") or {}
    duration = ignite.get("duration") or {}
    body_duration = (single.get("body") or {}).get("duration") or {}
    cooldown = cooldown or {}
    loops = loops or []
    points = hw.get("points")
    roll_interval = hw.get("roll_interval_s")
    roll_count = hw.get("roll_count")
    per_roll = hw.get("expected_stacks_per_roll")
    per_second = hw.get("expected_stacks_per_second")
    max_concurrent = max(
        (
            int((loop.get("totals") or {}).get("max_concurrent_black_holes") or 0)
            for loop in loops
            if isinstance(loop, dict)
        ),
        default=0,
    )
    nodes = [
        {"node_id": "stat:uncapped-fire-resistance", "label": "未封顶火焰抗性", "kind": "stat", "value": float(fire.get("total", 0.0)), "unit": "percent"},
        {"node_id": "stat:intelligence", "label": "智力属性", "kind": "stat", "value": snapshot.attribute("intelligence"), "unit": "points"},
        {
            "node_id": "rule:heat-wave",
            "label": f"Heat Wave（{points:g} 点，2%/点/1% 火抗/秒）" if isinstance(points, (int, float)) else "Heat Wave",
            "kind": "rule",
            "value": points,
        },
        {"node_id": "derived:ignite-per-roll", "label": "每次判定期望新增层数", "kind": "derived", "value": float(per_roll or 0.0), "unit": "stacks/roll"},
        {"node_id": "derived:ignite-per-second", "label": "每秒期望新增层数", "kind": "derived", "value": float(per_second or 0.0), "unit": "stacks/s"},
        {"node_id": "rule:ignite-duration", "label": "点燃持续时间乘区", "kind": "rule", "value": float(duration.get("duration_multiplier", 1.0))},
        {"node_id": "rule:bh-duration", "label": "Black Hole 持续时间", "kind": "derived", "value": float(body_duration.get("duration_s", 0.0)), "unit": "s"},
        {"node_id": "rule:cooldown", "label": "有效充能恢复", "kind": "derived", "value": cooldown.get("effective_recharge_s"), "unit": "s"},
        {"node_id": "rule:black-hole-overlap", "label": "每次施放 1 个黑洞；不同施放可并存", "kind": "rule", "value": max_concurrent},
    ]
    edges = [
        {"from": "stat:uncapped-fire-resistance", "to": "derived:ignite-per-roll", "label": "2%/点/1% 火抗/秒（Heat Wave 点数）"},
        {
            "from": "rule:heat-wave",
            "to": "derived:ignite-per-roll",
            "label": f"每 {roll_interval:g} 秒判定一次，共 {int(roll_count)} 次" if isinstance(roll_interval, (int, float)) and isinstance(roll_count, (int, float)) else "按判定间隔逐次判定",
        },
        {"from": "derived:ignite-per-second", "to": "single_cast.ignite.application", "label": "层数施加（期望值模型）"},
        {"from": "rule:ignite-duration", "to": "single_cast.ignite", "label": "决定每层完整伤害与重叠层数"},
        {"from": "stat:intelligence", "to": "single_cast.body", "label": "每点 +4% 加算伤害"},
        {"from": "stat:intelligence", "to": "single_cast.ignite", "label": "属性缩放传递给点燃"},
        {"from": "rule:bh-duration", "to": "single_cast.body", "label": "决定跳数与覆盖"},
        {"from": "rule:cooldown", "to": "loops", "label": "决定施放间隔"},
        {"from": "rule:black-hole-overlap", "to": "loops", "label": "决定同时存续与重叠"},
    ]
    return {"nodes": nodes, "edges": edges}


def _dps_by_monster_level(
    single: dict[str, Any],
    loops: list[dict[str, Any]],
    selected_level: int,
) -> dict[str, Any]:
    """All damage scales linearly with (1 - monster DR); precompute per-level DPS."""

    base_factor = monster_dr.factor_for_level(selected_level)
    if base_factor <= 0:  # pragma: no cover - source table max is 0.87
        base_factor = 1.0
    totals = single.get("totals") or {}
    per_policy: dict[str, dict[str, Any]] = {}
    for loop in loops:
        policy = str(loop.get("policy_id"))
        windows = loop.get("windows") or []
        window = next((w for w in windows if abs(float(w.get("window_s", 0)) - 120.0) < 1e-9), None)
        if window is None and windows:
            window = windows[-1]
        horizon = float((loop.get("totals") or {}).get("horizon_dps", 0.0))
        per_policy[policy] = {
            "window_s": float((window or {}).get("window_s", 120.0)),
            "dps": float((window or {}).get("dps", horizon)),
            "total_damage": float((window or {}).get("total_damage", 0.0)),
            "cast_count": int((window or {}).get("cast_count", (loop.get("totals") or {}).get("cast_count", 0))),
        }
    out: dict[str, Any] = {}
    for level in [0, *range(1, 101)]:
        ratio = monster_dr.factor_for_level(level) / base_factor
        entry: dict[str, Any] = {
            "factor": monster_dr.factor_for_level(level),
            "single_cast_total": float(totals.get("single_cast_total", 0.0)) * ratio,
            "body_total": float(totals.get("body_total", 0.0)) * ratio,
            "ignite_total": float(totals.get("ignite_total", 0.0)) * ratio,
            "ignite_tail_damage": float(totals.get("ignite_tail_damage", 0.0)) * ratio,
        }
        for policy, values in per_policy.items():
            entry[f"{policy}_window_s"] = values["window_s"]
            entry[f"{policy}_dps"] = values["dps"] * ratio
            entry[f"{policy}_total_damage"] = values["total_damage"] * ratio
        out[str(level)] = entry
    return out


def build_result(
    snapshot: model.Snapshot,
    ruleset: Ruleset,
    *,
    run_id: str,
    generated_at: str,
    ruleset_path: Optional[str] = None,
    out_dir: Optional[str] = None,
    extra_fingerprints: Optional[dict[str, Any]] = None,
    repro_command: Optional[str] = None,
    monster_level: Optional[int] = None,
    scenario_override: Optional[dict[str, Any]] = None,
    precomputed_outputs: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    scenario = dict(ruleset.scenario)
    scenario["target"] = {
        "target_id": f"{ruleset.target_side.get('scenario_id', 'scenario')}-target",
        "label": ruleset.target_side.get("target_label", "synthetic target"),
        "boss_tag": bool(ruleset.target_side.get("boss_tag", True)),
        "rare_tag": bool(ruleset.target_side.get("rare_tag", True)),
        "resistances": dict(ruleset.target_side.get("base_resistances", {})),
        "negative_resistance_allowed": bool(ruleset.target_side.get("negative_resistance_allowed", True)),
        "notes": DEFAULT_SCENARIO_NOTE,
    }
    # Monster level / enemy damage reduction: separate target-side factor.
    # None means "use the ruleset default" (rulesets without monster_dr default to 0
    # = disabled, preserving legacy no-DR runs).
    if monster_level is None:
        monster_level = int((ruleset.target_side.get("monster_dr") or {}).get("default_level", 0))
    mr_info = monster_dr.describe(monster_level)
    scenario["target"].update(
        {
            "monster_level": mr_info["monster_level"],
            "monster_damage_reduction": mr_info["monster_damage_reduction"],
            "monster_dr_factor": mr_info["monster_dr_factor"],
            "monster_dr_source": mr_info["source"].get("url"),
        }
    )
    scenario["monster_level"] = mr_info["monster_level"]
    scenario["monster_damage_reduction"] = mr_info["monster_damage_reduction"]
    scenario["monster_dr_factor"] = mr_info["monster_dr_factor"]
    level_label = (
        f"、无怪物 DR"
        if mr_info["monster_level"] == 0
        else f"、怪物等级 {mr_info['monster_level']}（DR {mr_info['monster_damage_reduction'] * 100:.0f}%）"
    )
    scenario.setdefault("scenario_id", ruleset.target_side.get("scenario_id", "scenario"))
    scenario.setdefault("name", "Boss、0 抗性、双星全覆盖、中心 1 米内")
    if not scenario.get("name", "").endswith(level_label):
        scenario["name"] = scenario["name"] + level_label
    target_conditions = scenario.get("target_conditions") or {}
    scenario["coverage"] = {
        "both_stars_cover_target": bool(target_conditions.get("both_stars_cover", True)),
        "center_condition_met": bool(target_conditions.get("target_within_center_1m", True)),
        "theoretical": True,
        "note": "双星全覆盖与中心条件同时满足是理论最优场景，不代表真实轨道物理可达。",
    }

    # Optional scenario override for scenario-applied reports (e.g. the local
    # one-click recompute service).  The override may carry the prepared target
    # object (including resistance shred), target_conditions, coverage and name.
    if isinstance(scenario_override, dict) and scenario_override:
        for key in ("scenario_id", "name", "target_conditions", "coverage"):
            if key in scenario_override:
                scenario[key] = copy.deepcopy(scenario_override[key])
        if isinstance(scenario_override.get("target"), dict):
            scenario["target"] = copy.deepcopy(scenario_override["target"])
        for key in ("monster_level", "monster_damage_reduction", "monster_dr_factor"):
            if key in scenario_override:
                scenario[key] = scenario_override[key]
        target_conditions = scenario.get("target_conditions") or {}

    if isinstance(precomputed_outputs, dict) and precomputed_outputs:
        # Reuse one-pass scenario outputs (single from snapshot_single_cast,
        # loops from snapshot_loops) instead of re-running the simulation.
        if isinstance(precomputed_outputs.get("scenario"), dict):
            scenario = copy.deepcopy(precomputed_outputs["scenario"])
        single = copy.deepcopy(precomputed_outputs["single"])
        loops = copy.deepcopy(precomputed_outputs["loops"])
    else:
        single = run_single_cast(snapshot, ruleset, scenario)
        loops = [
            run_loop(snapshot, ruleset, scenario, policy)
            for policy in ruleset.action_policies
        ]

    cd = compute_cooldown(snapshot, ruleset).to_dict()
    cast = compute_cast_time(snapshot, ruleset)
    duration_s = float(single["body"]["duration"]["duration_s"])
    md_data = monster_dr.load_monster_dr()
    monster_summary = {
        **mr_info,
        "levels": {str(k): float(v) for k, v in md_data["levels_int"].items()},
        "level_range": md_data.get("level_range", [1, 100]),
        "dps_by_level": _dps_by_monster_level(single, loops, mr_info["monster_level"]),
        "note": (
            "KPI/窗口数值已按所选怪物等级的 (1 - DR) 结算；dps_by_level 是同一模型按各等级换算的"
            "游戏内实际伤害（仅怪物 DR 换算，难度/机制/操作覆盖率不建模）。"
        ),
    }

    rule_ledger = list(ruleset.rules)
    rule_ledger += build_derived_rules(snapshot, ruleset, cd, cast, duration_s, single["body"], single["ignite"])

    source_ledger = [c.to_dict() for c in sorted(snapshot.contributions, key=model.contribution_sort_key)]
    excluded = list(snapshot.excluded_sources or [])
    issues = list(snapshot.issues or [])
    issues.extend(_engine_issues(snapshot, ruleset, loops, single))
    checks = consistency_checks(single, loops)
    if not checks["all_pass"]:
        issues.append(
            {
                "issue_id": "issue-consistency",
                "severity": "error",
                "title": "结果一致性校验失败",
                "detail": json.dumps({k: v for k, v in checks.items() if v is False}, ensure_ascii=False),
                "scope": "result",
            }
        )

    has_blocking = any(r.get("status") in ("assumed", "unresolved") for r in rule_ledger)
    run_status = "assumption" if has_blocking else "complete"

    fingerprints = {
        "build_spec_sha256": (snapshot.build_meta or {}).get("build_spec_sha256"),
        "build_content_sha256": (snapshot.build_meta or {}).get("content_sha256"),
        "calc_input_sha256": (snapshot.build_meta or {}).get("calc_input_sha256"),
        "database_sha256": (snapshot.build_meta or {}).get("database_sha256"),
        "dataset_logical_content_sha256": (snapshot.build_meta or {}).get("dataset_logical_content_sha256"),
    }
    fingerprints.update(extra_fingerprints or {})

    result: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "run": {
            "run_id": run_id,
            "generated_at": generated_at,
            "engine": {"id": ENGINE_ID, "version": __version__},
            "ruleset": {
                "id": ruleset.ruleset_id,
                "sha256": ruleset.sha256,
                "game_version": ruleset.game_version,
                "path": ruleset_path or ruleset.path,
            },
            "status": run_status,
            "status_reason": (
                f"{ruleset.ruleset_id} 规则包仍包含显式假设/未解决项（见 rule_ledger 中 "
                "status=assumed/unresolved）；结果不能标为精确值。"
                if run_status == "assumption"
                else "全部规则均已确认或由确认输入推导。"
            ),
            "scope_note": SCOPE_NOTE,
            "repro_command": repro_command,
        },
        "input": {
            "dataset": (snapshot.build_meta or {}).get("dataset", {}),
            "build": (snapshot.build_meta or {}).get("build", {}),
            "calc_input": (snapshot.build_meta or {}).get("calc_input", {}),
            "fingerprints": fingerprints,
        },
        "scope": {
            "included": [
                "Black Hole 本体（含 Red Giant 转火与 Binary System 双星）",
                "Black Hole 自然施加的 Ignite（含 Heat Wave 施加模型与尾部结算）",
                "角色装备、神像、祝福、角色被动",
            ],
            "notes": [
                "排除 Meteor / Enchant Weapon / Flame Ward / Frost Wall / Teleport 的主动与被动技能树效果",
                "排除编织树与容器、外部队友增益、其他技能施加的 Ignite",
                "CD 循环为充能/持续时间调度，不是完整实战操作序列",
            ],
        },
        "scenario": scenario,
        "action_policy": {
            "single_cast": ruleset.data.get("single_cast_observation", {}),
            "loops": [p for p in ruleset.action_policies],
        },
        "snapshot": snapshot.to_dict(),
        "source_ledger": source_ledger,
        "rule_ledger": rule_ledger,
        "modifier_graph": build_modifier_graph(snapshot, ruleset, single, cooldown=cd, loops=loops),
        "single_cast": single,
        "loops": loops,
        "issues": issues,
        "excluded_sources": excluded,
        "cooldown": cd,
        "cast": cast,
        "monster_dr": monster_summary,
        "consistency": checks,
    }
    if out_dir:
        result["run"]["out_dir"] = str(out_dir)
    return result


def _rule_status(ruleset: Ruleset, rule_id: str) -> str:
    for rule in ruleset.data.get("rules", []):
        if rule.get("rule_id") == rule_id:
            return str(rule.get("status"))
    return "unknown"


def _engine_issues(
    snapshot: model.Snapshot,
    ruleset: Ruleset,
    loops: list[dict[str, Any]],
    single: dict[str, Any],
) -> list[dict[str, Any]]:
    tick_model = ruleset.black_hole.get("tick_model") or {}
    tick_count = int((single.get("body") or {}).get("tick_count") or 0)
    hw = ruleset.ignite.get("heat_wave") or {}
    app = (single.get("ignite") or {}).get("application") or {}
    roll_interval = float(
        hw.get("application_interval_s") or (1.0 / float(hw.get("rolls_per_second", 1) or 1))
    )
    binary_confirmed = _rule_status(ruleset, "rule-bh-binary-more-interpretation") == "confirmed"
    tick_confirmed = str(tick_model.get("remainder_policy")) in (
        "ceil_full_ticks",
        "integer_ticks",
        "full_ticks",
    )
    cooldown_confirmed = bool((ruleset.cooldown or {}).get("cdr_applied_separately"))
    totals = single.get("totals") or {}
    body_total = float(totals.get("body_total") or 0.0)
    ignite_total = float(totals.get("ignite_total") or 0.0)

    issues: list[dict[str, Any]] = [
        {
            "issue_id": "engine-user-confirmed-elemental-int",
            "severity": "info",
            "title": "Elemental 标签与 Intelligence 继承已确认（用户规则 + 1.5.3 代码）",
            "detail": "用户确认 Elemental 标签等价于 Fire/Cold/Lightning，因此进入 Ignite；"
            "1.5.3 的 Tags.Applicable/Stats.GetTotalIncreased 代码确认了 Elemental 位展开逻辑。"
            "Intelligence 的 inc 先作用于 Black Hole 再传递给其 Ignite；引擎按此计算。",
            "scope": "ignite",
        },
        {
            "issue_id": "engine-heat-wave-expected",
            "severity": "info",
            "title": "Heat Wave 使用期望值模型（0.5s 光环，每黑洞一个）",
            "detail": f"生成时刻首次判定、之后每 {roll_interval:g}s 一次；每个 Black Hole 只有一个 "
            f"Heat Wave 光环（Binary 双星共用，1.5.3 Mutate 代码确认）。本次施放 "
            f"{app.get('roll_count')} 次判定 × {float(app.get('expected_stacks_per_roll', 0.0)):.6g} "
            f"= {float(app.get('expected_stacks_applied', 0.0)):.6g} 期望层；层数为独立判定的期望值。",
            "scope": "ignite",
        },
        {
            "issue_id": "engine-penetration-ignite",
            "severity": "info",
            "title": "穿透应用于 Ignite（含 Fire Penetration with Ignite）",
            "detail": "依据 tunklab Penetration 描述（作用于所有伤害含 DoT、可压到 0 以下）与 Ignite 的 "
            "additionalPenetrationDamageType=1(Fire)，引擎将玩家火焰穿透计入 Ignite；装备来源的 35% "
            "'Fire Penetration with Ignite' 按 phase-1 数据集 003 的 specialTag 显示映射作为仅作用于点燃的"
            "火焰穿透（见 engine-fire-penetration-with-ignite）。",
            "scope": "target",
        },
    ]
    if binary_confirmed:
        issues.append(
            {
                "issue_id": "engine-binary-more-confirmed",
                "severity": "info",
                "title": "Binary System +100% 已确认（独立 more + Ignite 继承）",
                "detail": f"用户 2026-11-05 实测 Binary 开/关总 DPS 400k→800k（DPS 主要为点燃）"
                f"→ 总伤害 ×2 且 Ignite 继承该 more；1.5.3 代码确认节点在结构双分量之外额外构造 "
                f"Stats.Stat(Damage, more=1.0)。本次单次：本体 {body_total:,.0f}、Ignite {ignite_total:,.0f}。",
                "scope": "body",
            }
        )
    else:
        issues.append(
            {
                "issue_id": "engine-binary-more-uncertainty",
                "severity": "warning",
                "title": "Binary System 的 +100% 语义未确定",
                "detail": "引擎按当前规则包解释 Binary 的 +100%；该解释未确认时不应把结果标为精确值。",
                "scope": "body",
            }
        )
    issues.append(
        {
            "issue_id": "engine-fire-penetration-with-ignite",
            "severity": "info",
            "title": "装备词条修正：'Fire Penetration with Ignite'（数据集 003 + 本地 BD 导入修复）",
            "detail": "此前装备导入把 Fiery Dragon Shoes 的 35% 词条当作 'Increased Ignite Effect'；"
            "实际游戏行是 'Fire Penetration with Ignite'。已修正 phase-1 数据集并重新导入本地 BD。"
            "数值口径不变：该 35% 只作用于 Ignite 火焰穿透（与 41% 玩家火焰穿透合并 = 76%，目标 0 抗 → ×1.76）。",
            "scope": "ignite",
        }
    )
    if tick_confirmed:
        issues.append(
            {
                "issue_id": "engine-tick-model-confirmed",
                "severity": "info",
                "title": "本体整数跳（1.5.3 代码确认）",
                "detail": f"每 0.3s 一跳且最后一跳按满额结算；跳数 = ceil(duration/0.3) = {tick_count}，"
                "不再把不足一跳的余量按比例折算（1.5.3 OnUpdateTick/OnDestroy 代码确认）。",
                "scope": "body",
            }
        )
    else:
        issues.append(
            {
                "issue_id": "engine-reference-tick-conflict",
                "severity": "warning",
                "title": "本体跳数口径与参考模型冲突（当前规则包采用 rate×duration）",
                "detail": "当前规则包按 rate×duration 合并末跳余量；参考模型为整数跳。",
                "scope": "body",
            }
        )
    issues.append(
        {
            "issue_id": "engine-reference-cast-time-conflict",
            "severity": "info",
            "title": "施法时间采用参考公式（尚未游戏内实测）",
            "detail": "当前采用参考公式 cast=1/(as×speedMultiplier×(1+cast speed))=0.5043s；"
            "旧口径 0.6/(1+cast speed)=0.4438s。两者差约 12%，对 120s DPS 影响约 -0.8%。",
            "scope": "loops",
        }
    )
    if cooldown_confirmed:
        issues.append(
            {
                "issue_id": "engine-cooldown-confirmed",
                "severity": "info",
                "title": "Recurrence 冷却缩减确认为 (1−CDR)（用户实测 3.6s）",
                "detail": "用户 2026-11-05 实测充能回满 3.6s，对应 "
                "(base 14 + Endless Maw 2) × (1 − Recurrence 0.6) / (1 + ICR 0.7575) = 3.641s；"
                "1.5.3 代码确认 BlackHoleMutator.getChargeRegen 以 (1−CDR) 除基础充能率。"
                "v1 的 ICR 加算池公式（6.787s）已被否定。",
                "scope": "loops",
            }
        )
    else:
        issues.append(
            {
                "issue_id": "engine-cooldown-mapping",
                "severity": "warning",
                "title": "Recurrence 的冷却缩减归入 ICR 池（映射未确认）",
                "detail": "当前规则包按 (base+flat)/(1+ICR) 处理；该映射未确认。",
                "scope": "loops",
            }
        )
    if len(loops) >= 2:
        eager = loops[0]
        patient = loops[1]
        issues.append(
            {
                "issue_id": "engine-multi-black-hole-overlap",
                "severity": "info",
                "title": "多黑洞并存已确认（1.5.3 代码 + 用户规则）",
                "detail": f"数量上限只针对单次施放；多次施放的黑洞独立共存（1.5.3 activeHoles 代码确认）。"
                f"eager（有充能即施放）120s DPS 约 {float(eager['totals'].get('horizon_dps', 0.0)):,.2f}、"
                f"最多 {eager['totals'].get('max_concurrent_black_holes')} 个并存；"
                f"patient（等全部黑洞消失再施放）约 {float(patient['totals'].get('horizon_dps', 0.0)):,.2f}、"
                f"最多 {patient['totals'].get('max_concurrent_black_holes')} 个并存。",
                "scope": "loops",
            }
        )
    issues.extend(
        [
            {
                "issue_id": "engine-crescendoing-rage-deferred",
                "severity": "info",
                "title": "Crescendoing Rage 本阶段不纳入（用户决定后续阶段处理）",
                "detail": "用户 2026-11-04：暂不考虑，放在后面阶段处理；当前规则包未应用。",
                "scope": "loops",
            },
            {
                "issue_id": "engine-pyromancer-ignite-chance",
                "severity": "info",
                "title": "Pyromancer 的 +40% Ignite Chance 未参与 Heat Wave（用户判断与 hit 相关）",
                "detail": "用户 2026-11-04 判断该 ignite chance 应与 hit 相关，不参与 Heat Wave 的按秒判定；"
                "当前规则包未计入，保留为待确认项。",
                "scope": "ignite",
            },
            {
                "issue_id": "engine-preparation-excluded",
                "severity": "info",
                "title": "Preparation 未默认生效",
                "detail": "Preparation（4 点）的『下一次元素法术』增益在 t=0 的就绪状态与循环中的消耗/刷新时序"
                "未确认；当前规则包不默认生效。",
                "scope": "single_cast",
            },
        ]
    )
    quest_applied = any(
        getattr(c, "contribution_id", None) == "mod:quest.reward#all_attributes"
        and getattr(c, "included", False)
        for c in snapshot.contributions
    )
    if not quest_applied:
        issues.append(
            {
                "issue_id": "engine-quest-attributes",
                "severity": "info",
                "title": "未纳入任务属性点（+2 全属性）",
                "detail": f"参考模型推导本构筑属性应含任务奖励 +2 全属性（Int 44）；"
                f"当前快照为 Int {snapshot.attribute('intelligence'):g}。"
                "影响本体/Ignite 各 +0.08 inc（约 +0.5% 总伤害），当前规则包未纳入。",
                "scope": "input",
            }
        )
    if len(loops) >= 2:
        eager = loops[0]
        patient = loops[1]
        if eager["totals"]["horizon_damage"] > patient["totals"]["horizon_damage"] * 1.05:
            issues.append(
                {
                    "issue_id": "engine-policy-difference",
                    "severity": "info",
                    "title": "两种操作策略结果不同（重叠 vs 不重叠）",
                    "detail": f"eager 120s 窗口总伤害 {eager['totals']['horizon_damage']:,.0f}，"
                    f"patient {patient['totals']['horizon_damage']:,.0f}；"
                    f"eager 最大并存黑洞 {eager['totals'].get('max_concurrent_black_holes')}，"
                    f"patient {patient['totals'].get('max_concurrent_black_holes')}。",
                    "scope": "loops",
                }
            )
    return issues


def write_json(path: Path | str, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")


def _conditional_unavailable(exc: Optional[BaseException] = None) -> dict[str, Any]:
    """Degraded conditional-context payload; never breaks a damage run."""

    message = (
        "条件 Buff 模块不可用：未找到或无法读取 le_calc.conditional / 条件效果注册表。"
        if exc is None
        else f"条件 Buff 模块不可用：{exc}"
    )
    return {
        "schema": "bh-conditional-context-v1",
        "status": "unavailable",
        "issues": [
            {
                "severity": "warning",
                "code": "conditional-package-unavailable",
                "message": message,
            }
        ],
        "registry": {},
        "input": {},
        "context": {},
        "catalog": {},
        "scenario": {},
        "evaluation": {"summary": {}, "effects": [], "proposed_contributions": [], "unsupported": []},
        "rules": {},
    }


def attach_recompute(
    payload: dict[str, Any],
    snapshot: model.Snapshot,
    ruleset: Ruleset,
    *,
    monster_level: Optional[int] = None,
) -> dict[str, Any]:
    """Run the cross-skill conditional recompute for the attached scenario.

    The recompute is additive documentation/capability: it never changes the
    legacy DPS baseline.  Failures degrade to a structured status instead of
    breaking report generation.
    """

    if not isinstance(payload, dict) or payload.get("status") not in ("ready", "degraded"):
        return payload
    try:
        from .conditional import recompute_scenario
        from .conditional.registry import load_registry

        registry = load_registry((payload.get("registry") or {}).get("path") or None)
        payload["recompute"] = recompute_scenario(
            snapshot,
            ruleset,
            scenario=payload.get("scenario") or {},
            registry=registry,
            catalog=payload.get("catalog") or {},
            evaluation=payload.get("evaluation") or {},
            monster_level=monster_level,
            include_auto=None,
        )
    except Exception as exc:  # a channel bug must not kill the report
        payload["recompute"] = {
            "schema": "bh-conditional-recompute-v1",
            "status": "error",
            "warnings": [f"条件重算不可用：{exc}"],
            "dps_recomputed": False,
        }
    return payload


def attach_conditional(
    result: dict[str, Any],
    snapshot: model.Snapshot,
    ruleset: Ruleset,
    *,
    ruleset_path: Optional[str] = None,
    calc_input_path: Optional[str] = None,
    build_path: Optional[str] = None,
    registry_path: Optional[str] = None,
    scenario_path: Optional[str] = None,
    preset_id: Optional[str] = None,
    repo_root: Optional[str] = None,
    monster_level: Optional[int] = None,
    recompute: bool = True,
) -> dict[str, Any]:
    """Attach the conditional-Buff context to ``result`` (in place) and return it.

    The conditional package builds discovery / scenario / evaluation / page-view
    data from the frozen snapshot and recorded build inputs.  It must never
    change damage numbers: the engine totals are computed before this step.
    ``recompute=True`` additionally runs the opt-in cross-skill Buff channel for
    the generated scenario (stored as ``conditional.recompute``).
    """

    try:
        from .conditional import build_context  # local import: optional feature
    except Exception as exc:  # pragma: no cover - only before the package exists
        payload = _conditional_unavailable(exc)
    else:
        try:
            payload = build_context(
                snapshot,
                scenario=result.get("scenario"),
                ruleset=ruleset,
                ruleset_path=ruleset_path or (result.get("run", {}).get("ruleset") or {}).get("path"),
                calc_input_path=calc_input_path,
                build_path=build_path,
                registry_path=registry_path,
                scenario_path=scenario_path,
                preset_id=preset_id,
                repo_root=repo_root or str(Path.cwd()),
            )
        except Exception as exc:  # defensive: a research data bug must not kill DPS runs
            payload = _conditional_unavailable(exc)
    if not isinstance(payload, dict):
        payload = _conditional_unavailable()
    if recompute:
        attach_recompute(payload, snapshot, ruleset, monster_level=monster_level)
    result["conditional"] = payload
    return payload


def render_run_readme(result: dict[str, Any]) -> str:
    run = result["run"]
    single = result["single_cast"]["totals"]
    lines: list[str] = []
    lines.append(f"# Black Hole 单技能伤害分析（{run['run_id']}）")
    lines.append("")
    lines.append(f"- 状态：**{run['status']}** — {run.get('status_reason', '')}")
    lines.append(f"- 规则包：`{run['ruleset']['id']}`（sha256 `{str(run['ruleset'].get('sha256'))[:16]}…`，游戏版本 {run['ruleset']['game_version']}）")
    build = result["input"].get("build") or {}
    dataset = result["input"].get("dataset") or {}
    lines.append(f"- 构筑：`{build.get('build_id')}` revision `{build.get('revision_id')}`")
    lines.append(f"- 数据集：`{dataset.get('dataset_id')}`（目标版本 {dataset.get('target_game_version')}）")
    lines.append(f"- 范围：{run.get('scope_note', '')}")
    lines.append(
        "- 报告模板：`report_ui` UI 改版 v3.4（2026-11-07 独立审查修复；2026-11-06 条件 Buff 页；沿用 2026-11-05 基线）；"
        "后续伤害报告统一沿用该模板与交互（见 作者本地历史记录（不随公开仓库分发））"
    )
    if run.get("repro_command"):
        lines.append(f"- 复现：`{run['repro_command']}`")
    lines.append("")
    lines.append("## 关键结果")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---:|")
    lines.append(f"| 单次完整伤害 | {single['single_cast_total']:,.1f} |")
    lines.append(f"| 本体伤害 | {single['body_total']:,.1f} |")
    lines.append(f"| Ignite 总伤害 | {single['ignite_total']:,.1f} |")
    lines.append(f"| Ignite 尾部伤害 | {single['ignite_tail_damage']:,.1f} |")
    lines.append(f"| 期望施加层数 | {single['expected_total_stacks_applied']:,.1f} |")
    lines.append(f"| 观察结束时刻 (s) | {single['observation_end_s']:.4f} |")
    monster = result.get("monster_dr") or {}
    if monster:
        level = monster.get("monster_level")
        dr = float(monster.get("monster_damage_reduction", 0.0))
        if level in (None, 0):
            lines.append("| 怪物等级 | 0（无怪物 DR） |")
        else:
            lines.append(
                f"| 怪物等级 | {level}（怪物 DR {dr * 100:.0f}%，KPI 已按 ×{1.0 - dr:.2f} 结算） |"
            )
    lines.append("")
    for loop in result.get("loops", []):
        lines.append(f"### 循环策略 `{loop['policy_id']}`（{loop.get('label','')}）")
        lines.append("")
        lines.append("| 窗口 (s) | 总伤害 | DPS | 未结算尾部 | 施放次数 |")
        lines.append("|---:|---:|---:|---:|---:|")
        for window in loop.get("windows", []):
            lines.append(
                f"| {window['window_s']:g} | {window['total_damage']:,.1f} | {window['dps']:,.1f} | "
                f"{window['unsettled_tail_damage']:,.1f} | {window['cast_count']} |"
            )
        totals = loop.get("totals", {})
        lines.append("")
        lines.append(
            f"施放次数 {totals.get('cast_count')}，循环间隔 {totals.get('cycle_s')} s，"
            f"稳态交叉验证 DPS {totals.get('steady_state_dps_cross_check'):,.1f}"
            if totals.get("steady_state_dps_cross_check") is not None
            else f"施放次数 {totals.get('cast_count')}"
        )
        lines.append("")
    lines.append("## 文件")
    lines.append("")
    lines.append("- `result.json`：唯一业务数据源（HTML 内嵌同一份数据）")
    lines.append("- `input-snapshot.json`：冻结的静态属性/来源快照")
    lines.append("- `source-ledger.json`：逐条属性来源账（含排除原因、条件状态、证据 locator）")
    lines.append("- `rule-ledger.json`：规则与乘区账（confirmed / derived / assumed / unresolved）")
    lines.append("- `run-manifest.json`：输入指纹、文件 sha256、一致性校验结果")
    lines.append("- `black-hole-damage.html`：离线单文件报告（双击打开，无网络请求；含「条件 Buff」页）")
    conditional = result.get("conditional") or {}
    if conditional.get("status") in ("ready", "degraded"):
        lines.append("- `conditional-effects.json`：条件效果注册表指纹、主技能依赖与来源检查目录")
        lines.append("- `conditional-scenario.json`：本轮条件场景（可保存/恢复/交给后续重算）")
        lines.append("- `conditional-evaluation.json`：条件求值、拟议贡献与校验结果（保持 DPS 基线口径；不是场景重算）")
        lines.append("- `conditional-recompute.json`：跨技能 Buff 输入通道对“生成场景”的重算结果（不改写 DPS 基线）")
    lines.append("")
    lines.append("## 条件 Buff 配置")
    lines.append("")
    if conditional.get("status") in ("ready", "degraded"):
        evaluation = conditional.get("evaluation") or {}
        summary = evaluation.get("summary") or {}
        registry = conditional.get("registry") or {}
        lines.append(
            f"- 状态：**{conditional.get('status')}**；注册表 `{registry.get('id')}`"
            f"（sha256 `{str(registry.get('sha256'))[:16]}…`）"
        )
        if summary:
            lines.append(
                "- 检查对象 {checked} · 候选 {candidates} · 已配置 {configured} · 相关 {relevant}"
                " · 条件已满足 {condition_met} · 自动生效 {auto_active}".format(
                    checked=summary.get("checked_objects", 0),
                    candidates=summary.get("candidates", 0),
                    configured=summary.get("configured", 0),
                    relevant=summary.get("relevant", 0),
                    condition_met=summary.get("condition_met", 0),
                    auto_active=summary.get("auto_active", 0),
                )
            )
        lines.append(
            "- 口径：固定条件估算——选中的持续状态在观察期间保持，不模拟获得/维持成本；"
            "条件求值本身保持生成基线；条件场景重算单独写入 `conditional-recompute.json`，可选一键重算生成场景整页报告；`result.json` 的基线伤害字段不变。"
        )
        lines.append(
            "- 场景与求值：`conditional-scenario.json` / `conditional-evaluation.json`；"
            "页面「条件 Buff」可勾选、设层数、导出/导入场景 JSON。"
        )
        lines.append(
            "- 统一目标（v3.4）：场景 `target` 含怪物等级/目标火抗/boss/rare；导出、导入与一键重算均携带，"
            "baseline 与 scenario 使用同一目标。"
        )
        lines.append(
            "- 应用状态（v3.4）：`conditional-recompute.json` 的 `application_states` 给出 17 个效果的实际应用状态"
            "（已计入/仅单次/未计入/未启用/含假设）；场景整页报告按此显示。"
        )
        lines.append(
            "- 基线口径（v3.4）：`build_base` 含常驻自动效果（11,440,036.2），"
            "`legacy_single_skill` 不含（10,662,227.5）；由场景 `baseline_profile` 决定。"
        )
        recompute = conditional.get("recompute") or {}
        if recompute.get("status") == "ok":
            baseline_single = ((recompute.get("baseline") or {}).get("single") or {}).get("single_cast_total")
            scenario_single = ((recompute.get("scenario") or {}).get("single") or {}).get("single_cast_total")
            applied = ((recompute.get("condition_application") or {}).get("applied") or [])
            if isinstance(baseline_single, (int, float)) and isinstance(scenario_single, (int, float)):
                lines.append(
                    "- 条件场景重算（跨技能 Buff 输入通道）：单次 "
                    f"{baseline_single:,.1f} → {scenario_single:,.1f}；应用 {len(applied)} 条条件贡献；"
                    "旧 DPS 基线数值不变。详见 `conditional-recompute.json`。"
                )
            else:
                lines.append("- 条件场景重算：已生成（见 `conditional-recompute.json`）；旧 DPS 基线数值不变。")
            lines.append(
                "- 一键重算（可选）：本机运行 `le_calc serve` 后，在条件 Buff 页点击“应用条件并重算 DPS”"
                "即可整页重算；报告本身不会自动联网。"
            )
        elif recompute:
            lines.append(f"- 条件场景重算：**{recompute.get('status')}**（见 `conditional-recompute.json`）。")
        for issue in conditional.get("issues") or []:
            lines.append(f"- [{issue.get('severity', 'info')}] {issue.get('message', '')}")
    else:
        lines.append("- 状态：**unavailable** — 本报告未包含条件 Buff 配置数据（旧结果或注册表不可用）。")
    lines.append("")
    lines.append("## 未解决 / 假设摘要")
    lines.append("")
    for status in ("unresolved", "assumed"):
        rules = [r for r in result.get("rule_ledger", []) if r.get("status") == status]
        if rules:
            lines.append(f"### {status}")
            lines.append("")
            for rule in rules:
                lines.append(f"- `{rule['rule_id']}` {rule['title']}：{rule['statement']}")
            lines.append("")
    warnings = [i for i in result.get("issues", []) if i.get("severity") in ("warning", "error")]
    if warnings:
        lines.append("### 需要知悉的 issues")
        lines.append("")
        for issue in warnings:
            lines.append(f"- [{issue.get('severity')}] {issue.get('title')}：{issue.get('detail')}")
        lines.append("")
    lines.append("## 重要限制")
    lines.append("")
    lines.append("- 本报告是**限定来源、限定目标场景**的 Black Hole 本体及其自然 Ignite 的单次伤害／循环 DPS，不是完整 BD 实战总 DPS。")
    lines.append("- 场景为合成 Boss 靶（0 抗性）+ 双星全覆盖 + 中心条件满足的理论最优场景，不代表真实轨道物理可达。")
    lines.append("- 未实现：法力消耗与恢复、Chill/Blind 等非伤害状态、外部队友增益；其他技能/装备/被动的条件效果由「条件 Buff」页单独发现与配置，不重算本报告 DPS。")
    lines.append("- 任何 assumed / unresolved 规则都会使结果不能称为精确值；详见 `rule-ledger.json` 与 `issues`。")
    lines.append("")
    return "\n".join(lines)


def write_conditional_artifacts(conditional: dict[str, Any], out_dir: str | Path) -> dict[str, str]:
    """Write the three conditional-Buff artefacts (idempotent, deterministic).

    Returns ``{filename: path}`` for the files actually written.  The payloads
    are pure data already present in ``result["conditional"]``; nothing here
    touches the damage result.
    """

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if not isinstance(conditional, dict) or conditional.get("status") not in ("ready", "degraded"):
        return {}
    written: dict[str, str] = {}
    effects_payload = {
        "schema": "bh-conditional-effects-v1",
        "registry": conditional.get("registry") or {},
        "input": conditional.get("input") or {},
        "context": conditional.get("context") or {},
        "catalog": conditional.get("catalog") or {},
    }
    scenario = conditional.get("scenario") or {}
    evaluation = conditional.get("evaluation") or {}
    recompute = conditional.get("recompute") or {}
    evaluation_payload = {
        "schema": "bh-conditional-evaluation-v1",
        "registry": conditional.get("registry") or {},
        "input": conditional.get("input") or {},
        "scenario_fingerprint": evaluation.get("scenario_fingerprint"),
        "baseline_fingerprint": evaluation.get("baseline_fingerprint"),
        "changed_from_baseline": bool(evaluation.get("changed_from_baseline")),
        "dps_recomputed": False,
        "evaluation": evaluation,
    }
    for name, payload in (
        ("conditional-effects.json", effects_payload),
        ("conditional-scenario.json", scenario),
        ("conditional-evaluation.json", evaluation_payload),
        ("conditional-recompute.json", recompute),
    ):
        if not payload:
            continue
        path = out / name
        write_json(path, payload)
        written[name] = str(path)
    return written


def _write_conditional_artifacts(conditional: dict[str, Any], out: Path) -> dict[str, str]:
    return write_conditional_artifacts(conditional, out)


def write_run(
    result: dict[str, Any],
    out_dir: str | Path,
    *,
    render_html: bool = True,
) -> dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}

    source_ledger = result["source_ledger"]
    rule_ledger = result["rule_ledger"]
    snapshot = result["snapshot"]

    paths = {
        "result.json": result,
        "input-snapshot.json": snapshot,
        "source-ledger.json": source_ledger,
        "rule-ledger.json": rule_ledger,
    }
    for name, payload in paths.items():
        p = out / name
        write_json(p, payload)
        written[name] = str(p)
    readme_path = out / "README.zh-CN.md"
    readme_path.write_text(render_run_readme(result), encoding="utf-8")
    written["README.zh-CN.md"] = str(readme_path)

    conditional = result.get("conditional") or {}
    extra: dict[str, str] = {}
    if conditional.get("status") in ("ready", "degraded"):
        extra = _write_conditional_artifacts(conditional, out)
        written.update(extra)
        if extra:
            # keep the run README/manifest in sync with the conditional summary
            readme_path.write_text(render_run_readme(result), encoding="utf-8")
            written["README.zh-CN.md"] = str(readme_path)

    manifest = {
        "run": result["run"],
        "input": result["input"],
        "scope": result["scope"],
        "scenario": result["scenario"],
        "consistency": result.get("consistency", {}),
        "files": {},
    }
    if conditional.get("status"):
        manifest["conditional"] = {
            "schema": conditional.get("schema"),
            "status": conditional.get("status"),
            "registry": conditional.get("registry") or {},
            "evaluation_summary": (conditional.get("evaluation") or {}).get("summary") or {},
        }
    extra_names = tuple(extra) if conditional.get("status") in ("ready", "degraded") else ()
    manifest["files"] = {
        name: hashlib.sha256((out / name).read_bytes()).hexdigest()
        for name in (*paths, "README.zh-CN.md", *extra_names)
        if (out / name).is_file()
    }
    write_json(out / "run-manifest.json", manifest)
    written["run-manifest.json"] = str(out / "run-manifest.json")

    if render_html:
        try:
            from .html import render
        except Exception as exc:  # pragma: no cover - html team may still be writing it
            written["black-hole-damage.html"] = f"skipped: {exc}"
        else:
            html = render(result)
            html_path = out / "black-hole-damage.html"
            html_path.write_text(html, encoding="utf-8")
            written["black-hole-damage.html"] = str(html_path)
            manifest["files"]["black-hole-damage.html"] = hashlib.sha256(
                html_path.read_bytes()
            ).hexdigest()
            write_json(out / "run-manifest.json", manifest)
    return written


def load_and_build(
    snapshot: model.Snapshot,
    *,
    run_id: str,
    generated_at: str,
    ruleset_path: Optional[str] = None,
    out_dir: Optional[str] = None,
    extra_fingerprints: Optional[dict[str, Any]] = None,
    repro_command: Optional[str] = None,
    monster_level: Optional[int] = None,
    conditional_registry: Optional[str] = None,
    calc_input_path: Optional[str] = None,
    build_path: Optional[str] = None,
    conditional_scenario: Optional[str] = None,
    conditional_preset: Optional[str] = None,
    enable_conditional: bool = True,
    enable_recompute: bool = True,
) -> dict[str, Any]:
    ruleset = load_ruleset(ruleset_path)
    result = build_result(
        snapshot,
        ruleset,
        run_id=run_id,
        generated_at=generated_at,
        ruleset_path=ruleset_path,
        out_dir=out_dir,
        extra_fingerprints=extra_fingerprints,
        repro_command=repro_command,
        monster_level=monster_level,
    )
    if enable_conditional:
        attach_conditional(
            result,
            snapshot,
            ruleset,
            ruleset_path=ruleset_path or ruleset.path,
            calc_input_path=calc_input_path,
            build_path=build_path,
            registry_path=conditional_registry,
            scenario_path=conditional_scenario,
            preset_id=conditional_preset,
            repo_root=str(Path.cwd()),
            monster_level=monster_level,
            recompute=enable_recompute,
        )
    return result


__all__ = [
    "DEFAULT_SCENARIO_NOTE",
    "SCOPE_NOTE",
    "attach_conditional",
    "attach_recompute",
    "build_result",
    "consistency_checks",
    "load_and_build",
    "render_run_readme",
    "write_conditional_artifacts",
    "write_json",
    "write_run",
]
