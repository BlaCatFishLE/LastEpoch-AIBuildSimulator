"""Tests for ``le_calc.conditional.recompute`` (conditional Buff input channel).

Contract: ``docs/contracts/calc-channel.md`` sections 2-5.

Coverage:
* baseline口径 equality with the frozen legacy ``result.json`` (single + both
  loop policies, ``include_auto=False``, build-base scenario) within 1e-9;
* per-effect direction on the real frozen build for Craterborn / Infusion /
  Eternal Flames (passive + active) / Throne of Ambition / Chilled Touch /
  Crescendoing Rage / Preparation / layout CDR;
* target-condition overrides (Supermassive / Weakened Reality drop, both-stars
  warning) feeding the ruleset tree-more conditions;
* unsupported effects (Frozen Steel / Conflagrate / Apex) and dedupe rules;
* degraded paths, input immutability, idempotency and determinism.

The loop-heavy variants use a reduced ruleset copy (only the ``eager`` policy,
or no policies at all for single-cast-only assertions) to keep the suite fast;
the baseline-equality test runs the real frozen ruleset with both policies.
All engine inputs are the real frozen snapshot / registry / ruleset.

Run from the repo root::

    $env:PYTHONPATH='tools\\phase3\\src'
    .\\.venv\\Scripts\\python.exe -m unittest tools.phase3.tests.test_conditional_recompute -v
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from le_calc import model
from le_calc.conditional import (
    DEFAULT_REGISTRY_PATH,
    build_catalog,
    channel_support,
    compute_scenario_outputs,
    default_scenario,
    evaluate_scenario,
    load_registry,
    normalize_scenario,
    prepare_scenario,
    recompute_scenario,
    scenario_fingerprint,
    validate_scenario,
)
from le_calc.conditional import constants as C
from le_calc.conditional.recompute import SCHEMA
from le_calc.ruleset import Ruleset, load_ruleset
from le_calc.simulate import run_single_cast

REPO_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_PATH = REPO_ROOT / "examples" / "black-hole" / "snapshot" / "input-snapshot.affix-db.json"
CALC_INPUT_PATH = REPO_ROOT / "examples" / "black-hole" / "inputs" / "calculation-input.affix-db.json"
BUILD_PATH = REPO_ROOT / "examples" / "black-hole" / "inputs" / "bd.json"
RESULT_PATH = REPO_ROOT / "reports" / "black-hole" / "save-build-bh-affix-db" / "result.json"
RULESET_PATH = REPO_ROOT / "tools" / "phase3" / "rules" / "ruleset-bh-stage1-v4.json"

REQUIRED_REAL_FILES = (
    DEFAULT_REGISTRY_PATH,
    SNAPSHOT_PATH,
    CALC_INPUT_PATH,
    RESULT_PATH,
    RULESET_PATH,
)

#: absolute tolerance required by the contract (section 3.7)
BASELINE_TOL = 1e-9


# ---------------------------------------------------------------------------
# Lazily loaded real assets
# ---------------------------------------------------------------------------

_CACHE: dict[str, object] = {}



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def require_real_assets(testcase: unittest.TestCase) -> None:
    missing = [
        str(path.relative_to(REPO_ROOT)) for path in REQUIRED_REAL_FILES if not path.is_file()
    ]
    if missing:
        testcase.skipTest("required real build assets are absent: " + ", ".join(missing))


def real_snapshot() -> model.Snapshot:
    if "snapshot" not in _CACHE:
        _CACHE["snapshot"] = model.Snapshot.from_dict(_load_json(SNAPSHOT_PATH))
    return _CACHE["snapshot"]  # type: ignore[return-value]


def real_registry():
    if "registry" not in _CACHE:
        _CACHE["registry"] = load_registry(DEFAULT_REGISTRY_PATH)
    return _CACHE["registry"]


def real_ruleset() -> Ruleset:
    if "ruleset" not in _CACHE:
        _CACHE["ruleset"] = load_ruleset(RULESET_PATH)
    return _CACHE["ruleset"]  # type: ignore[return-value]


def legacy_result() -> dict:
    if "legacy" not in _CACHE:
        _CACHE["legacy"] = _load_json(RESULT_PATH)
    return _CACHE["legacy"]  # type: ignore[return-value]


def real_catalog() -> dict:
    if "catalog" not in _CACHE:
        _CACHE["catalog"] = build_catalog(
            real_snapshot(),
            registry=real_registry(),
            calc_input=_load_json(CALC_INPUT_PATH),
            build=_load_json(BUILD_PATH),
            dps_scenario=legacy_result()["scenario"],
        )
    return _CACHE["catalog"]  # type: ignore[return-value]


def base_scenario() -> dict:
    if "base_scenario" not in _CACHE:
        _CACHE["base_scenario"] = default_scenario(
            real_registry(), {"binding": real_catalog().get("binding") or {}}
        )
    return _CACHE["base_scenario"]  # type: ignore[return-value]


def _subset_ruleset(policy_ids: tuple[str, ...]) -> Ruleset:
    key = "ruleset:" + ",".join(policy_ids)
    if key not in _CACHE:
        ruleset = real_ruleset()
        data = copy.deepcopy(ruleset.data)
        data["action_policies"] = [
            policy
            for policy in data.get("action_policies") or []
            if str(policy.get("policy_id")) in policy_ids
        ]
        _CACHE[key] = Ruleset(data=data, path=ruleset.path, sha256=ruleset.sha256)
    return _CACHE[key]  # type: ignore[return-value]


def single_cast_ruleset() -> Ruleset:
    """Ruleset copy without loop policies (fast single-cast-only assertions)."""
    return _subset_ruleset(())


def eager_loop_ruleset() -> Ruleset:
    """Ruleset copy with only the ``eager`` policy (fast loop assertions)."""
    return _subset_ruleset(("eager",))


# ---------------------------------------------------------------------------
# Recompute helpers
# ---------------------------------------------------------------------------


def _cache_key(
    ruleset: Ruleset,
    *,
    include_auto: bool,
    monster_level,
    evaluation,
    scenario: dict,
) -> tuple:
    evaluation_key = C.canonical_json(evaluation) if isinstance(evaluation, dict) else "-"
    return (
        id(ruleset),
        include_auto,
        monster_level,
        evaluation_key,
        C.canonical_json(scenario),
    )


def recompute(
    mutate=None,
    *,
    ruleset: Ruleset | None = None,
    include_auto: bool = False,
    scenario: dict | None = None,
    evaluation: dict | None = None,
    monster_level=None,
) -> dict:
    """Cached recompute for one scenario variant (real asset inputs)."""
    active_ruleset = ruleset or single_cast_ruleset()
    if scenario is None:
        scenario = copy.deepcopy(base_scenario())
        if mutate is not None:
            mutate(scenario)
    key = _cache_key(
        active_ruleset,
        include_auto=include_auto,
        monster_level=monster_level,
        evaluation=evaluation,
        scenario=scenario,
    )
    if key not in _CACHE:
        _CACHE[key] = recompute_scenario(
            real_snapshot(),
            active_ruleset,
            scenario=scenario,
            registry=real_registry(),
            catalog=real_catalog(),
            evaluation=evaluation,
            monster_level=monster_level,
            include_auto=include_auto,
        )
    return _CACHE[key]  # type: ignore[return-value]


def selected_effect(effect_id: str):
    def mutate(scenario: dict) -> None:
        scenario["effects"][effect_id] = {"enabled": True}

    return mutate


def _effect_ids(recompute_result: dict, key: str = "applied") -> set[str]:
    return {
        str(entry.get("effect_id"))
        for entry in recompute_result["condition_application"][key]
    }


def _skipped_pairs(recompute_result: dict) -> set[tuple[str, str]]:
    return {
        (str(entry.get("effect_id")), str(entry.get("reason")))
        for entry in recompute_result["condition_application"]["skipped"]
    }


def _applied(recompute_result: dict, effect_id: str, *, operation: str | None = None):
    rows = [
        entry
        for entry in recompute_result["condition_application"]["applied"]
        if entry.get("effect_id") == effect_id
        and (operation is None or entry.get("operation") == operation)
    ]
    if not rows:
        raise AssertionError(f"no applied contribution for {effect_id!r}")
    return rows


def _single(result: dict, key: str) -> float:
    return float(result["scenario"]["single"][key])


def _base_single(result: dict, key: str) -> float:
    return float(result["baseline"]["single"][key])


# ---------------------------------------------------------------------------
# Baseline口径
# ---------------------------------------------------------------------------


class RecomputeBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_real_assets(cls)
        # Current official ruleset (v4, P1 ruleset cleanup). The frozen legacy
        # result.json pinned the v3-era numbers; value comparisons against it
        # remain valid, but its ruleset sha256 is historical metadata.
        cls.out = recompute(include_auto=False, ruleset=real_ruleset())

    def test_status_metadata_and_shape(self) -> None:
        out = self.out
        self.assertEqual(out["schema"], SCHEMA)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["ruleset"]["id"], "bh-stage1-v4")
        # P1 ruleset cleanup: the current official ruleset is v4, so the
        # recompute metadata must match the loaded current ruleset file. The
        # frozen legacy result still carries the historical v3 sha256 and is
        # compared by value only (see `legacy_result`).
        self.assertEqual(out["ruleset"]["sha256"], real_ruleset().sha256)
        self.assertEqual(out["monster_level"], 100)
        self.assertTrue(out["dps_recomputed"])
        self.assertEqual(len(out["scenario_fingerprint"]), 64)
        for key in (
            "baseline",
            "scenario",
            "delta",
            "condition_application",
            "warnings",
            "assumptions",
        ):
            self.assertIn(key, out)
        self.assertNotIn(":\\", json.dumps(out, ensure_ascii=False))

    def test_baseline_single_equals_legacy_within_1e9(self) -> None:
        single = self.out["baseline"]["single"]
        legacy = legacy_result()["single_cast"]["totals"]
        for key in (
            "single_cast_total",
            "body_total",
            "ignite_total",
            "ignite_tail_damage",
            "expected_total_stacks_applied",
            "observation_end_s",
        ):
            with self.subTest(key=key):
                self.assertLessEqual(
                    abs(float(single[key]) - float(legacy[key])), BASELINE_TOL
                )
        self.assertLessEqual(
            abs(
                float(single["cast_time_s"])
                - float(legacy_result()["cast"]["cast_time_s"])
            ),
            BASELINE_TOL,
        )

    def test_baseline_loops_equal_legacy_within_1e9(self) -> None:
        baseline_loops = self.out["baseline"]["loops"]
        legacy_loops = legacy_result()["loops"]
        self.assertEqual(
            [loop["policy_id"] for loop in baseline_loops],
            [loop["policy_id"] for loop in legacy_loops],
        )
        for loop, legacy_loop in zip(baseline_loops, legacy_loops):
            self.assertEqual(loop["cast_count"], legacy_loop["totals"]["cast_count"])
            with self.subTest(policy=loop["policy_id"], metric="cycle_s"):
                self.assertLessEqual(
                    abs(float(loop["cycle_s"]) - float(legacy_loop["totals"]["cycle_s"])),
                    BASELINE_TOL,
                )
            for window, legacy_window in zip(loop["windows"], legacy_loop["windows"]):
                with self.subTest(
                    policy=loop["policy_id"], window=window["window_s"], metric="dps"
                ):
                    self.assertLessEqual(
                        abs(float(window["dps"]) - float(legacy_window["dps"])),
                        BASELINE_TOL,
                    )
                    self.assertLessEqual(
                        abs(
                            float(window["total_damage"])
                            - float(legacy_window["total_damage"])
                        ),
                        BASELINE_TOL,
                    )

    def test_scenario_equals_baseline_when_nothing_selected(self) -> None:
        self.assertEqual(self.out["scenario"], self.out["baseline"])
        self.assertEqual(self.out["delta"]["single_cast_total"], 0.0)
        self.assertEqual(self.out["condition_application"]["applied"], [])
        self.assertIn(
            ("enchant_weapon.eternal_flames.passive", "auto_excluded"),
            _skipped_pairs(self.out),
        )

    def test_monster_level_override_scales_baseline(self) -> None:
        level0 = recompute(include_auto=False, monster_level=0)
        self.assertEqual(level0["monster_level"], 0)
        self.assertEqual(level0["status"], "ok")
        ratio = level0["baseline"]["single"]["single_cast_total"] / self.out["baseline"]["single"]["single_cast_total"]
        self.assertAlmostEqual(ratio, 1.0 / 0.13, places=9)

    def test_explicit_evaluation_matches_internal_evaluation(self) -> None:
        scenario = copy.deepcopy(base_scenario())

        def mutate(scn: dict) -> None:
            scn["effects"]["meteor.craterborn"] = {"enabled": True}

        mutate(scenario)
        evaluation = evaluate_scenario(scenario, real_registry(), real_catalog())
        via_evaluation = recompute(
            scenario=copy.deepcopy(scenario), evaluation=evaluation
        )
        via_internal = recompute(scenario=copy.deepcopy(scenario))
        self.assertEqual(
            json.dumps(via_evaluation, sort_keys=True),
            json.dumps(via_internal, sort_keys=True),
        )
        self.assertTrue(via_evaluation["condition_application"]["proposals_used"])


# ---------------------------------------------------------------------------
# Per-effect direction (real frozen build)
# ---------------------------------------------------------------------------


class RecomputeEffectDirectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_real_assets(cls)
        cls.base = recompute(include_auto=False)

    def test_auto_effects_default_on(self) -> None:
        default_auto = recompute(include_auto=True)
        self.assertTrue(default_auto["condition_application"]["auto_included"])
        # Eternal Flames passive (+25% ignite duration) is auto-active.
        self.assertIn(
            "enchant_weapon.eternal_flames.passive",
            _effect_ids(default_auto),
        )
        self.assertGreater(
            _single(default_auto, "ignite_total"), _base_single(default_auto, "ignite_total")
        )
        self.assertAlmostEqual(
            _single(default_auto, "body_total"), _base_single(default_auto, "body_total")
        )
        self.assertGreater(default_auto["delta"]["ignite_total"], 0.0)

    def test_craterborn_penetration_and_cast_speed(self) -> None:
        out = recompute(selected_effect("meteor.craterborn"))
        self.assertGreater(_single(out, "body_total"), _base_single(out, "body_total"))
        self.assertGreater(_single(out, "ignite_total"), _base_single(out, "ignite_total"))
        self.assertLess(_single(out, "cast_time_s"), _base_single(out, "cast_time_s"))
        pen = _applied(out, "meteor.craterborn", operation="added")[0]
        self.assertEqual(pen["engine_stat_key"], "penetration")
        self.assertEqual(pen["target"], "fire")
        self.assertEqual(pen["damage_tags"], ["Fire"])
        cast = _applied(out, "meteor.craterborn", operation="increased")[0]
        self.assertEqual(cast["engine_stat_key"], "cast_speed")

    def test_infusion_fire_increased(self) -> None:
        out = recompute(selected_effect("flame_ward.infusion"))
        self.assertGreater(_single(out, "body_total"), _base_single(out, "body_total"))
        self.assertGreater(_single(out, "ignite_total"), _base_single(out, "ignite_total"))
        row = _applied(out, "flame_ward.infusion")[0]
        self.assertEqual(row["engine_stat_key"], "damage")
        self.assertEqual(row["operation"], "increased")
        self.assertEqual(row["value"], 2.5)
        self.assertEqual(row["damage_tags"], ["Fire"])
        # Fire-scoped: the cold body component is excluded by the engine.
        from le_calc.damage import BODY_COLD_TAGS, tags_match

        self.assertFalse(tags_match(row["damage_tags"], BODY_COLD_TAGS))

    def test_eternal_flames_active_ignite_duration(self) -> None:
        out = recompute(selected_effect("enchant_weapon.eternal_flames.active"))
        self.assertGreater(_single(out, "ignite_total"), _base_single(out, "ignite_total"))
        self.assertGreater(
            _single(out, "ignite_tail_damage"), _base_single(out, "ignite_tail_damage")
        )
        self.assertAlmostEqual(
            _single(out, "body_total"), _base_single(out, "body_total")
        )
        row = _applied(out, "enchant_weapon.eternal_flames.active")[0]
        self.assertEqual(row["engine_stat_key"], "ailment_duration")
        self.assertEqual(row["target"], "ignite")
        self.assertEqual(row["value"], 0.25)
        # dedupe reference (passive modifier) is not an active baseline row:
        # the incremental +25% is applied and documented as an assumption.
        self.assertIn("dedupe", row["assumption_note"])

    def test_eternal_flames_passive_plus_active_stacks(self) -> None:
        active_only = recompute(
            selected_effect("enchant_weapon.eternal_flames.active"), include_auto=False
        )
        with_passive = recompute(
            selected_effect("enchant_weapon.eternal_flames.active"), include_auto=True
        )
        self.assertGreater(
            _single(with_passive, "ignite_total"), _single(active_only, "ignite_total")
        )
        self.assertIn(
            "enchant_weapon.eternal_flames.passive", _effect_ids(with_passive)
        )

    def test_throne_of_ambition_20_stacks(self) -> None:
        def mutate(scenario: dict) -> None:
            scenario["effects"]["idol.throne_of_ambition"] = {"stacks": 20}

        out = recompute(mutate)
        self.assertGreater(_single(out, "body_total"), _base_single(out, "body_total"))
        self.assertGreater(_single(out, "ignite_total"), _base_single(out, "ignite_total"))
        rows = _applied(out, "idol.throne_of_ambition", operation="more")
        self.assertEqual(len(rows), 2)
        self.assertEqual({tuple(row["damage_tags"]) for row in rows}, {("Fire",), ("Cold",)})
        for row in rows:
            self.assertEqual(row["value"], 0.4)  # 0.02 x 20

    def test_chilled_touch_untagged_more_hits_body_and_ignite(self) -> None:
        def mutate(scenario: dict) -> None:
            scenario["states"]["target.chilled"] = True
            scenario["effects"]["frost_wall.chilled_touch"] = {"enabled": True}

        out = recompute(mutate)
        self.assertGreater(_single(out, "body_total"), _base_single(out, "body_total"))
        # tags [] (all three engine components) -> ignite rises as well; this is
        # the implementation fixed by the branch->tags rule of contract §3.
        self.assertGreater(_single(out, "ignite_total"), _base_single(out, "ignite_total"))
        row = _applied(out, "frost_wall.chilled_touch")[0]
        self.assertEqual(row["operation"], "more")
        self.assertEqual(row["value"], 0.14)
        self.assertEqual(row["damage_tags"], [])
        self.assertEqual(
            set(row["branches"]), {"body.fire", "body.cold", "ignite"}
        )

    def test_preparation_single_cast_only(self) -> None:
        def mutate(scenario: dict) -> None:
            scenario["single_cast"]["preparation_ready"] = True

        out = recompute(mutate)
        self.assertGreater(_single(out, "body_total"), _base_single(out, "body_total"))
        # Spell tag cannot match Ignite (DoT/Ailment only); exact equality.
        self.assertEqual(_single(out, "ignite_total"), _base_single(out, "ignite_total"))
        row = _applied(out, "passive.preparation")[0]
        self.assertEqual(row["scope"], "single_cast")
        self.assertEqual(row["damage_tags"], ["Elemental", "Spell"])
        self.assertIn(
            ("passive.preparation", "cast_state_single_cast_only"),
            _skipped_pairs(out),
        )

    def test_crescendoing_rage_single_cast_only(self) -> None:
        base = recompute(ruleset=eager_loop_ruleset(), include_auto=False)

        def mutate(scenario: dict) -> None:
            scenario["single_cast"]["crescendoing_rage_applies"] = True

        out = recompute(mutate, ruleset=eager_loop_ruleset())
        self.assertGreater(_single(out, "single_cast_total"), _base_single(out, "single_cast_total"))
        row = _applied(out, "ring.crescendoing_rage")[0]
        self.assertEqual(row["scope"], "single_cast")
        self.assertEqual(row["operation"], "more")
        self.assertEqual(row["value"], 0.5)
        # The loop must not move: CastState needs per-cast phase simulation.
        self.assertEqual(out["baseline"]["loops"], base["baseline"]["loops"])
        self.assertEqual(out["scenario"]["loops"], base["baseline"]["loops"])
        for loop_delta in out["delta"]["loops"]:
            self.assertEqual(loop_delta["dps"], 0.0)
        self.assertIn(
            ("ring.crescendoing_rage", "needs_action_or_time_simulation"),
            _skipped_pairs(out),
        )

    def test_layout_cdr_changes_loop_dps(self) -> None:
        base = recompute(ruleset=eager_loop_ruleset(), include_auto=False)
        out = recompute(
            selected_effect("idol_altar.layout_cdr"), ruleset=eager_loop_ruleset()
        )
        row = _applied(out, "idol_altar.layout_cdr")[0]
        self.assertEqual(row["engine_stat_key"], "cooldown_recovery_speed")
        self.assertEqual(row["value"], 0.1)
        self.assertEqual(_single(out, "single_cast_total"), _base_single(out, "single_cast_total"))
        self.assertGreater(len(out["delta"]["loops"]), 0)
        self.assertGreater(out["delta"]["loops"][0]["dps"], 0.0)
        self.assertGreater(
            out["scenario"]["loops"][0]["windows"][-1]["dps"],
            base["baseline"]["loops"][0]["windows"][-1]["dps"],
        )


# ---------------------------------------------------------------------------
# Target-condition overrides feed ruleset tree-more conditions
# ---------------------------------------------------------------------------


class RecomputeTargetConditionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_real_assets(cls)
        cls.base = recompute(include_auto=False)

    def test_center_unchecked_drops_weakened_reality(self) -> None:
        def mutate(scenario: dict) -> None:
            scenario["states"]["target.within_center_1m"] = False

        out = recompute(mutate)
        self.assertFalse(
            out["condition_application"]["target_conditions"]["target_within_center_1m"]
        )
        self.assertLess(_single(out, "body_total"), _base_single(out, "body_total"))
        # Weakened Reality applies to body only; Ignite is untouched.
        self.assertEqual(_single(out, "ignite_total"), _base_single(out, "ignite_total"))
        ratio = _single(out, "body_total") / _base_single(out, "body_total")
        self.assertAlmostEqual(ratio, 1.0 / 1.6, places=9)  # node 27 = +60% more

    def test_boss_unchecked_drops_supermassive(self) -> None:
        def mutate(scenario: dict) -> None:
            scenario["target"] = {
                "monster_level": 100,
                "fire_resistance": 0.0,
                "boss": False,
                "rare": False,
            }

        out = recompute(mutate)
        self.assertFalse(
            out["condition_application"]["target_conditions"]["target_is_boss_or_rare"]
        )
        # R1: the baseline environment uses the same target parameters, so the
        # legacy baseline loses Supermassive exactly like the scenario.
        self.assertAlmostEqual(
            out["baseline"]["single"]["body_total"]
            / self.base["baseline"]["single"]["body_total"],
            0.5,
            places=9,
        )
        self.assertAlmostEqual(
            _single(out, "body_total")
            / self.base["baseline"]["single"]["body_total"],
            0.5,
            places=9,
        )
        self.assertAlmostEqual(
            _single(out, "ignite_total")
            / self.base["baseline"]["single"]["ignite_total"],
            0.5,
            places=9,
        )
        self.assertEqual(out["delta"]["single_cast_total"], 0.0)

    def test_legacy_boss_state_bridges_to_target_block(self) -> None:
        def mutate(scenario: dict) -> None:
            scenario["states"]["target.boss_or_rare"] = False

        out = recompute(mutate)
        self.assertEqual(out["target"]["boss"], False)
        self.assertEqual(out["target"]["rare"], False)
        self.assertFalse(
            out["condition_application"]["target_conditions"]["target_is_boss_or_rare"]
        )
        self.assertAlmostEqual(
            out["baseline"]["single"]["body_total"]
            / self.base["baseline"]["single"]["body_total"],
            0.5,
            places=9,
        )

    def test_both_stars_unchecked_warns_without_fake_change(self) -> None:
        def mutate(scenario: dict) -> None:
            scenario["states"]["target.both_stars_cover"] = False

        out = recompute(mutate)
        self.assertFalse(
            out["condition_application"]["target_conditions"]["both_stars_cover"]
        )
        self.assertTrue(
            any("both_stars_cover" in warning for warning in out["warnings"])
        )
        self.assertEqual(out["scenario"], out["baseline"])
        self.assertEqual(out["delta"]["single_cast_total"], 0.0)

    def test_target_conditions_default_true(self) -> None:
        self.assertEqual(
            self.base["condition_application"]["target_conditions"],
            {
                "target_is_boss_or_rare": True,
                "target_within_center_1m": True,
                "both_stars_cover": True,
            },
        )


# ---------------------------------------------------------------------------
# Skipped / dedupe / unsupported
# ---------------------------------------------------------------------------


class RecomputeSkippedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_real_assets(cls)
        cls.base = recompute(include_auto=False)

    def test_unsupported_effects_skipped_and_dps_unchanged(self) -> None:
        # Apex is now a *supported* glyph channel when selected, so this test
        # covers the remaining unsupported mechanisms only.
        def mutate(scenario: dict) -> None:
            scenario["states"]["target.chilled"] = True
            scenario["states"]["target.frozen"] = True

        out = recompute(mutate)
        self.assertEqual(out["scenario"], out["baseline"])
        self.assertEqual(out["condition_application"]["applied"], [])
        pairs = _skipped_pairs(out)
        self.assertIn(("passive.frozen_steel", "mechanism_unresolved"), pairs)
        self.assertIn(
            ("enchant_weapon.conflagrate", "needs_action_or_time_simulation"), pairs
        )
        for effect_id in (
            "passive.frozen_steel",
            "enchant_weapon.conflagrate",
        ):
            self.assertNotIn(effect_id, _effect_ids(out))

    def test_dedupe_already_active_is_skipped(self) -> None:
        active_ids = [c.contribution_id for c in real_snapshot().active_contributions()]
        self.assertTrue(active_ids)
        evaluation = {
            "proposed_contributions": [
                {
                    "proposal_id": "prop:synthetic.dupe",
                    "effect_id": "synthetic.dupe",
                    "modifier_id": "synthetic.dupe.more",
                    "stat_key": "damage",
                    "stat_name": "synthetic more",
                    "operation": "more",
                    "value": 0.5,
                    "unit": "fraction",
                    "target": None,
                    "damage_tags": [],
                    "branches": ["body.fire", "ignite"],
                    "condition": "",
                    "requires": "needs_rule_mapping",
                    "dedupe_contribution_ids": [active_ids[0]],
                    "status": "proposed",
                    "effect_status": "selected_ready_needs_rules",
                }
            ]
        }
        out = recompute(evaluation=evaluation)
        self.assertEqual(out["scenario"], out["baseline"])
        self.assertIn(("synthetic.dupe", "dedupe_already_active"), _skipped_pairs(out))
        self.assertEqual(out["condition_application"]["applied"], [])

    def test_malformed_proposal_degrades_without_raising(self) -> None:
        evaluation = {
            "proposed_contributions": [
                {"effect_id": "broken", "modifier_id": "", "stat_key": "damage"},
                "not-a-dict",
            ]
        }
        out = recompute(evaluation=evaluation)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["condition_application"]["applied"], [])
        self.assertIn(("broken", "invalid_proposal"), _skipped_pairs(out))

    def test_no_absolute_paths_or_timestamps(self) -> None:
        text = json.dumps(self.base, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(":\\", text)
        self.assertNotIn(str(REPO_ROOT), text)
        self.assertNotIn("generated_at", text)


class RecomputeChannelSupportTests(unittest.TestCase):
    """The selection-independent support matrix used by the conditions page."""

    @classmethod
    def setUpClass(cls) -> None:
        require_real_assets(cls)
        cls.support = channel_support(real_registry())
        cls.base = recompute(include_auto=False)

    def test_covers_every_registered_effect(self) -> None:
        registry_ids = {effect["effect_id"] for effect in real_registry().effects}
        self.assertEqual(set(self.support), registry_ids)
        self.assertEqual(len(self.support), 17)
        for effect_id, entry in self.support.items():
            self.assertEqual(entry["effect_id"], effect_id)
            self.assertIn(entry["state"], ("supported", "skipped", "baseline"))
            self.assertTrue(entry["reason"])
            self.assertTrue(entry["reason_detail"])
            self.assertIsInstance(entry["modifiers"], list)

    def test_expected_state_split(self) -> None:
        by_state: dict[str, set[str]] = {}
        for effect_id, entry in self.support.items():
            by_state.setdefault(entry["state"], set()).add(effect_id)
        self.assertEqual(
            by_state["supported"],
            {
                "enchant_weapon.eternal_flames.active",
                "enchant_weapon.eternal_flames.passive",
                "equipment.apex_glyph_conversion",
                "flame_ward.infusion",
                "frost_wall.chilled_touch",
                "idol.throne_of_ambition",
                "idol_altar.layout_cdr",
                "meteor.craterborn",
                "passive.preparation",
                "ring.crescendoing_rage",
            },
        )
        self.assertEqual(
            by_state["baseline"],
            {
                "idol.fire_damage_mana_threshold",
                "idol.fire_damage_mana_threshold.idols_3",
                "main_skill.supermassive",
                "main_skill.weakened_reality",
            },
        )
        self.assertEqual(
            by_state["skipped"],
            {
                "enchant_weapon.conflagrate",
                "flame_ward.fire_aura",
                "passive.frozen_steel",
            },
        )

    def test_skipped_reasons_are_machine_readable(self) -> None:
        expected = {
            "enchant_weapon.conflagrate": "needs_action_or_time_simulation",
            "equipment.apex_glyph_conversion": "glyph_conversion",
            "flame_ward.fire_aura": "no_damage_modifiers",
            "passive.frozen_steel": "mechanism_unresolved",
        }
        for effect_id, reason in expected.items():
            with self.subTest(effect_id=effect_id):
                self.assertEqual(self.support[effect_id]["reason"], reason)

    def test_apex_glyph_channel_metadata(self) -> None:
        entry = self.support["equipment.apex_glyph_conversion"]
        self.assertEqual(entry["state"], "supported")
        self.assertEqual(entry["reason"], "glyph_conversion")
        self.assertEqual(entry["scope"], "both")
        modifier = next(
            item
            for item in entry["modifiers"]
            if item["modifier_id"] == "equipment.apex_glyph_conversion.ignite_to_fire_shred"
        )
        self.assertTrue(modifier["supported"])
        self.assertEqual(modifier["engine_stat_key"], "resistance_shred")
        self.assertEqual(modifier["scope"], "both")

    def test_cast_state_effects_are_single_cast_only(self) -> None:
        for effect_id in ("ring.crescendoing_rage", "passive.preparation"):
            entry = self.support[effect_id]
            self.assertEqual(entry["state"], "supported")
            self.assertEqual(entry["scope"], "single_cast")
            for modifier in entry["modifiers"]:
                self.assertEqual(modifier["scope"], "single_cast")

    def test_recompute_output_embeds_the_matrix(self) -> None:
        self.assertEqual(
            json.dumps(self.base["channel_support"], sort_keys=True),
            json.dumps(self.support, sort_keys=True),
        )
        # Selection-independent: a selected scenario carries the same matrix.
        selected = recompute(selected_effect("meteor.craterborn"))
        self.assertEqual(
            json.dumps(selected["channel_support"], sort_keys=True),
            json.dumps(self.support, sort_keys=True),
        )

    def test_unknown_registry_yields_empty_matrix(self) -> None:
        self.assertEqual(channel_support(None), {})
        out = recompute_scenario(
            real_snapshot(),
            single_cast_ruleset(),
            scenario=copy.deepcopy(base_scenario()),
            registry=None,  # type: ignore[arg-type]
            include_auto=False,
        )
        self.assertEqual(out["channel_support"], {})


# ---------------------------------------------------------------------------
# prepare_scenario (reusable engine environment for the full-report builder)
# ---------------------------------------------------------------------------


def prepare_for(
    mutate=None,
    *,
    ruleset: Ruleset | None = None,
    include_auto: bool = False,
    scenario: dict | None = None,
) -> dict:
    if scenario is None:
        scenario = copy.deepcopy(base_scenario())
        if mutate is not None:
            mutate(scenario)
    return prepare_scenario(
        real_snapshot(),
        ruleset or single_cast_ruleset(),
        scenario=scenario,
        registry=real_registry(),
        catalog=real_catalog(),
        include_auto=include_auto,
    )


def glyph_on(mutate=None):
    def apply(scenario: dict) -> None:
        scenario["effects"]["equipment.apex_glyph_conversion"] = {"enabled": True}
        if mutate is not None:
            mutate(scenario)

    return apply


def target_boss_off(scenario: dict) -> None:
    scenario["states"]["target.boss_or_rare"] = False


class PrepareScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_real_assets(cls)

    def test_returns_deep_copied_engine_environment(self) -> None:
        prepared = prepare_for(selected_effect("meteor.craterborn"))
        for key in (
            "snapshot",
            "snapshot_loops",
            "snapshot_single_cast",
            "baseline_snapshot",
            "ruleset",
            "baseline_ruleset",
            "engine_scenario",
            "baseline_scenario",
            "applied",
            "skipped",
            "warnings",
            "assumptions",
            "condition_application",
            "channel_support",
            "glyph_model",
        ):
            self.assertIn(key, prepared)
        self.assertIsInstance(prepared["snapshot"], model.Snapshot)
        self.assertIsInstance(prepared["ruleset"], Ruleset)
        self.assertIsInstance(prepared["engine_scenario"], dict)
        self.assertIsNot(prepared["snapshot"], real_snapshot())
        self.assertIsNot(prepared["ruleset"], real_ruleset())
        self.assertEqual(
            len(prepared["baseline_snapshot"].contributions),
            len(real_snapshot().contributions),
        )
        self.assertGreater(
            len(prepared["snapshot"].contributions),
            len(real_snapshot().contributions),
        )
        self.assertEqual(prepared["status"], "ok")
        self.assertEqual(prepared["ruleset_info"]["id"], "bh-stage1-v4")
        self.assertIn("meteor.craterborn", {e["effect_id"] for e in prepared["applied"]})

    def test_does_not_mutate_inputs(self) -> None:
        snapshot = real_snapshot()
        ruleset = real_ruleset()
        scenario = copy.deepcopy(base_scenario())
        scenario["effects"]["equipment.apex_glyph_conversion"] = {"enabled": True}
        before_snapshot = copy.deepcopy(snapshot.to_dict())
        before_ruleset = copy.deepcopy(ruleset.data)
        before_scenario = copy.deepcopy(scenario)
        prepare_scenario(
            snapshot,
            ruleset,
            scenario=scenario,
            registry=real_registry(),
            catalog=real_catalog(),
            include_auto=True,
        )
        self.assertEqual(snapshot.to_dict(), before_snapshot)
        self.assertEqual(ruleset.data, before_ruleset)
        self.assertEqual(scenario, before_scenario)

    def test_deterministic(self) -> None:
        first = prepare_for(selected_effect("meteor.craterborn"))
        second = prepare_for(selected_effect("meteor.craterborn"))
        for key in (
            "applied",
            "skipped",
            "warnings",
            "assumptions",
            "condition_application",
            "channel_support",
            "glyph_model",
            "engine_scenario",
            "baseline_scenario",
            "target_conditions",
            "monster_level",
            "scenario_fingerprint",
        ):
            self.assertEqual(
                json.dumps(first[key], sort_keys=True, ensure_ascii=False),
                json.dumps(second[key], sort_keys=True, ensure_ascii=False),
                key,
            )
        self.assertEqual(first["snapshot"].to_dict(), second["snapshot"].to_dict())
        self.assertEqual(first["ruleset"].data, second["ruleset"].data)
        self.assertEqual(first["status"], second["status"])

    def test_engine_environment_matches_recompute(self) -> None:
        prepared = prepare_for(selected_effect("meteor.craterborn"))
        out = recompute(selected_effect("meteor.craterborn"))
        scenario_single = run_single_cast(
            prepared["snapshot_single_cast"],
            prepared["ruleset"],
            prepared["engine_scenario"],
        )
        baseline_single = run_single_cast(
            prepared["baseline_snapshot"],
            prepared["baseline_ruleset"],
            prepared["baseline_scenario"],
        )
        self.assertAlmostEqual(
            scenario_single["totals"]["single_cast_total"],
            out["scenario"]["single"]["single_cast_total"],
            places=9,
        )
        self.assertAlmostEqual(
            baseline_single["totals"]["single_cast_total"],
            out["baseline"]["single"]["single_cast_total"],
            places=9,
        )

    def test_glyph_engine_inputs(self) -> None:
        prepared = prepare_for(glyph_on())
        self.assertTrue(prepared["glyph_model"]["enabled"])
        heat_wave = prepared["ruleset"].ignite["heat_wave"]
        self.assertEqual(heat_wave["points"], 0)
        self.assertEqual(heat_wave["chance_per_point_per_uncapped_fire_res"], 0.0)
        self.assertEqual(
            prepared["engine_scenario"]["target"]["resistances"]["fire"], -0.2
        )
        self.assertEqual(
            prepared["engine_scenario"]["target"]["fire_resistance_shred"], 0.2
        )
        self.assertEqual(prepared["baseline_ruleset"].ignite["heat_wave"]["points"], 5)
        self.assertEqual(
            prepared["baseline_scenario"]["target"]["resistances"]["fire"], 0.0
        )
        self.assertIn(
            "equipment.apex_glyph_conversion",
            {entry["effect_id"] for entry in prepared["applied"]},
        )


def _apex_mechanism_proposal() -> dict:
    return {
        "proposal_id": "prop:equipment.apex_glyph_conversion.ignite_to_fire_shred",
        "effect_id": "equipment.apex_glyph_conversion",
        "modifier_id": "equipment.apex_glyph_conversion.ignite_to_fire_shred",
        "stat_key": "ailment_chance_conversion",
        "stat_name": "点燃几率→火焰抗性削减几率",
        "operation": "conversion",
        "value": 1.0,
        "unit": "fraction",
        "target": "fire_resistance_shred",
        "damage_tags": ["Fire"],
        "branches": ["ignite.application"],
        "condition": "player.standing_on_glyph_of_dominion",
        "requires": "mechanism_unresolved",
        "dedupe_contribution_ids": [],
        "status": "proposed",
        "effect_status": "selected_unconfirmed",
    }


# ---------------------------------------------------------------------------
# Apex of Thought + Glyph of Dominion conversion
# ---------------------------------------------------------------------------


class RecomputeGlyphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_real_assets(cls)
        cls.base = recompute(include_auto=False)
        cls.glyph_boss = recompute(glyph_on())
        cls.nonboss_plain = recompute(target_boss_off)
        cls.nonboss_glyph = recompute(glyph_on(target_boss_off))

    def test_glyph_off_keeps_apex_skipped(self) -> None:
        self.assertFalse(self.base["glyph_model"]["enabled"])
        self.assertEqual(self.base["glyph_model"]["reason"], "apex_not_selected")
        self.assertGreater(self.base["scenario"]["single"]["ignite_total"], 0.0)
        self.assertNotIn(
            "equipment.apex_glyph_conversion",
            _effect_ids(self.base),
        )
        # An externally supplied Apex proposal (glyph off) still degrades to the
        # historical mechanism_unresolved skip.
        out = recompute(
            scenario=copy.deepcopy(base_scenario()),
            evaluation={"proposed_contributions": [_apex_mechanism_proposal()]},
        )
        self.assertEqual(out["scenario"], out["baseline"])
        self.assertIn(
            ("equipment.apex_glyph_conversion", "mechanism_unresolved"),
            _skipped_pairs(out),
        )
        self.assertFalse(out["glyph_model"]["enabled"])

    def test_glyph_on_zeroes_ignite_and_boosts_body(self) -> None:
        out = self.glyph_boss
        self.assertTrue(out["glyph_model"]["enabled"])
        self.assertEqual(out["glyph_model"]["reason"], "glyph_conversion")
        single = out["scenario"]["single"]
        self.assertEqual(single["ignite_total"], 0.0)
        self.assertEqual(single["ignite_tail_damage"], 0.0)
        self.assertEqual(single["expected_total_stacks_applied"], 0.0)
        self.assertGreater(
            single["body_total"], self.base["baseline"]["single"]["body_total"]
        )
        self.assertLess(
            single["single_cast_total"], self.base["baseline"]["single"]["single_cast_total"]
        )
        self.assertLess(out["delta"]["ignite_total"], 0.0)
        apex = _applied(out, "equipment.apex_glyph_conversion")[0]
        self.assertEqual(apex["stat_key"], "resistance_shred")
        self.assertEqual(apex["engine_stat_key"], "resistance_shred")
        self.assertEqual(apex["value"], 0.2)
        self.assertEqual(apex["scope"], "both")
        self.assertEqual(out["glyph_model"]["total_shred"], 0.2)
        self.assertEqual(out["glyph_model"]["boss_effectiveness"], 0.4)
        self.assertEqual(out["glyph_model"]["effective_duration_s"], 6.08)
        self.assertEqual(out["glyph_model"]["target_fire_resistance_after"], -0.2)
        self.assertEqual(
            out["scenario_target"]["effective_fire_resistance"], -0.2
        )
        self.assertEqual(out["scenario_target"]["fire_resistance_shred"], 0.2)
        self.assertTrue(out["glyph_model"]["heat_wave_disabled"])
        self.assertNotIn(
            ("equipment.apex_glyph_conversion", "mechanism_unresolved"),
            _skipped_pairs(out),
        )

    def test_glyph_nonboss_uses_full_fifty_percent_shred(self) -> None:
        out = self.nonboss_glyph
        self.assertEqual(out["glyph_model"]["boss_effectiveness"], 1.0)
        self.assertEqual(out["glyph_model"]["total_shred"], 0.5)
        self.assertEqual(out["scenario"]["single"]["ignite_total"], 0.0)
        # Supermassive drops with the non-boss target; shred still raises body
        # relative to the same non-boss scenario without glyph.
        self.assertGreater(
            out["scenario"]["single"]["body_total"],
            self.nonboss_plain["scenario"]["single"]["body_total"],
        )

    def test_glyph_model_assumptions_and_warning(self) -> None:
        model_data = self.glyph_boss["glyph_model"]
        self.assertGreaterEqual(len(model_data["assumptions"]), 4)
        self.assertIn("heat_wave_disabled", model_data)
        self.assertTrue(
            any("boss effectiveness" in warning for warning in self.glyph_boss["warnings"]),
            self.glyph_boss["warnings"],
        )
        self.assertTrue(
            any("Glyph 模式" in assumption for assumption in self.glyph_boss["assumptions"])
        )

    def test_glyph_include_auto_only_adds_duration_not_ignite(self) -> None:
        out = recompute(glyph_on(), include_auto=True)
        self.assertIn(
            "enchant_weapon.eternal_flames.passive", _effect_ids(out)
        )
        self.assertEqual(out["scenario"]["single"]["ignite_total"], 0.0)

    def test_glyph_loop_dps_reflects_ignite_loss(self) -> None:
        baseline = recompute(ruleset=eager_loop_ruleset(), include_auto=False)
        out = recompute(glyph_on(), ruleset=eager_loop_ruleset())
        self.assertEqual(len(out["scenario"]["loops"]), 1)
        baseline_dps = baseline["baseline"]["loops"][0]["windows"][-1]["dps"]
        scenario_dps = out["scenario"]["loops"][0]["windows"][-1]["dps"]
        self.assertLess(scenario_dps, baseline_dps)
        self.assertLess(out["delta"]["loops"][0]["dps"], 0.0)

    def test_glyph_deterministic_and_no_mutation(self) -> None:
        scenario = copy.deepcopy(base_scenario())
        glyph_on()(scenario)
        snapshot = real_snapshot()
        ruleset = real_ruleset()
        before_snapshot = copy.deepcopy(snapshot.to_dict())
        before_ruleset = copy.deepcopy(ruleset.data)
        before_scenario = copy.deepcopy(scenario)
        first = recompute_scenario(
            snapshot,
            ruleset,
            scenario=scenario,
            registry=real_registry(),
            catalog=real_catalog(),
            include_auto=False,
        )
        second = recompute_scenario(
            snapshot,
            ruleset,
            scenario=scenario,
            registry=real_registry(),
            catalog=real_catalog(),
            include_auto=False,
        )
        self.assertEqual(
            json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True)
        )
        self.assertEqual(snapshot.to_dict(), before_snapshot)
        self.assertEqual(ruleset.data, before_ruleset)
        self.assertEqual(scenario, before_scenario)


# ---------------------------------------------------------------------------
# Degraded paths, immutability, determinism
# ---------------------------------------------------------------------------


class RecomputeSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_real_assets(cls)
        cls.base = recompute(include_auto=False)

    def test_registry_missing_degrades_to_skipped(self) -> None:
        scenario = copy.deepcopy(base_scenario())
        out = recompute_scenario(
            real_snapshot(),
            single_cast_ruleset(),
            scenario=scenario,
            registry=None,  # type: ignore[arg-type]
            catalog=real_catalog(),
            include_auto=False,
        )
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(out["schema"], SCHEMA)
        self.assertTrue(out["warnings"])
        self.assertTrue(out["dps_recomputed"])
        # Baseline is still the legacy口径 even though the conditional channel
        # is unavailable.
        self.assertLessEqual(
            abs(
                out["baseline"]["single"]["single_cast_total"]
                - legacy_result()["single_cast"]["totals"]["single_cast_total"]
            ),
            BASELINE_TOL,
        )

    def test_invalid_monster_level_falls_back_with_warning(self) -> None:
        out = recompute(include_auto=False, monster_level=999)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["monster_level"], 100)
        self.assertTrue(any("回退" in warning for warning in out["warnings"]))

    def test_malformed_ruleset_degrades_to_error(self) -> None:
        out = recompute_scenario(
            real_snapshot(),
            Ruleset(data={}),
            scenario=copy.deepcopy(base_scenario()),
            registry=real_registry(),
            catalog=real_catalog(),
            include_auto=False,
        )
        self.assertEqual(out["status"], "error")
        self.assertFalse(out["dps_recomputed"])
        self.assertTrue(out["warnings"])
        self.assertEqual(out["baseline"], {})

    def test_inputs_are_not_mutated(self) -> None:
        snapshot = real_snapshot()
        ruleset = single_cast_ruleset()
        scenario = copy.deepcopy(base_scenario())
        before_snapshot = copy.deepcopy(snapshot.to_dict())
        before_ruleset = copy.deepcopy(ruleset.data)
        before_scenario = copy.deepcopy(scenario)
        before_registry = copy.deepcopy(real_registry().data)
        recompute_scenario(
            snapshot,
            ruleset,
            scenario=scenario,
            registry=real_registry(),
            catalog=real_catalog(),
            include_auto=True,
        )
        self.assertEqual(snapshot.to_dict(), before_snapshot)
        self.assertEqual(ruleset.data, before_ruleset)
        self.assertEqual(scenario, before_scenario)
        self.assertEqual(real_registry().data, before_registry)
        self.assertEqual(len(snapshot.contributions), len(before_snapshot["contributions"]))

    def test_deterministic_and_idempotent(self) -> None:
        def mutate(scenario: dict) -> None:
            scenario["effects"]["meteor.craterborn"] = {"enabled": True}
            scenario["effects"]["idol.throne_of_ambition"] = {"stacks": 10}

        scenario = copy.deepcopy(base_scenario())
        mutate(scenario)
        first = recompute_scenario(
            real_snapshot(),
            single_cast_ruleset(),
            scenario=scenario,
            registry=real_registry(),
            catalog=real_catalog(),
            include_auto=True,
        )
        second = recompute_scenario(
            real_snapshot(),
            single_cast_ruleset(),
            scenario=scenario,
            registry=real_registry(),
            catalog=real_catalog(),
            include_auto=True,
        )
        self.assertEqual(
            json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True)
        )
        self.assertEqual(first["scenario_fingerprint"], second["scenario_fingerprint"])
        self.assertNotEqual(first["scenario_fingerprint"], "")

    def test_delta_math_consistent(self) -> None:
        out = recompute(selected_effect("flame_ward.infusion"))
        baseline_total = out["baseline"]["single"]["single_cast_total"]
        scenario_total = out["scenario"]["single"]["single_cast_total"]
        self.assertAlmostEqual(
            out["delta"]["single_cast_total"], scenario_total - baseline_total, places=6
        )
        self.assertAlmostEqual(
            out["delta"]["single_cast_total_pct"],
            (scenario_total - baseline_total) / baseline_total,
            places=12,
        )
        for loop_delta in out["delta"]["loops"]:
            self.assertAlmostEqual(
                loop_delta["dps"],
                loop_delta["scenario_dps"] - loop_delta["baseline_dps"],
                places=6,
            )

    def test_condition_application_entry_shape(self) -> None:
        out = recompute(selected_effect("meteor.craterborn"))
        expected_keys = {
            "effect_id",
            "modifier_id",
            "stat_key",
            "operation",
            "value",
            "unit",
            "target",
            "damage_tags",
            "branches",
            "condition",
            "status",
            "assumption_note",
        }
        for entry in out["condition_application"]["applied"]:
            self.assertTrue(expected_keys <= set(entry))
        for entry in out["condition_application"]["skipped"]:
            self.assertIn("effect_id", entry)
            self.assertIn("reason", entry)
            self.assertIn("scope", entry)


class RecomputeReviewFixTests(unittest.TestCase):
    """Independent-review fixes R1/R2/R4/R5/R7/R8 + single-run outputs."""

    @classmethod
    def setUpClass(cls) -> None:
        require_real_assets(cls)
        cls.base = recompute(include_auto=False)
        cls.snapshot = real_snapshot()
        cls.registry = real_registry()
        cls.binding = {
            "build_id": ((cls.snapshot.build_meta or {}).get("build") or {}).get("build_id"),
            "revision_id": ((cls.snapshot.build_meta or {}).get("build") or {}).get(
                "revision_id"
            ),
            "build_content_sha256": (
                (cls.snapshot.build_meta or {}).get("build") or {}
            ).get("content_sha256"),
            "calc_input_sha256": (
                (cls.snapshot.build_meta or {}).get("calc_input") or {}
            ).get("sha256"),
            "dataset_id": ((cls.snapshot.build_meta or {}).get("dataset") or {}).get(
                "dataset_id"
            ),
            "registry_id": cls.registry.registry_id,
            "registry_sha256": cls.registry.sha256,
            "ruleset_id": real_ruleset().ruleset_id,
            "ruleset_sha256": real_ruleset().sha256,
        }
        cls.calc_input = json.loads(CALC_INPUT_PATH.read_text(encoding="utf-8"))
        cls.build_data = json.loads(BUILD_PATH.read_text(encoding="utf-8"))
        cls.bound_catalog = build_catalog(
            cls.snapshot,
            registry=cls.registry,
            calc_input=cls.calc_input,
            build=cls.build_data,
            dps_scenario=legacy_result()["scenario"],
            binding=cls.binding,
        )

    # -- R1 unified target ------------------------------------------------
    def test_target_block_roundtrip_validation_fingerprint(self) -> None:
        target = {
            "monster_level": 50,
            "fire_resistance": 0.2,
            "boss": True,
            "rare": False,
        }
        scenario = default_scenario(
            self.registry, {"binding": {}, "target": target}
        )
        self.assertEqual(scenario["target"], target)
        resolved = normalize_scenario(
            scenario, self.registry, {"binding": {}, "target": target}
        )
        self.assertEqual(resolved["target"], target)
        self.assertTrue(resolved["states"]["target.boss_or_rare"])
        first_fingerprint = scenario_fingerprint(scenario)
        changed = copy.deepcopy(scenario)
        changed["target"]["monster_level"] = 60
        self.assertNotEqual(first_fingerprint, scenario_fingerprint(changed))
        self.assertEqual(
            validate_scenario(scenario, self.registry, {})["errors"], []
        )
        invalid = copy.deepcopy(scenario)
        invalid["target"] = {
            "monster_level": 300,
            "fire_resistance": 3.0,
            "boss": "yes",
        }
        codes = {
            error.get("code")
            for error in validate_scenario(invalid, self.registry, {})["errors"]
        }
        self.assertIn("value_out_of_bounds", codes)
        self.assertIn("invalid_type", codes)
        # Old scenarios without a target block stay valid.
        old = copy.deepcopy(base_scenario())
        old.pop("target", None)
        self.assertEqual(validate_scenario(old, self.registry, {})["errors"], [])

    def test_target_missing_defaults_from_ruleset(self) -> None:
        prepared = prepare_for()
        self.assertEqual(
            prepared["target"],
            {
                "monster_level": 100,
                "fire_resistance": 0.0,
                "boss": True,
                "rare": True,
            },
        )
        self.assertEqual(prepared["scenario_target"]["boss_or_rare"], True)
        self.assertTrue(prepared["scenario_target"]["within_center_1m"])

    def test_target_applied_to_baseline_and_scenario_engine(self) -> None:
        target = {
            "monster_level": 50,
            "fire_resistance": 0.2,
            "boss": True,
            "rare": True,
        }
        scenario = copy.deepcopy(base_scenario())
        scenario["target"] = target
        out = recompute(scenario=copy.deepcopy(scenario), include_auto=False)
        self.assertEqual(out["target"], target)
        self.assertEqual(out["scenario_target"]["monster_level"], 50)
        self.assertEqual(out["scenario_target"]["fire_resistance"], 0.2)
        self.assertNotEqual(
            out["baseline"]["single"]["single_cast_total"],
            self.base["baseline"]["single"]["single_cast_total"],
        )
        prepared = prepare_for(scenario=copy.deepcopy(scenario))
        self.assertEqual(prepared["engine_scenario"]["target"]["monster_level"], 50)
        self.assertEqual(
            prepared["engine_scenario"]["target"]["resistances"]["fire"], 0.2
        )
        self.assertEqual(
            prepared["baseline_scenario"]["target"]["monster_level"], 50
        )
        self.assertEqual(
            prepared["baseline_scenario"]["target"]["resistances"]["fire"], 0.2
        )
        self.assertEqual(prepared["target"], target)

    # -- R2 baseline profile ---------------------------------------------
    def test_baseline_profile_policy_frozen_numbers(self) -> None:
        build_base = recompute(
            scenario=copy.deepcopy(base_scenario()), include_auto=None
        )
        self.assertTrue(build_base["include_auto"])
        self.assertEqual(build_base["baseline_profile"], "build_base")
        self.assertAlmostEqual(
            build_base["scenario"]["single"]["single_cast_total"],
            11440036.186141811,
            places=6,
        )
        legacy_scenario = copy.deepcopy(base_scenario())
        legacy_scenario["baseline_profile"] = "legacy_single_skill"
        legacy = recompute(scenario=copy.deepcopy(legacy_scenario), include_auto=None)
        self.assertFalse(legacy["include_auto"])
        self.assertEqual(legacy["baseline_profile"], "legacy_single_skill")
        self.assertAlmostEqual(
            legacy["scenario"]["single"]["single_cast_total"],
            10662227.469716422,
            places=6,
        )
        self.assertAlmostEqual(
            legacy["baseline"]["single"]["single_cast_total"],
            10662227.469716422,
            places=6,
        )
        # explicit include_auto overrides the profile policy
        explicit = recompute(
            scenario=copy.deepcopy(legacy_scenario), include_auto=True
        )
        self.assertAlmostEqual(
            explicit["scenario"]["single"]["single_cast_total"],
            11440036.186141811,
            places=6,
        )
        explicit_off = recompute(
            scenario=copy.deepcopy(base_scenario()), include_auto=False
        )
        self.assertAlmostEqual(
            explicit_off["scenario"]["single"]["single_cast_total"],
            10662227.469716422,
            places=6,
        )

    # -- R7 strict validation -------------------------------------------
    def test_stale_scenario_fails_closed_and_diagnostic_bypass(self) -> None:
        stale = copy.deepcopy(base_scenario())
        stale["binding"]["revision_id"] = "obsolete-revision"
        out = recompute_scenario(
            self.snapshot,
            single_cast_ruleset(),
            scenario=stale,
            registry=self.registry,
            catalog=self.bound_catalog,
            include_auto=False,
        )
        self.assertEqual(out["status"], "error")
        self.assertFalse(out["dps_recomputed"])
        self.assertEqual(out["baseline"], {})
        self.assertEqual(out["condition_application"]["applied"], [])
        self.assertTrue(
            any("fail-closed" in warning for warning in out["warnings"])
        )
        diagnostic = recompute_scenario(
            self.snapshot,
            single_cast_ruleset(),
            scenario=copy.deepcopy(stale),
            registry=self.registry,
            catalog=self.bound_catalog,
            include_auto=False,
            allow_stale=True,
        )
        self.assertEqual(diagnostic["status"], "ok")
        self.assertTrue(diagnostic["diagnostic"])
        # A fresh scenario with the matching binding is accepted.
        fresh = default_scenario(self.registry, {"binding": self.binding})
        accepted = recompute_scenario(
            self.snapshot,
            single_cast_ruleset(),
            scenario=fresh,
            registry=self.registry,
            catalog=self.bound_catalog,
            include_auto=False,
        )
        self.assertEqual(accepted["status"], "ok")
        self.assertFalse(accepted["diagnostic"])

    # -- R4 assumptions --------------------------------------------------
    def test_ignite_inheritance_assumptions_are_explicit(self) -> None:
        ambition = recompute(
            lambda scenario: scenario["effects"].__setitem__(
                "idol.throne_of_ambition", {"stacks": 20}
            ),
            include_auto=False,
        )
        ambition_assumptions = {
            row["assumption_id"]
            for row in ambition["assumptions_applied"]
            if row["effect_id"] == "idol.throne_of_ambition"
        }
        self.assertIn(
            "assume:ignite-inheritance:idol.throne_of_ambition.fire_more",
            ambition_assumptions,
        )
        self.assertGreater(
            ambition["scenario"]["single"]["ignite_total"],
            ambition["baseline"]["single"]["ignite_total"],
        )
        fire_row = next(
            entry
            for entry in ambition["condition_application"]["applied"]
            if entry["modifier_id"] == "idol.throne_of_ambition.fire_more"
        )
        self.assertIn(
            "assume:ignite-inheritance:idol.throne_of_ambition.fire_more",
            fire_row["assumption_ids"],
        )
        self.assertTrue(fire_row["assumption"])
        for label, mutate in (
            (
                "chilled",
                lambda scenario: (
                    scenario["effects"].__setitem__(
                        "frost_wall.chilled_touch", {"enabled": True}
                    ),
                    scenario["states"].__setitem__("target.chilled", True),
                ),
            ),
            (
                "crescendo",
                lambda scenario: scenario["single_cast"].__setitem__(
                    "crescendoing_rage_applies", True
                ),
            ),
        ):
            out = recompute(mutate, include_auto=False)
            with self.subTest(label=label):
                self.assertTrue(
                    any(
                        row["assumption_id"].startswith("assume:ignite-inheritance:")
                        for row in out["assumptions_applied"]
                    )
                )

    # -- R5 application states ------------------------------------------
    def test_application_states_cover_all_effects(self) -> None:
        out = recompute(
            lambda scenario: scenario["effects"].__setitem__(
                "idol.throne_of_ambition", {"stacks": 20}
            ),
            include_auto=False,
        )
        states = out["application_states"]
        self.assertEqual(
            set(states), {effect["effect_id"] for effect in self.registry.effects}
        )
        ambition = states["idol.throne_of_ambition"]
        self.assertEqual(ambition["state"], "applied_both")
        self.assertTrue(ambition["single"])
        self.assertTrue(ambition["loop"])
        self.assertTrue(ambition["assumptions"])
        self.assertIn("idol.throne_of_ambition.fire_more", ambition["modifier_ids"])
        self.assertEqual(len(ambition["applied_rows"]), 2)
        self.assertIn("idol.throne_of_ambition", out["applied_effects"])
        passive = states["enchant_weapon.eternal_flames.passive"]
        self.assertEqual(passive["state"], "skipped")
        self.assertEqual(passive["reason"], "auto_excluded")
        supermassive = states["main_skill.supermassive"]
        self.assertEqual(supermassive["state"], "not_selected")
        self.assertEqual(supermassive["reason"], "already_in_baseline")
        self.assertTrue(states["equipment.apex_glyph_conversion"]["support_level"])
        crescendo = recompute(
            lambda scenario: scenario["single_cast"].__setitem__(
                "crescendoing_rage_applies", True
            ),
            include_auto=False,
        )["application_states"]["ring.crescendoing_rage"]
        self.assertEqual(crescendo["state"], "applied_single")
        self.assertFalse(crescendo["loop"])
        self.assertEqual(crescendo["reason"], "needs_action_or_time_simulation")

    # -- R8 independent boss/rare flags ---------------------------------
    def test_rare_only_glyph_uses_full_shred_but_keeps_supermassive(self) -> None:
        rare_scenario = copy.deepcopy(base_scenario())
        rare_scenario["effects"]["equipment.apex_glyph_conversion"] = {"enabled": True}
        rare_scenario["target"] = {
            "monster_level": 100,
            "fire_resistance": 0.0,
            "boss": False,
            "rare": True,
        }
        rare = recompute(scenario=rare_scenario, include_auto=False)
        self.assertFalse(rare["glyph_model"]["boss_target"])
        self.assertTrue(rare["glyph_model"]["rare_target"])
        self.assertEqual(rare["glyph_model"]["boss_effectiveness"], 1.0)
        self.assertEqual(rare["glyph_model"]["total_shred"], 0.5)
        # Supermassive still applies because the combined condition is boss OR rare.
        self.assertTrue(
            rare["condition_application"]["target_conditions"]["target_is_boss_or_rare"]
        )
        self.assertAlmostEqual(
            rare["baseline"]["single"]["body_total"],
            self.base["baseline"]["single"]["body_total"],
            places=6,
        )
        self.assertGreater(
            rare["scenario"]["single"]["body_total"],
            rare["baseline"]["single"]["body_total"],
        )
        boss_scenario = copy.deepcopy(rare_scenario)
        boss_scenario["target"]["boss"] = True
        boss_scenario["target"]["rare"] = False
        boss = recompute(scenario=boss_scenario, include_auto=False)
        self.assertEqual(boss["glyph_model"]["boss_effectiveness"], 0.4)
        self.assertEqual(boss["glyph_model"]["total_shred"], 0.2)
        self.assertGreater(
            rare["scenario"]["single"]["body_total"],
            boss["scenario"]["single"]["body_total"],
        )

    # -- performance: one simulation per environment --------------------
    def test_compute_scenario_outputs_single_run_shape(self) -> None:
        scenario = copy.deepcopy(base_scenario())
        prepared = prepare_scenario(
            self.snapshot,
            real_ruleset(),
            scenario=scenario,
            registry=self.registry,
            catalog=real_catalog(),
            include_auto=False,
        )
        outputs = compute_scenario_outputs(prepared)
        self.assertEqual(outputs["status"], "ok")
        self.assertEqual(
            sorted(outputs["raw"]),
            ["baseline_loops", "baseline_single", "scenario_loops", "scenario_single"],
        )
        rule_loop_count = len(real_ruleset().action_policies)
        self.assertEqual(len(outputs["raw"]["scenario_loops"]), rule_loop_count)
        self.assertEqual(len(outputs["raw"]["baseline_loops"]), rule_loop_count)
        reference = recompute(include_auto=False, ruleset=real_ruleset())
        self.assertAlmostEqual(
            outputs["scenario"]["single"]["single_cast_total"],
            reference["scenario"]["single"]["single_cast_total"],
            places=9,
        )
        self.assertAlmostEqual(
            outputs["delta"]["single_cast_total"],
            reference["delta"]["single_cast_total"],
            places=6,
        )
        self.assertEqual(outputs["baseline"]["single"], reference["baseline"]["single"])
        scenario_only = compute_scenario_outputs(prepared, include_baseline=False)
        self.assertEqual(scenario_only["baseline"], {})
        self.assertEqual(scenario_only["delta"], {})
        self.assertEqual(
            scenario_only["scenario"], outputs["scenario"]
        )
        self.assertEqual(
            [loop["policy_id"] for loop in scenario_only["raw"]["scenario_loops"]],
            [loop["policy_id"] for loop in outputs["raw"]["scenario_loops"]],
        )
        # Ready-to-forward bundle for report.build_result(precomputed_outputs=...)
        self.assertEqual(outputs["report"]["scenario"], prepared["engine_scenario"])
        self.assertIs(outputs["report"]["single"], outputs["raw"]["scenario_single"])
        self.assertIs(outputs["report"]["loops"], outputs["raw"]["scenario_loops"])
        # The service can reuse the one-pass outputs for the recompute payload
        # instead of running the engine again.
        reused = recompute_scenario(
            self.snapshot,
            real_ruleset(),
            scenario=copy.deepcopy(base_scenario()),
            registry=self.registry,
            catalog=real_catalog(),
            include_auto=False,
            precomputed_outputs=outputs,
        )
        self.assertEqual(reused["status"], "ok")
        self.assertEqual(reused["scenario"], outputs["scenario"])
        self.assertEqual(reused["delta"], outputs["delta"])


if __name__ == "__main__":
    unittest.main()
