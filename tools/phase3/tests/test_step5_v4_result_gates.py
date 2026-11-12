"""P1-C 结果门禁 + P1-A actor 命中调用侧接线的定向测试。

背景：``docs/reviews/ballista-armed-construction-independent-review-20261113.zh-CN.md``
§2 指出 v4-actor 命中装配未做伤害通道类型隔离（Fire 附加伤害串入其它六类），
§4 指出 assembly 分支结果门禁只做字段自洽、账本自己证明 known 组件，因而
coverage/账本引用/组件 damage_total/贡献 value 的篡改都能通过。

本测试文件覆盖本轮新增的两条边界：

1. **调用侧接线**：``plan_v4._execute_native_v4`` 在 hit 组件 ``scope=="actor"``
   时向 ``hit_component_assembly-v1`` 传 ``typed_added_policy="by_type"`` 与
   ``isolate_damage_channels=True``；非 actor 路径保持历史默认（legacy/不隔离）。
2. **结果门禁**：``_validate_assembly_result`` / ``_assembly_stage_checks`` 现在
   用 plan/execution 声明构造 known 组件集合，校验账本引用、应用摘要计数、
   totals/windows 算式、coverage 投影和 contributions 与 execution 行绑定。

测试分三类，全部在内存中构造/变异，不写 examples/reports/knowledge：

- 合成 assembly plan（actor 与非 actor 两份）经真实 ``execute_plan_v4`` 执行；
- 对执行结果做内存变异，反例必须被 ``validate_result_v6`` 与正式阶段校验路径拒绝；
- 只读历史 run（Ballista/VC/FR/BH）作为旧行为/回归证据，文件不存在时跳过。
"""
from __future__ import annotations

import copy
import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc import calculation_plan as planmod  # noqa: E402
from le_calc import plan_v4  # noqa: E402

#: workflow ``validate_stage`` 对 v4 计划要求的 10 个 check id。
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

#: 只读历史 run：用于旧行为不变与真实案例接线证据。
BALLISTA_RUN = ROOT / "reports/ballista-beta5-native-20261112/dps-ballista-armed-construction-1"
LEGACY_RUNS = {
    "vc": ROOT / "reports/void-cleave-20261110/dps-step5-v4-vc-13",
    "fr": ROOT / "reports/flame-reave/dps-step5-v4-fr-18",
    "bh": ROOT / "reports/black-hole-beta33-v4-20261112/dps-step5-v4-beta33-13",
}
SUBJECT = {
    "case_id": "syn-result-gates",
    "main_ability_id": "syn-actor-hit",
    "build_id": "syn-build",
    "revision_id": "syn-rev-1",
}

TYPED_ADDED_EFFECT = {
    "effect_id": "effect:added-typed",
    "contribution_id": "effect:added-typed",
    "emits_contribution": True,
    "source_refs": ["source:synth"],
    "ownership_key": {"owner": "actor", "key": "added-typed"},
    "scope": "all",
    "stat_key": "added_damage",
    "stat_name": "synthetic typed added",
    "operation": "added",
    "value": 10.0,
    "damage_tags": ["Bow", "Fire"],
    "source_kind": "derived",
    "source_label": "synthetic typed added",
    "source_path": "tests.result_gates",
    "unit": "flat",
    "value_mode": "static",
    "value_params": {},
    "included_static": True,
    "condition_status_static": "always",
    "condition_code": None,
    "target": None,
    "sequence": 2,
    "status": "derived",
}
UNTYPED_ADDED_EFFECT = {
    **TYPED_ADDED_EFFECT,
    "effect_id": "effect:added-untyped",
    "contribution_id": "effect:added-untyped",
    "ownership_key": {"owner": "actor", "key": "added-untyped"},
    "stat_name": "synthetic untyped added",
    "source_label": "synthetic untyped added",
    "value": 5.0,
    "damage_tags": ["Bow"],
    "sequence": 3,
}
INCREASED_EFFECT = {
    "effect_id": "effect:increased",
    "contribution_id": "effect:increased",
    "emits_contribution": True,
    "source_refs": ["source:synth"],
    "ownership_key": {"owner": "actor", "key": "increased"},
    "scope": "all",
    "stat_key": "damage",
    "stat_name": "synthetic increased damage",
    "operation": "increased",
    "value": 0.5,
    "damage_tags": ["Fire"],
    "source_kind": "derived",
    "source_label": "synthetic increased",
    "source_path": "tests.result_gates",
    "unit": "percent",
    "value_mode": "static",
    "value_params": {},
    "included_static": True,
    "condition_status_static": "always",
    "condition_code": None,
    "target": None,
    "sequence": 1,
    "status": "derived",
}
#: 两条静态覆盖缺口：一条伤害相关、一条 out_of_scope（用于 coverage 门禁与文案分类）。
COVERAGE_BASE_GAPS = [
    {
        "code": "unmapped:damage_relevant_unknown",
        "detail": "synthetic unmodeled damage source",
        "contribution_ids": ["effect:increased"],
        "count": 1,
    },
    {
        "code": "unmapped:out_of_scope",
        "detail": "synthetic out of scope source",
        "contribution_ids": ["effect:increased"],
        "count": 1,
    },
]


def _derived_derivation() -> dict:
    return {
        "kind": "summon_lifecycle",
        "upstream": {"kind": "scenario_control", "control": "trap_rate", "default": 4.0},
        "gate": {"kind": "chance", "probability": 0.2, "aggregate": "per_point_add"},
        "lifecycle": {
            "limit": 2,
            "duration_s": 10.0,
            "destroy_policy": "replace_or_expire",
            "events_per_summon": 1,
        },
        "rate_formula": "upstream_rate * gate_probability",
        "assertions": {"max_rate": "lifecycle.limit / lifecycle.duration_s", "on_violation": "warn"},
    }


def _actor_model() -> dict:
    return {
        "actor_id": "minion:syn:42",
        "scope": "actor",
        "actors": [
            {
                "actor_id": "minion:syn:42",
                "kind": "summon",
                "source_entity": "minion:syn:42",
                "inherits": "none",
                "base_effects": [],
            }
        ],
    }


def _legacy_plan(*, actor: bool) -> dict:
    facts: dict = {"coverage_base_gaps": copy.deepcopy(COVERAGE_BASE_GAPS)}
    capabilities = ["hit_components"]
    if actor:
        facts["actor_model"] = _actor_model()
        facts["stat_transfers"] = []
        capabilities = ["hit_components", "minion_actor", "explicit_stat_transfer", "derived_event_rate"]
    legacy = {
        "schema": "le-calculation-plan-v1",
        "version": "1.0.0",
        "subject": dict(SUBJECT),
        "case_ref": "",
        "components": [
            {
                "component_id": "hit:declared",
                "kind": "hit",
                "tags": ["Bow", "Fire", "Minion"],
                "crit_capable": True,
                "adaptive_damage_type": "Fire",
            }
        ],
        "parameters": {
            "skill": {
                "base_damage": {"Fire": 50.0},
                "added_damage_scaling": 2.5,
                "crit_chance": 0.05,
                "crit_multiplier": 2.0,
                "cooldown_s": 0.0,
                "action_s": 0.0,
                "skill_name": "SYNTH-ACTOR",
            },
            "crit": {},
            "ailments": [],
            "cooldown_reduction": 0.0,
            "additional_chance": {},
            "damage_to_penetration": None,
        },
        "controls": [
            {"key": "trap_rate", "namespace": "state", "default": 4.0, "type": "number"},
        ],
        "conditions": [],
        "effects": [
            copy.deepcopy(INCREASED_EFFECT),
            copy.deepcopy(TYPED_ADDED_EFFECT),
            copy.deepcopy(UNTYPED_ADDED_EFFECT),
        ],
        "sources": [{"source_id": "source:synth", "kind": "rule_source", "binding_status": "mapped"}],
        "schedule": {
            "driver": "derived_event_rate",
            "derivation": _derived_derivation(),
            "windows_s": [5.0, 30.0],
            "action_s": 0.0,
            "first_cast_s": 0.0,
        },
        "facts": facts,
        "scope": {},
        "assumptions": [],
        "limitations": [],
        "coverage": {"status": "from_execution", "gap_count": 0, "gaps": []},
        "capabilities": capabilities,
    }
    return planmod.finalize_plan(legacy, schema="le-calculation-plan-v1")


def _build_plan(*, actor: bool) -> dict:
    legacy = _legacy_plan(actor=actor)
    components = [
        {
            "component_id": "hit:declared",
            "kind": "hit",
            "damage_type": "Fire",
            "tags": ["Bow", "Fire", "Minion"],
            "crit_capable": True,
            "adaptive_damage_type": "Fire",
            "target_ref": "hit:declared",
        }
    ]
    target_entries = {
        "hit:declared": {
            "ref": "hit:declared",
            "damage_channel": "fire",
            "resistance": 0.0,
            "penetration": 0.0,
            "damage_reduction": 0.0,
            "strategy": "separate_multiplier",
            "negative_resistance_policy": "allow",
            "unit": "fraction",
            "multiplier": 1.0,
            "resistance_multiplier": 1.0,
            "penetration_multiplier": 1.0,
        }
    }
    evaluations_meta = [{"evaluation_id": "derived_evaluation", "primary": True}]
    execution_v1 = plan_v4._build_execution_v1(
        legacy,
        components,
        target_entries,
        [5.0, 30.0],
        evaluations_meta,
    )
    execution = {
        "engine": "mechanics_static_v1",
        "source_contract": "synthetic.result_gates",
        "strategies": {},
        "execution_v1": execution_v1,
    }
    capabilities = ["hit_components", "presentation"]
    if actor:
        capabilities = [
            "hit_components",
            "presentation",
            "minion_actor",
            "explicit_stat_transfer",
            "derived_event_rate",
        ]
    plan = {
        "schema": "le-calculation-plan-v4",
        "version": "4.0.0",
        "subject": dict(SUBJECT),
        "case_ref": "",
        "scenario": {"states": {}, "target": {"monster_level": 100, "resistance": 0.0}},
        "primary_evaluation": "derived_evaluation",
        "capabilities": capabilities,
        "components": components,
        "applications": [],
        "schedule": {
            "kind": "assembly",
            "horizon_s": 30.0,
            "windows_s": [5.0, 30.0],
            "evaluations": evaluations_meta,
        },
        "target_side": {"unit": "fraction", "strategy": "separate_multiplier", "components": target_entries},
        "presentation": {"title": "SYNTHETIC · result gates"},
        "coverage": {"status": "from_execution", "gap_count": 0, "gaps": []},
        "issues": [],
        "assumptions": [],
        "limitations": [],
        "input_refs": [],
        "assembly": {
            "kind": "synthetic_assembly",
            "legacy_schema": "le-calculation-plan-v1",
            "legacy_plan": legacy,
        },
        "execution": execution,
    }
    plan["execution_sha256"] = plan_v4._sha256_text(plan_v4._canonical(execution))
    plan["scenario_sha256"] = planmod.scenario_sha256(plan["scenario"])
    plan["execution_fingerprint"] = plan_v4._sha256_text(
        plan_v4._canonical(
            {
                "plan": {
                    key: value
                    for key, value in plan.items()
                    if key not in ("plan_sha256", "execution_fingerprint")
                },
                "scenario_sha256": plan["scenario_sha256"],
            }
        )
    )
    plan["plan_sha256"] = plan_v4._sha256_text(
        plan_v4._canonical({key: value for key, value in plan.items() if key != "plan_sha256"})
    )
    return plan


_FIXTURES: dict[str, tuple[dict, dict]] = {}


def fixture(kind: str) -> tuple[dict, dict]:
    """返回 (plan, result)；同一进程内只执行一次。"""
    if kind not in _FIXTURES:
        actor = kind == "actor"
        plan = _build_plan(actor=actor)
        plan_v4.validate_plan_v4(plan)
        result = plan_v4.execute_plan_v4(plan)
        _FIXTURES[kind] = (plan, result)
    plan, result = _FIXTURES[kind]
    return plan, copy.deepcopy(result)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class SyntheticResultGateTest(unittest.TestCase):
    """合成 assembly 结果的正例/反例（内存变异，不写盘）。"""

    def test_normal_result_passes_full_validation_and_stage_checks(self) -> None:
        plan, result = fixture("actor")
        plan_v4.validate_result_v6(plan, result)
        checks = plan_v4.stage_validation_checks(plan, result)
        self.assertTrue(all(ok for ok, _detail in checks.values()), checks)
        self.assertTrue(set(REQUIRED_VALIDATION_CHECKS_V6).issubset(checks))
        # 覆盖缺口投影来自 plan facts，而不是 result 自证。
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertEqual(result["coverage"]["gap_count"], len(COVERAGE_BASE_GAPS))
        # 门禁使用的 known 组件集合来自 plan 声明。
        self.assertIn("hit:declared", plan_v4._assembly_declared_component_refs(plan)["exact"])

    def test_coverage_tamper_complete_zero_empty_rejected(self) -> None:
        plan, _result = fixture("actor")
        for tamper in (
            {"status": "complete", "gap_count": 0, "gaps": []},
            {"status": "partial", "gap_count": 0, "gaps": []},
            {"status": "partial", "gap_count": 1, "gaps": []},
        ):
            with self.subTest(tamper=tamper):
                _plan, result = fixture("actor")
                result["coverage"] = copy.deepcopy(tamper)
                with self.assertRaises(plan_v4.PlanV4Error):
                    plan_v4.validate_result_v6(plan, result)
                checks = plan_v4._assembly_stage_checks(plan, result)
                self.assertFalse(checks["result.coverage"][0], checks["result.coverage"])
                self.assertFalse(checks["result.recompute"][0], checks["result.recompute"])

    def test_applications_ledger_dangling_component_rejected(self) -> None:
        plan, result = fixture("actor")
        self.assertTrue(result["applications_ledger"])
        result["applications_ledger"][0]["component_id"] = "nonexistent:component"
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.validate_result_v6(plan, result)
        checks = plan_v4._assembly_stage_checks(plan, result)
        self.assertFalse(checks["result.applications"][0], checks["result.applications"])
        self.assertFalse(checks["result.recompute"][0], checks["result.recompute"])

    def test_component_damage_total_tamper_rejected(self) -> None:
        plan, result = fixture("actor")
        result["components"][0]["damage_total"] = 999999999.0
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.validate_result_v6(plan, result)
        checks = plan_v4._assembly_stage_checks(plan, result)
        self.assertFalse(checks["result.totals"][0], checks["result.totals"])
        self.assertFalse(checks["result.recompute"][0])

    def test_contribution_value_tamper_rejected(self) -> None:
        plan, result = fixture("actor")
        self.assertTrue(result["contributions"])
        result["contributions"][0]["value"] = 999999999.0
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.validate_result_v6(plan, result)
        checks = plan_v4._assembly_stage_checks(plan, result)
        self.assertFalse(checks["result.applications"][0], checks["result.applications"])
        self.assertFalse(checks["result.recompute"][0])

    def test_totals_and_window_tamper_rejected_by_stage_checks(self) -> None:
        plan, result = fixture("actor")
        totals_tampered = copy.deepcopy(result)
        totals_tampered["totals"]["hit_damage"] = 999999999.0
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.validate_result_v6(plan, totals_tampered)
        self.assertFalse(plan_v4._assembly_stage_checks(plan, totals_tampered)["result.totals"][0])

        window_tampered = copy.deepcopy(result)
        window_tampered["windows"][-1]["dps"] = 1.0
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.validate_result_v6(plan, window_tampered)
        checks = plan_v4._assembly_stage_checks(plan, window_tampered)
        self.assertFalse(checks["result.windows"][0], checks["result.windows"])

    def test_invented_component_id_not_self_certified(self) -> None:
        plan, result = fixture("actor")
        # hit: 前缀的展开只接受声明过的伤害类型；伪造类型不能经 result 自证 known。
        result["components"][0]["component_id"] = "hit:Invented"
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.validate_result_v6(plan, result)

    def test_coverage_issue_text_matches_gap_classification(self) -> None:
        _plan, result = fixture("actor")
        counts = plan_v4._coverage_gap_counts(result["coverage"])
        self.assertEqual(
            counts,
            {"unmapped:damage_relevant_unknown": 1, "unmapped:out_of_scope": 1},
        )
        issue = next(item for item in result["issues"] if str(item).startswith("覆盖状态 partial"))
        self.assertIn(f"{result['coverage']['gap_count']} 项未建模/未验证", issue)
        self.assertIn("out_of_scope 1", issue)
        self.assertIn("未解析伤害相关 1", issue)
        # 不是把所有缺口都写成伤害相关：damage_relevant 只出现一次且计数为 1。
        self.assertEqual(issue.count("未解析伤害相关"), 1)
        self.assertNotIn("out_of_scope 1 / 未解析伤害相关 1 /", issue)


class ActorCallSiteWiringTest(unittest.TestCase):
    """P1-A 调用侧：actor 隔离、非 actor 保持历史默认。"""

    def test_actor_scope_passes_isolation_and_only_typed_channels(self) -> None:
        plan, result = fixture("actor")
        execution = plan["execution"]["execution_v1"]
        self.assertEqual(execution["components"][0]["scope"], "actor")
        component_ids = [item["component_id"] for item in result["components"]]
        # 显式带类型的 added（Fire）与无类型 added（adaptive Fire）都只进入 Fire；
        # 历史口径会把 [Bow, Fire] 行串入全部七个通道。
        self.assertEqual(component_ids, ["hit:Fire"])
        # 结果门禁仍通过（单发组件就是 totals.hit_damage）。
        self.assertAlmostEqual(
            float(result["totals"]["hit_damage"]),
            float(result["components"][0]["damage_total"]),
            delta=1e-9,
        )

    def test_non_actor_scope_keeps_historical_legacy_channels(self) -> None:
        plan, result = fixture("body")
        self.assertEqual(plan["execution"]["execution_v1"]["components"][0]["scope"], "body")
        component_ids = {item["component_id"] for item in result["components"]}
        # 非 actor 路径保持 legacy 默认：公共 tags 中的 Fire 会进入其它通道闭包。
        self.assertEqual(
            component_ids,
            {
                "hit:Physical",
                "hit:Fire",
                "hit:Cold",
                "hit:Lightning",
                "hit:Void",
                "hit:Necrotic",
                "hit:Poison",
            },
        )
        plan_v4.validate_result_v6(plan, result)


class HistoricalRunGateRegressionTest(unittest.TestCase):
    """只读历史 run：真实案例执行接线 + 旧结果门禁回归（不写回）。"""

    def _load_run(self, run: Path) -> tuple[dict, dict]:
        if not run.is_dir():
            self.skipTest(f"缺少历史 run：{run.relative_to(ROOT).as_posix()}")
        return _load_json(run / "calculation-plan.json"), _load_json(run / "result.json")

    def test_real_ballista_actor_execution_uses_isolation(self) -> None:
        if not BALLISTA_RUN.is_dir():
            self.skipTest("缺少 Ballista 历史 run")
        plan, saved = self._load_run(BALLISTA_RUN)
        result = plan_v4.execute_plan_v4(plan, saved["scenario"])
        plan_v4.validate_result_v6(plan, result)
        checks = plan_v4._assembly_stage_checks(plan, result)
        self.assertTrue(all(ok for ok, _detail in checks.values()), checks)
        component_ids = [item["component_id"] for item in result["components"]]
        self.assertIn("hit:Fire", component_ids)
        # P1-A 反例：修复前 Fire 附加伤害会串入 Void/Necrotic/Poison（各 13101.42）。
        for leaked in ("hit:Void", "hit:Necrotic", "hit:Poison"):
            self.assertNotIn(leaked, component_ids)
        self.assertEqual(result["frequency"]["driver"], "derived_event_rate")
        # 覆盖缺口来自 plan facts（32 条），结果与门禁一致。
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertEqual(result["coverage"]["gap_count"], len(result["coverage"]["gaps"]))

    def test_saved_legacy_results_still_pass_gates(self) -> None:
        for name, run in LEGACY_RUNS.items():
            with self.subTest(case=name):
                plan, saved = self._load_run(run)
                plan_v4.validate_result_v6(plan, saved)
                checks = plan_v4._assembly_stage_checks(plan, saved)
                self.assertTrue(all(ok for ok, _detail in checks.values()), checks)


if __name__ == "__main__":
    unittest.main()
