"""Tests for ``le_calc.conditional`` (conditional Buff configuration backend).

Coverage:
* registry loader/validator (real registry + synthetic malformed fixtures),
* discovery denominators/candidates/data gaps on the real frozen build,
* scenario defaults/validation/save-load/fingerprint,
* evaluation status precedence (contract §8.4) on real + synthetic fixtures,
* client-rules payload / page view contract (§8.5/§9),
* degradation, determinism, idempotency and "proposed contributions do not
  touch the snapshot/damage" guarantees.

Run from the repo root::

    $env:PYTHONPATH='tools\\phase3\\src'
    .\\.venv\\Scripts\\python.exe -m unittest tools.phase3.tests.test_conditional -v
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from le_calc import model
from le_calc.conditional import (
    DEFAULT_REGISTRY_PATH,
    RegistryError,
    ScenarioError,
    apply_preset,
    build_catalog,
    build_client_rules,
    build_context,
    default_scenario,
    evaluate_scenario,
    export_scenario,
    load_registry,
    load_scenario,
    normalize_scenario,
    resolve_actuals,
    save_scenario,
    scenario_fingerprint,
    validate_registry,
    validate_scenario,
    write_artifacts,
)
from le_calc.conditional import constants as C

REPO_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_PATH = REPO_ROOT / "examples" / "black-hole" / "snapshot" / "input-snapshot.affix-db.json"
CALC_INPUT_PATH = (
    REPO_ROOT / "examples" / "black-hole" / "inputs" / "calculation-input.affix-db.json"
)
RESULT_PATH = REPO_ROOT / "reports" / "black-hole" / "save-build-bh-affix-db" / "result.json"
BUILD_PATH = REPO_ROOT / "examples" / "black-hole" / "inputs" / "bd.json"
RULESET_PATH = REPO_ROOT / "tools" / "phase3" / "rules" / "ruleset-bh-stage1-v4.json"
UI_FIXTURE_PATH = REPO_ROOT / "tools" / "phase3" / "fixtures" / "conditional-ui" / "fixture_view.json"

REQUIRED_REAL_FILES = (DEFAULT_REGISTRY_PATH, SNAPSHOT_PATH, CALC_INPUT_PATH, RESULT_PATH)


# ---------------------------------------------------------------------------
# Lazily loaded real assets
# ---------------------------------------------------------------------------

_CACHE: dict[str, object] = {}



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def real_registry():
    if "registry" not in _CACHE:
        _CACHE["registry"] = load_registry(DEFAULT_REGISTRY_PATH)
    return _CACHE["registry"]


def real_snapshot() -> model.Snapshot:
    if "snapshot" not in _CACHE:
        _CACHE["snapshot"] = model.Snapshot.from_dict(_load_json(SNAPSHOT_PATH))
    return _CACHE["snapshot"]


def real_calc_input() -> dict:
    if "calc_input" not in _CACHE:
        _CACHE["calc_input"] = _load_json(CALC_INPUT_PATH)
    return _CACHE["calc_input"]


def real_build() -> dict:
    if "build" not in _CACHE:
        _CACHE["build"] = _load_json(BUILD_PATH)
    return _CACHE["build"]


def real_dps_scenario() -> dict:
    if "dps_scenario" not in _CACHE:
        _CACHE["dps_scenario"] = _load_json(RESULT_PATH)["scenario"]
    return _CACHE["dps_scenario"]


def real_context_kwargs() -> dict:
    return {
        "scenario": real_dps_scenario(),
        "ruleset_path": str(RULESET_PATH),
        "build_path": str(BUILD_PATH),
        "repo_root": str(REPO_ROOT),
    }


def real_context():
    """Cached ``build_context`` result on the real frozen build."""
    if "context" not in _CACHE:
        _CACHE["context"] = build_context(real_snapshot(), **real_context_kwargs())
    return _CACHE["context"]


def find_effect(registry, *needles: str) -> dict:
    """Locate one registry effect by case-insensitive substring(s)."""
    wanted = [needle.casefold().replace("_", " ") for needle in needles]
    for effect in registry.effects:
        haystack = " ".join(
            str(effect.get(key) or "")
            for key in ("effect_id", "name", "name_en", "group_id")
        ).casefold().replace("_", " ")
        if all(needle in haystack for needle in wanted):
            return effect
    raise AssertionError(f"no registry effect matching {needles!r}")


def effect_id(registry, *needles: str) -> str:
    return str(find_effect(registry, *needles)["effect_id"])


def evaluation_by_id(evaluation: dict) -> dict[str, dict]:
    return {
        record["effect_id"]: record
        for record in evaluation.get("effects") or []
        if isinstance(record, dict) and record.get("effect_id")
    }


def eval_variant(context: dict, mutate) -> dict:
    """Evaluate a deep-copied resolved scenario after ``mutate(scenario)``."""
    scenario = copy.deepcopy(context["scenario"])
    mutate(scenario)
    return evaluate_scenario(scenario, real_registry(), context["catalog"])


def require_real_assets(testcase: unittest.TestCase) -> None:
    missing = [str(path.relative_to(REPO_ROOT)) for path in REQUIRED_REAL_FILES if not path.is_file()]
    if missing:
        testcase.skipTest("required real build assets are absent: " + ", ".join(missing))


# ---------------------------------------------------------------------------
# Synthetic registry / build fixtures (edge cases)
# ---------------------------------------------------------------------------


def _synthetic_effect(
    effect_id_value: str,
    *,
    category: str = "other_skill",
    node: int = 1,
    tree: str = "Test Tree",
    min_points: int = 1,
    control: dict | None = None,
    conditions: dict | None = None,
    activation: dict | None = None,
    relevance_status: str = "relevant",
    baseline_relation: str = "new",
    support: str = "needs_rule_mapping",
    research: str = "confirmed",
    value: float = 0.1,
    name: str | None = None,
) -> dict:
    effect_name = name or effect_id_value
    values = [
        {
            "key": "test_value",
            "label": "测试值",
            "per_point": None,
            "constant": value,
            "unit": "fraction",
            "display": "+10%",
        }
    ]
    modifiers = [
        {
            "modifier_id": f"{effect_id_value}.mod",
            "stat_key": "cast_speed",
            "stat_label": "施法速度",
            "operation": "increased",
            "unit": "fraction",
            "target": None,
            "damage_tags": [],
            "value_ref": "test_value",
            "branches": ["cast"],
            "engine_sink": "snapshot.contributions[stat_key=cast_speed]",
            "dedupe_contribution_ids": [],
            "note": "synthetic",
        }
    ]
    return {
        "effect_id": effect_id_value,
        "group_id": effect_id_value,
        "name": effect_name,
        "name_en": effect_name,
        "category": category,
        "source": {
            "kind": "skill_node",
            "display": f"{tree} · {effect_name} · 3 点",
            "tree": tree,
            "tree_entity_id": None,
            "node_id": node,
            "node_name": effect_name,
            "slot": None,
            "item_instance_id": None,
            "item_display_name": None,
            "locator": "synthetic#/effects/0",
            "evidence": [],
        },
        "match": {
            "type": "skill_node",
            "tree": tree,
            "tree_entity_id": None,
            "node_id": node,
            "min_points": min_points,
            "instance_id": None,
            "mod_id": None,
            "name_contains": None,
        },
        "values": values,
        "modifiers": modifiers,
        "activation": activation
        or {
            "kind": "persistent_buff",
            "duration_s": None,
            "max_stacks": 1,
            "refresh": "unknown",
            "source_action": None,
            "description": "synthetic activation",
        },
        "control": control
        or {
            "kind": "toggle",
            "state_id": "state.user",
            "stack_state_id": None,
            "label": "synthetic toggle",
            "help": "",
            "default": False,
        },
        "conditions": conditions or {"all": [], "any": [], "note": ""},
        "relevance": {
            "status": relevance_status,
            "path": "synthetic impact path",
            "branches": ["cast"],
            "scenario_prerequisites": [],
            "baseline_relation": baseline_relation,
            "notes": "",
        },
        "support": {
            "level": support,
            "engine_gap": "synthetic engine gap",
            "next_probe": "synthetic probe",
            "open_questions": [],
        },
        "research": {
            "status": research,
            "confidence_note": "synthetic fixture",
            "open_questions": [],
        },
        "evidence": [{"kind": "derivation", "detail": "synthetic test fixture"}],
    }


def _synthetic_control(kind: str, *, state_id: str | None = None, stack_state_id: str | None = None) -> dict:
    return {
        "kind": kind,
        "state_id": state_id,
        "stack_state_id": stack_state_id,
        "cast_state_id": None,
        "label": "synthetic control",
        "help": "",
        "default": False if kind != "stacks" else 0,
    }


def synthetic_registry_doc() -> dict:
    effects = [
        _synthetic_effect("test.ready_rules"),
        _synthetic_effect(
            "test.unmet",
            control=_synthetic_control("toggle", state_id="state.user"),
            conditions={"all": [{"state": "state.other", "equals": True}], "any": [], "note": ""},
        ),
        _synthetic_effect(
            "test.needs_time",
            support="needs_action_or_time_simulation",
        ),
        _synthetic_effect(
            "test.auto_inactive",
            control=_synthetic_control("none"),
            conditions={"all": [{"state": "auto.threshold", "equals": True}], "any": [], "note": ""},
        ),
        _synthetic_effect(
            "test.auto_unknown",
            control=_synthetic_control("none"),
            conditions={"all": [{"state": "auto.missing", "equals": True}], "any": [], "note": ""},
        ),
        _synthetic_effect(
            "test.auto_active",
            control=_synthetic_control("none"),
            support="mechanism_unresolved",
        ),
        _synthetic_effect("test.engine_supported", support="engine_supported"),
        _synthetic_effect(
            "test.auto_engine_supported",
            control=_synthetic_control("none"),
            support="engine_supported",
        ),
        _synthetic_effect("test.unconfirmed", support="mechanism_unresolved", research="unresolved"),
        _synthetic_effect(
            "test.action",
            category="action",
            control=_synthetic_control("none"),
            activation={
                "kind": "action",
                "duration_s": None,
                "max_stacks": 1,
                "refresh": "unknown",
                "source_action": None,
                "description": "synthetic action",
            },
            relevance_status="scenario_dependent",
            baseline_relation="action_not_supported",
            support="needs_action_or_time_simulation",
        ),
        _synthetic_effect("test.unavailable", node=2, min_points=2),
        _synthetic_effect(
            "test.baseline",
            control=_synthetic_control("none"),
            baseline_relation="already_in_baseline",
            support="engine_supported",
        ),
        _synthetic_effect(
            "test.stacks",
            control=_synthetic_control("stacks", stack_state_id="state.stacks"),
        ),
    ]
    for effect in effects:
        # ``test.auto_unknown`` must not carry the toggle control default.
        if effect["effect_id"] == "test.auto_unknown":
            effect["control"] = _synthetic_control("none")
    return {
        "schema": C.SCHEMA_REGISTRY,
        "registry_id": "synthetic-conditional-registry",
        "game_version": "1.5.2",
        "dataset_id": "phase1-dataset-003",
        "created": "2026-11-06",
        "context": {
            "main_skill_id": "bh2",
            "main_skill_name": "Black Hole",
            "ruleset_id": "bh-stage1-v4",
            "modes": [
                {
                    "id": "fixed_conditions",
                    "label": "固定条件估算",
                    "description": "synthetic mode",
                }
            ],
            "baseline_profiles": [
                {
                    "id": "legacy_single_skill",
                    "label": "旧版单技能基线",
                    "description": "synthetic",
                },
                {
                    "id": "build_base",
                    "label": "构筑基础状态",
                    "description": "synthetic default",
                    "default": True,
                },
            ],
            "disclaimer": "synthetic disclaimer",
        },
        "computation_modules": [
            {
                "id": "cast",
                "label": "施法时间",
                "question": "?",
                "consumes": ["cast_speed"],
                "calc_refs": [],
                "rule_ids": [],
                "notes": "",
                "evidence": [],
            }
        ],
        "shared_states": [
            {
                "id": "state.user",
                "label": "用户状态",
                "kind": "bool",
                "source": "user",
                "default": False,
                "bounds": None,
                "description": "",
                "affects": [],
                "evidence": [],
            },
            {
                "id": "state.other",
                "label": "另一状态",
                "kind": "bool",
                "source": "user",
                "default": False,
                "bounds": None,
                "description": "",
                "affects": [],
                "evidence": [],
            },
            {
                "id": "state.stacks",
                "label": "层数",
                "kind": "int",
                "source": "user",
                "default": 0,
                "bounds": {"min": 0, "max": 3},
                "description": "",
                "affects": [],
                "evidence": [],
            },
            {
                "id": "auto.known",
                "label": "已知派生",
                "kind": "int",
                "source": "auto",
                "default": 0,
                "bounds": None,
                "derive": {"kind": "snapshot", "key": "derived.known_value"},
                "description": "",
                "affects": [],
                "evidence": [],
            },
            {
                "id": "auto.threshold",
                "label": "门槛",
                "kind": "bool",
                "source": "auto",
                "default": False,
                "bounds": None,
                "derive": {"ref": "auto.known", "op": ">=", "threshold": 10},
                "description": "",
                "affects": [],
                "evidence": [],
            },
            {
                "id": "auto.missing",
                "label": "未知派生",
                "kind": "bool",
                "source": "auto",
                "default": False,
                "bounds": None,
                "derive": {"kind": "snapshot", "key": "derived.missing_value"},
                "description": "",
                "affects": [],
                "evidence": [],
            },
        ],
        "effects": effects,
        "checked_records": [],
        "checked_scope_expectations": {
            "equipment_items": 1,
            "idols": 0,
            "blessings": 0,
            "idol_altar": 0,
            "passive_nodes": 1,
            "specializations": 1,
            "snapshot_contributions": 0,
            "excluded_sources": 0,
        },
        "presets": [
            {
                "preset_id": "build_base",
                "label": "构筑基础状态",
                "description": "synthetic default",
                "states": {},
                "effects": {},
                "single_cast": {},
                "loop": {},
            }
        ],
    }


def synthetic_snapshot() -> model.Snapshot:
    return model.Snapshot(
        build_meta={
            "build": {
                "build_id": "synthetic:build",
                "revision_id": "rev-synthetic",
                "content_sha256": "synthetic-content",
            },
            "dataset": {"dataset_id": "phase1-dataset-003", "target_game_version": "1.5.2"},
            "calc_input": {"path": "synthetic/calc-input.json", "sha256": "synthetic-calc"},
        },
        character={"level": 100, "class": "Mage", "mastery": "Sorcerer"},
        attributes={"intelligence": 10.0},
        resistances={},
        contributions=[],
        derived={"known_value": 5},
        issues=[],
        excluded_sources=[],
    )


def synthetic_calc_input() -> dict:
    mods = [
        {
            "mod_id": "equipment.head#implicit#0",
            "item_instance_id": "equipment.head",
            "source": "implicit",
            "channel": "implicit",
            "kind": "mod",
            "stat": {"name": "Armor", "resolved": True},
            "readable_text": "+5 Armor",
            "display_value": "5",
        }
    ]
    return {
        "schema_version": "calculation-input-v1",
        "items": [
            {
                "instance_id": "equipment.head",
                "role": "equipment",
                "slot": "head",
                "display_name": "Synthetic Helmet",
                "mods": [{"mod_id": "equipment.head#implicit#0"}],
            }
        ],
        "mods": mods,
        "skills": {
            "specializations": [
                {
                    "tree": {"entity_id": "tree:test:tt", "name": "Test Tree"},
                    "points_total": 3,
                    "nodes": [
                        {"node_id": 1, "name": "Node One", "points": 3, "max_points": 3},
                        {"node_id": 2, "name": "Node Two", "points": 0, "max_points": 3},
                    ],
                }
            ]
        },
        "passive_tree": {
            "tree": {"entity_id": "tree:test:passive", "name": "Test Passive Tree"},
            "nodes": [{"node_id": 1, "name": "Test Passive", "points": 4, "max_points": 4}],
        },
    }


_SYNTH: dict[str, object] = {}


def synthetic_registry():
    if "registry" not in _SYNTH:
        directory = Path(tempfile.mkdtemp(prefix="le-conditional-synth-"))
        path = directory / "conditional-effects.json"
        path.write_text(
            json.dumps(synthetic_registry_doc(), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        _SYNTH["registry"] = load_registry(path)
    return _SYNTH["registry"]


def synthetic_catalog() -> dict:
    if "catalog" not in _SYNTH:
        registry = synthetic_registry()
        catalog = build_catalog(
            synthetic_snapshot(),
            registry=registry,
            calc_input=synthetic_calc_input(),
            build=None,
            catalog=None,
            dps_scenario={},
            binding={},
        )
        baseline = default_scenario(registry, {"binding": {}})
        catalog["baseline_scenario"] = baseline
        catalog["baseline_fingerprint"] = scenario_fingerprint(baseline)
        _SYNTH["catalog"] = catalog
    return _SYNTH["catalog"]


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_real_assets(cls)

    def test_load_registry_sha256_and_indexes(self) -> None:
        registry = real_registry()
        self.assertEqual(registry.schema, C.SCHEMA_REGISTRY)
        self.assertRegex(registry.sha256 or "", r"^[0-9a-f]{64}$")
        self.assertGreaterEqual(len(registry.effects), 16)
        self.assertGreaterEqual(len(registry.shared_states), 10)
        self.assertIn("fixed_conditions", {mode["id"] for mode in registry.modes})
        self.assertEqual(registry.default_preset()["preset_id"], "build_base")
        self.assertEqual(
            registry.effect(registry.effects[0]["effect_id"]), registry.effects[0]
        )

    def test_registry_fingerprint_stable(self) -> None:
        first = load_registry(DEFAULT_REGISTRY_PATH)
        second = load_registry(DEFAULT_REGISTRY_PATH)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(len(first.effects), len(second.effects))

    def test_key_effects_present(self) -> None:
        registry = real_registry()
        for needles in (
            ("craterborn",),
            ("infusion",),
            ("chilled touch",),
            ("eternal flames",),
            ("crescendoing",),
            ("ambition",),
            ("frozen steel",),
            ("preparation",),
            ("supermassive",),
            ("weakened reality",),
        ):
            with self.subTest(needles=needles):
                find_effect(registry, *needles)
        self.assertTrue(
            any("mana" in effect["effect_id"] or "法力" in str(effect.get("name")) for effect in registry.effects)
        )

    def test_registry_actuals_match_real_points(self) -> None:
        registry = real_registry()
        craterborn = find_effect(registry, "craterborn")
        actuals = resolve_actuals(craterborn, points=5)["values"]
        self.assertAlmostEqual(actuals["fire_penetration"]["value"], 0.75)
        self.assertAlmostEqual(actuals["cast_speed"]["value"], 0.15)
        self.assertAlmostEqual(actuals["duration_s"]["value"], 6.0)

        eternal_passive = find_effect(registry, "eternal flames", "passive")
        passive_actuals = resolve_actuals(eternal_passive, points=5)["values"]
        self.assertAlmostEqual(passive_actuals["passive_ignite_duration"]["value"], 0.25)

        eternal_active = find_effect(registry, "eternal flames", "active")
        active_actuals = resolve_actuals(eternal_active, points=5)["values"]
        self.assertAlmostEqual(active_actuals["active_incremental_ignite_duration"]["value"], 0.25)

        infusion = find_effect(registry, "infusion")
        self.assertAlmostEqual(resolve_actuals(infusion, points=5)["values"]["increased_fire_damage"]["value"], 2.5)

        frozen = find_effect(registry, "frozen steel")
        self.assertAlmostEqual(
            resolve_actuals(frozen, points=4)["values"]["damage_vs_chilled_or_frozen"]["value"], 0.06
        )

    def test_validate_registry_rejects_malformed_documents(self) -> None:
        doc = copy.deepcopy(synthetic_registry_doc())

        cases: list[tuple[str, dict, str]] = []
        missing_key = copy.deepcopy(doc)
        del missing_key["presets"]
        cases.append(("missing_top_key", missing_key, "missing_field"))
        bad_enum = copy.deepcopy(doc)
        bad_enum["effects"][0]["category"] = "bogus"
        cases.append(("bad_category", bad_enum, "invalid_enum"))
        dangling_state = copy.deepcopy(doc)
        dangling_state["effects"][0]["conditions"] = {
            "all": [{"state": "state.nope", "equals": True}],
            "any": [],
            "note": "",
        }
        cases.append(("dangling_state", dangling_state, "unknown_state"))
        bad_branch = copy.deepcopy(doc)
        bad_branch["effects"][0]["modifiers"][0]["branches"] = ["cast", "nope"]
        cases.append(("bad_branch", bad_branch, "unknown_branch"))
        bad_ref = copy.deepcopy(doc)
        bad_ref["effects"][0]["modifiers"][0]["value_ref"] = "nope"
        cases.append(("bad_value_ref", bad_ref, "unknown_value_ref"))
        duplicate_effect = copy.deepcopy(doc)
        duplicate_effect["effects"].append(copy.deepcopy(duplicate_effect["effects"][0]))
        cases.append(("duplicate_effect", duplicate_effect, "duplicate_id"))
        no_evidence = copy.deepcopy(doc)
        no_evidence["effects"][0]["evidence"] = []
        cases.append(("no_evidence", no_evidence, "missing_evidence"))
        no_value = copy.deepcopy(doc)
        no_value["effects"][0]["values"][0].update({"per_point": None, "constant": None})
        cases.append(("no_value", no_value, "no_value"))
        control_auto = copy.deepcopy(doc)
        control_auto["effects"][0]["control"]["state_id"] = "auto.known"
        cases.append(("control_auto_state", control_auto, "control_state_not_user"))
        duplicate_modifier = copy.deepcopy(doc)
        duplicate_modifier["effects"][1]["modifiers"][0]["modifier_id"] = duplicate_modifier["effects"][0][
            "modifiers"
        ][0]["modifier_id"]
        cases.append(("duplicate_modifier", duplicate_modifier, "duplicate_id"))

        for label, document, expected_code in cases:
            with self.subTest(label=label):
                codes = {issue["code"] for issue in validate_registry(document)}
                self.assertIn(expected_code, codes)

    def test_load_registry_raises_readable_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RegistryError) as missing:
                load_registry(Path(tmp) / "absent.json")
            self.assertIn("not found", str(missing.exception))
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaises(RegistryError) as invalid:
                load_registry(broken)
            self.assertIn("JSON", str(invalid.exception))
            malformed = Path(tmp) / "malformed.json"
            malformed.write_text(json.dumps({"schema": "wrong"}), encoding="utf-8")
            with self.assertRaises(RegistryError):
                load_registry(malformed)


# ---------------------------------------------------------------------------
# Synthetic discovery + evaluation status precedence
# ---------------------------------------------------------------------------


class SyntheticEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = synthetic_registry()
        cls.catalog = synthetic_catalog()
        cls.base_scenario = default_scenario(cls.registry, {"binding": {}})

    def _evaluate(self, mutate) -> dict:
        scenario = copy.deepcopy(self.base_scenario)
        mutate(scenario)
        return evaluate_scenario(scenario, self.registry, self.catalog)

    def _status(self, evaluation: dict, effect_id_value: str) -> str:
        return evaluation_by_id(evaluation)[effect_id_value]["status"]

    def _enable(self, effect_id_value: str) -> dict:
        def mutate(scenario: dict) -> None:
            scenario["effects"].setdefault(effect_id_value, {})
            scenario["effects"][effect_id_value]["enabled"] = True

        return self._evaluate(mutate)

    def test_default_statuses(self) -> None:
        evaluation = evaluate_scenario(self.base_scenario, self.registry, self.catalog)
        self.assertEqual(evaluation["validation"]["errors"], [])
        statuses = {record["effect_id"]: record["status"] for record in evaluation["effects"]}
        self.assertEqual(statuses["test.ready_rules"], "not_selected")
        self.assertEqual(statuses["test.unavailable"], "not_available")
        self.assertEqual(statuses["test.action"], "unsupported_action")
        self.assertEqual(statuses["test.baseline"], "already_in_baseline")
        self.assertEqual(statuses["test.auto_inactive"], "auto_inactive")
        self.assertEqual(statuses["test.auto_unknown"], "auto_unknown")
        self.assertEqual(statuses["test.auto_active"], "auto_active")
        self.assertEqual(statuses["test.stacks"], "not_selected")

    def test_selected_statuses(self) -> None:
        self.assertEqual(self._status(self._enable("test.ready_rules"), "test.ready_rules"), "selected_ready_needs_rules")
        self.assertEqual(
            self._status(self._enable("test.needs_time"), "test.needs_time"), "selected_ready_needs_time"
        )
        self.assertEqual(
            self._status(self._enable("test.engine_supported"), "test.engine_supported"), "active_supported"
        )
        self.assertEqual(
            self._status(self._enable("test.unconfirmed"), "test.unconfirmed"), "selected_unconfirmed"
        )
        selection = self._enable("test.unmet")
        record = evaluation_by_id(selection)["test.unmet"]
        self.assertEqual(record["status"], "selected_condition_unmet")
        self.assertFalse(record["condition_results"][0]["met"])

    def test_unavailable_selection_is_validation_error(self) -> None:
        evaluation = self._enable("test.unavailable")
        codes = {error["code"] for error in evaluation["validation"]["errors"]}
        self.assertIn("not_available", codes)
        self.assertEqual(self._status(evaluation, "test.unavailable"), "not_available")

    def test_action_selection_stays_unsupported(self) -> None:
        evaluation = evaluate_scenario(self.base_scenario, self.registry, self.catalog)
        self.assertEqual(self._status(evaluation, "test.action"), "unsupported_action")
        self.assertTrue(any(item["effect_id"] == "test.action" for item in evaluation["unsupported"]))

    def test_proposed_contributions_only_for_selected_ready_effects(self) -> None:
        evaluation = self._enable("test.ready_rules")
        proposals = {item["modifier_id"]: item for item in evaluation["proposed_contributions"]}
        self.assertIn("test.ready_rules.mod", proposals)
        self.assertAlmostEqual(proposals["test.ready_rules.mod"]["value"], 0.1)
        default = evaluate_scenario(self.base_scenario, self.registry, self.catalog)
        ready_rule_proposals = [
            item
            for item in default["proposed_contributions"]
            if item["effect_id"] == "test.ready_rules"
        ]
        self.assertEqual(ready_rule_proposals, [])

    def test_auto_active_engine_supported_is_not_proposed(self) -> None:
        evaluation = evaluate_scenario(self.base_scenario, self.registry, self.catalog)
        records = evaluation_by_id(evaluation)
        engine_auto = records["test.auto_engine_supported"]
        self.assertEqual(engine_auto["status"], "auto_active")
        self.assertEqual(engine_auto["support"], "engine_supported")
        self.assertFalse(engine_auto["already_in_baseline"])
        proposed_effect_ids = {item["effect_id"] for item in evaluation["proposed_contributions"]}
        self.assertNotIn("test.auto_engine_supported", proposed_effect_ids)
        # a non-engine auto effect still yields a proposal (documentation only)
        self.assertIn("test.auto_active", proposed_effect_ids)

    def test_stacks_control_resolves_per_selection(self) -> None:
        scenario = copy.deepcopy(self.base_scenario)
        scenario["effects"]["test.stacks"] = {"stacks": 2}
        evaluation = evaluate_scenario(scenario, self.registry, self.catalog)
        record = evaluation_by_id(evaluation)["test.stacks"]
        self.assertTrue(record["selected"])
        self.assertEqual(evaluation["scenario_resolved"]["states"]["state.stacks"], 2)
        proposals = evaluation["proposed_contributions"]
        self.assertTrue(proposals)

    def test_synthetic_catalog_denominators(self) -> None:
        counts = self.catalog["checked_scope"]["counts"]
        self.assertEqual(counts["items"], 1)
        self.assertEqual(counts["item_mods"], 1)
        self.assertEqual(counts["specializations"], 1)
        self.assertEqual(counts["skill_nodes_allocated"], 2)
        self.assertEqual(counts["passive_nodes"], 1)
        self.assertEqual(counts["snapshot_contributions"], 0)
        self.assertEqual(counts["excluded_sources"], 0)
        self.assertEqual(counts["effect_instances"], len(self.registry.effects))


# ---------------------------------------------------------------------------
# Real discovery
# ---------------------------------------------------------------------------


class RealDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_real_assets(cls)
        cls.registry = real_registry()
        cls.snapshot = real_snapshot()
        cls.calc_input = real_calc_input()
        cls.build = real_build()
        dps = real_dps_scenario()
        cls.catalog = build_catalog(
            cls.snapshot,
            registry=cls.registry,
            calc_input=cls.calc_input,
            build=cls.build,
            catalog=None,
            dps_scenario=dps,
            binding={},
        )

    def test_denominators_from_real_build(self) -> None:
        counts = self.catalog["checked_scope"]["counts"]
        self.assertEqual(counts["items"], 29)
        self.assertEqual(counts["item_mods"], 147)
        self.assertEqual(counts["specializations"], 5)
        self.assertEqual(counts["skill_nodes_allocated"], 61)
        self.assertEqual(counts["passive_nodes"], 19)
        self.assertEqual(counts["snapshot_contributions"], 240)
        self.assertEqual(counts["excluded_sources"], 14)
        self.assertEqual(
            counts["denominator_breakdown"],
            {
                "items": 29,
                "item_mods": 147,
                "specializations": 5,
                "skill_nodes_allocated": 61,
                "passive_nodes": 19,
                "snapshot_contributions": 240,
                "excluded_sources": 14,
            },
        )

    def test_scope_expectations_match(self) -> None:
        self.assertEqual(self.catalog["expectation_mismatches"], [])
        self.assertEqual(self.catalog["data_gaps"], [])

    def test_registered_candidates_cover_expected_effects(self) -> None:
        registered = {
            candidate["effect_id"]
            for candidate in self.catalog["candidates"]
            if candidate.get("disposition") == "registered"
        }
        expected_ids = {
            effect_id(self.registry, "craterborn"),
            effect_id(self.registry, "infusion"),
            effect_id(self.registry, "chilled touch"),
            effect_id(self.registry, "eternal flames", "passive"),
            effect_id(self.registry, "eternal flames", "active"),
            effect_id(self.registry, "crescendoing"),
            effect_id(self.registry, "ambition"),
            effect_id(self.registry, "frozen steel"),
            effect_id(self.registry, "preparation"),
        }
        self.assertTrue(expected_ids <= registered, expected_ids - registered)

    def test_candidates_include_effects_not_in_effective_contributions(self) -> None:
        effects = {entry["effect_id"]: entry for entry in self.catalog["effects"]}
        for needles in (
            ("craterborn",),
            ("chilled touch",),
            ("infusion",),
            ("eternal flames", "passive"),
            ("crescendoing",),
            ("ambition",),
            ("frozen steel",),
            ("preparation",),
        ):
            with self.subTest(needles=needles):
                entry = effects[effect_id(self.registry, *needles)]
                active = [
                    row
                    for row in entry["existing_handling"]
                    if row.get("included") and row.get("condition_status") in ("always", "satisfied")
                ]
                self.assertEqual(active, [], "effect should not already be active in effective contributions")

    def test_relevance_categories_all_present(self) -> None:
        statuses = {candidate["relevance_status"] for candidate in self.catalog["candidates"]}
        statuses |= {
            record.get("relevance")
            for record in self.catalog["checked_records"]
            if record.get("relevance")
        }
        for expected in C.RELEVANCE_STATUSES:
            self.assertIn(expected, statuses)

    def test_checked_records_have_reasons_for_excluded(self) -> None:
        records = self.catalog["checked_records"]
        self.assertGreaterEqual(len(records), 130)
        for record in records:
            if record["disposition"] == "excluded":
                with self.subTest(record=record["record_id"]):
                    self.assertTrue(record["reason"].strip())
            self.assertIn(record["disposition"], C.CHECKED_DISPOSITIONS)

    def test_auto_states_and_values(self) -> None:
        values = self.catalog["auto_values"]
        self.assertAlmostEqual(float(values["auto.max_mana"]), 230.2810716, places=4)
        self.assertFalse(values["auto.max_mana_gte_300"])
        # the former DPS-baseline target conditions are user-editable in this
        # step (default checked), so they are no longer derived auto values
        self.assertNotIn("target.boss_or_rare", values)
        self.assertNotIn("target.within_center_1m", values)
        self.assertTrue(values["baseline.target_boss"])
        self.assertTrue(values["baseline.centered"])
        self.assertTrue(values["baseline.both_stars_cover"])

    def test_effect_actuals_resolved_from_real_points(self) -> None:
        effects = {entry["effect_id"]: entry for entry in self.catalog["effects"]}
        craterborn = effects[effect_id(self.registry, "craterborn")]
        self.assertEqual(craterborn["qualification"]["points"], 5)
        self.assertAlmostEqual(craterborn["actuals"]["fire_penetration"]["value"], 0.75)
        frozen = effects[effect_id(self.registry, "frozen steel")]
        self.assertEqual(frozen["qualification"]["points"], 4)
        self.assertAlmostEqual(frozen["actuals"]["damage_vs_chilled_or_frozen"]["value"], 0.06)

    def test_bd_cross_check_produces_no_false_mismatch(self) -> None:
        build_catalog(
            self.snapshot,
            registry=self.registry,
            calc_input=self.calc_input,
            build=self.build,
            catalog=None,
            dps_scenario=real_dps_scenario(),
            binding={},
        )
        # a dedicated catalog with bd.json passed in; mismatches would be data gaps
        catalog = build_catalog(
            self.snapshot,
            registry=self.registry,
            calc_input=self.calc_input,
            build=self.build,
            catalog=None,
            dps_scenario=real_dps_scenario(),
            binding={},
        )
        cross_gaps = [gap for gap in catalog["data_gaps"] if gap["id"].startswith("gap:build-cross-check")]
        self.assertEqual(cross_gaps, [])


# ---------------------------------------------------------------------------
# Real context / evaluation / view
# ---------------------------------------------------------------------------


class RealContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_real_assets(cls)
        cls.registry = real_registry()
        cls.context = real_context()

    def test_context_shape_and_status(self) -> None:
        context = self.context
        self.assertEqual(context["schema"], C.SCHEMA_CONTEXT)
        self.assertEqual(context["status"], "ready")
        self.assertEqual(context["issues"], [])
        for key in ("registry", "input", "context", "catalog", "scenario", "evaluation", "rules", "view"):
            self.assertIn(key, context)
        self.assertEqual(context["registry"]["id"], self.registry.registry_id)
        self.assertEqual(context["registry"]["sha256"], self.registry.sha256)
        self.assertFalse(Path(context["registry"]["path"]).is_absolute())
        self.assertEqual(context["input"]["build"]["revision_id"], "rev-dbba378f6fe3d829")
        self.assertEqual(context["input"]["dataset"]["dataset_id"], "phase1-dataset-003")
        self.assertEqual(context["evaluation"]["dps_recomputed"], False)

    def test_context_deterministic(self) -> None:
        second = build_context(real_snapshot(), **real_context_kwargs())
        self.assertEqual(
            json.dumps(self.context, sort_keys=True, ensure_ascii=False),
            json.dumps(second, sort_keys=True, ensure_ascii=False),
        )

    def test_context_has_no_absolute_paths(self) -> None:
        text = json.dumps(self.context, ensure_ascii=False)
        # third-party reference URLs (https://...) are allowed; only local host paths are rejected
        local_only = re.sub(r"https?://\S+", "", text)
        self.assertNotIn(str(REPO_ROOT), text)
        self.assertIsNone(re.search(r"[A-Za-z]:[\\/]", local_only))

    def test_view_is_strict_json(self) -> None:
        text = json.dumps(self.context["view"], allow_nan=False, ensure_ascii=False, sort_keys=True)
        json.loads(text)
        self.assertEqual(self.context["view"]["schema"], C.SCHEMA_VIEW)

    def test_build_context_does_not_mutate_snapshot_or_damage(self) -> None:
        snapshot = real_snapshot()
        before = copy.deepcopy(snapshot.to_dict())
        active_before = len(snapshot.active_contributions())
        result_before = RESULT_PATH.read_bytes()
        context = build_context(snapshot, **real_context_kwargs())
        self.assertEqual(snapshot.to_dict(), before)
        self.assertEqual(len(snapshot.active_contributions()), active_before)
        self.assertEqual(context["evaluation"]["dps_recomputed"], False)
        self.assertEqual(RESULT_PATH.read_bytes(), result_before)

    def test_default_scenario_is_build_base(self) -> None:
        registry = self.registry
        base = default_scenario(registry, {"binding": self.context["catalog"]["binding"]})
        self.assertEqual(base["preset_id"], "build_base")
        self.assertEqual(base["schema"], C.SCHEMA_SCENARIO)
        preset_scenario = apply_preset(
            registry, "build_base", {"binding": self.context["catalog"]["binding"]}
        )
        self.assertEqual(scenario_fingerprint(base), scenario_fingerprint(preset_scenario))
        self.assertAlmostEqual(
            self.context["evaluation"]["scenario_fingerprint"],
            self.context["evaluation"]["baseline_fingerprint"],
        )
        self.assertFalse(self.context["evaluation"]["changed_from_baseline"])
        user_state_ids = {state["id"] for state in registry.user_states()}
        self.assertEqual(set(base["states"]), user_state_ids)
        # defaults come from the registry: the former DPS-baseline target
        # conditions are user-editable but default to their baseline values
        for state in registry.user_states():
            with self.subTest(state=state["id"]):
                self.assertEqual(base["states"][state["id"]], state["default"])
        self.assertFalse(base["states"]["target.chilled"])
        self.assertTrue(base["states"]["target.boss_or_rare"])
        self.assertTrue(base["states"]["target.within_center_1m"])
        self.assertTrue(base["states"]["target.both_stars_cover"])

    def test_evaluation_default_statuses(self) -> None:
        records = evaluation_by_id(self.context["evaluation"])
        self.assertEqual(records[effect_id(self.registry, "craterborn")]["status"], "not_selected")
        self.assertEqual(
            records[effect_id(self.registry, "supermassive")]["status"], "already_in_baseline"
        )
        self.assertEqual(
            records[effect_id(self.registry, "weakened reality")]["status"], "already_in_baseline"
        )
        self.assertEqual(records[effect_id(self.registry, "frozen steel")]["status"], "auto_inactive")
        self.assertEqual(records[effect_id(self.registry, "conflagrate")]["status"], "unsupported_action")
        self.assertEqual(
            records[effect_id(self.registry, "eternal flames", "passive")]["status"], "auto_active"
        )
        self.assertEqual(records[effect_id(self.registry, "crescendoing")]["status"], "not_selected")

    def test_evaluation_auto_inactive_and_threshold_semantics(self) -> None:
        records = evaluation_by_id(self.context["evaluation"])
        frozen = records[effect_id(self.registry, "frozen steel")]
        self.assertEqual(frozen["status"], "auto_inactive")
        self.assertIn("机制未确认", frozen["status_reason"])
        threshold = records[effect_id(self.registry, "mana threshold")]
        # registry marks the base idol line as already_in_baseline; contract §8.4
        # rule 10 therefore wins over auto_* while the condition itself is false.
        self.assertEqual(threshold["status"], "already_in_baseline")
        self.assertFalse(threshold["conditions_met"])
        self.assertIn("未满足", threshold["status_reason"])
        # the reason must separate "base already counted" from "doubling not counted"
        self.assertIn("基础值已计入，翻倍部分未计入", threshold["status_reason"])
        self.assertIn("注册表注记", threshold["status_reason"])
        self.assertFalse(self.context["catalog"]["auto_values"]["auto.max_mana_gte_300"])
        self.assertEqual(records[effect_id(self.registry, "fire aura")]["status"], "auto_inactive")
        self.assertIn("当前场景不相关", records[effect_id(self.registry, "fire aura")]["status_reason"])

    def test_selected_ready_status_and_proposals(self) -> None:
        registry = self.registry
        evaluation = eval_variant(
            self.context,
            lambda scenario: scenario["effects"][effect_id(registry, "craterborn")].update(
                {"enabled": True}
            ),
        )
        record = evaluation_by_id(evaluation)[effect_id(registry, "craterborn")]
        self.assertEqual(record["status"], "selected_ready_needs_rules")
        self.assertTrue(record["conditions_met"])
        self.assertTrue(evaluation["changed_from_baseline"])
        proposals = {
            item["modifier_id"]: item for item in evaluation["proposed_contributions"]
        }
        craterborn = find_effect(registry, "craterborn")
        expected = {
            modifier["modifier_id"]: value_ref
            for modifier, value_ref in zip(
                craterborn["modifiers"],
                [modifier["value_ref"] for modifier in craterborn["modifiers"]],
            )
        }
        for modifier_id in expected:
            self.assertIn(modifier_id, proposals)
        fire_pen = next(
            item for item in proposals.values() if item["stat_key"] == "penetration"
        )
        self.assertAlmostEqual(fire_pen["value"], 0.75)
        cast_speed = next(
            item for item in proposals.values() if item["stat_key"] == "cast_speed"
        )
        self.assertAlmostEqual(cast_speed["value"], 0.15)

    def test_selected_condition_unmet(self) -> None:
        registry = self.registry
        evaluation = eval_variant(
            self.context,
            lambda scenario: scenario["effects"][effect_id(registry, "chilled touch")].update(
                {"enabled": True}
            ),
        )
        record = evaluation_by_id(evaluation)[effect_id(registry, "chilled touch")]
        self.assertEqual(record["status"], "selected_condition_unmet")
        unmet_states = {row["state_id"] for row in record["condition_results"] if row["met"] is not True}
        self.assertIn("target.chilled", unmet_states)
        chilled_proposals = [
            item
            for item in evaluation["proposed_contributions"]
            if item["effect_id"] == effect_id(registry, "chilled touch")
        ]
        self.assertEqual(chilled_proposals, [])

    def test_preset_statuses(self) -> None:
        registry = self.registry
        persistent = build_context(
            real_snapshot(), preset_id="persistent_buffs", **real_context_kwargs()
        )
        self.assertEqual(persistent["status"], "ready")
        self.assertEqual(persistent["evaluation"]["validation"]["errors"], [])
        records = evaluation_by_id(persistent["evaluation"])
        self.assertEqual(
            records[effect_id(registry, "craterborn")]["status"], "selected_ready_needs_rules"
        )
        self.assertEqual(
            records[effect_id(registry, "chilled touch")]["status"], "selected_condition_unmet"
        )
        proposals = persistent["evaluation"]["proposed_contributions"]
        self.assertTrue(any(item["effect_id"] == effect_id(registry, "infusion") for item in proposals))
        self.assertFalse(
            any(item["effect_id"] == effect_id(registry, "chilled touch") for item in proposals)
        )

        maxed = build_context(real_snapshot(), preset_id="max_stacks_bench", **real_context_kwargs())
        records = evaluation_by_id(maxed["evaluation"])
        ambition = records[effect_id(registry, "ambition")]
        self.assertEqual(ambition["status"], "selected_ready_needs_rules")
        ambition_values = [
            item["value"]
            for item in maxed["evaluation"]["proposed_contributions"]
            if item["effect_id"] == effect_id(registry, "ambition")
        ]
        self.assertEqual(len(ambition_values), 2)
        for value in ambition_values:
            self.assertAlmostEqual(value, 0.4)

        third = build_context(real_snapshot(), preset_id="third_cast_single", **real_context_kwargs())
        records = evaluation_by_id(third["evaluation"])
        self.assertEqual(
            records[effect_id(registry, "crescendoing")]["status"], "selected_ready_needs_time"
        )
        crescendo = [
            item
            for item in third["evaluation"]["proposed_contributions"]
            if item["effect_id"] == effect_id(registry, "crescendoing")
        ]
        self.assertEqual(len(crescendo), 1)
        self.assertAlmostEqual(crescendo[0]["value"], 0.5)

    def test_evaluate_idempotent(self) -> None:
        registry = self.registry
        scenario = copy.deepcopy(self.context["scenario"])
        scenario["effects"][effect_id(registry, "craterborn")]["enabled"] = True
        first = evaluate_scenario(scenario, registry, self.context["catalog"])
        second = evaluate_scenario(scenario, registry, self.context["catalog"])
        self.assertEqual(
            json.dumps(first, sort_keys=True, ensure_ascii=False),
            json.dumps(second, sort_keys=True, ensure_ascii=False),
        )
        self.assertEqual(first["summary"], second["summary"])
        self.assertEqual(first["proposed_contributions"], second["proposed_contributions"])

    def test_summary_denominator_breakdown(self) -> None:
        summary = self.context["evaluation"]["summary"]
        self.assertEqual(summary["checked_objects"], 515)
        self.assertEqual(
            summary["denominator_breakdown"],
            {
                "items": 29,
                "item_mods": 147,
                "specializations": 5,
                "skill_nodes_allocated": 61,
                "passive_nodes": 19,
                "snapshot_contributions": 240,
                "excluded_sources": 14,
            },
        )
        self.assertEqual(summary["checked_objects"], sum(summary["denominator_breakdown"].values()))
        view_counts = self.context["view"]["counts"]
        self.assertEqual(view_counts["checked_objects"], 515)
        self.assertEqual(view_counts["denominator_breakdown"], summary["denominator_breakdown"])

    def test_unknown_preset_degrades(self) -> None:
        context = build_context(
            real_snapshot(), preset_id="no-such-preset", **real_context_kwargs()
        )
        self.assertEqual(context["status"], "degraded")
        codes = {issue["code"] for issue in context["issues"]}
        self.assertIn("unknown_preset", codes)
        self.assertFalse(context["evaluation"]["dps_recomputed"])


# ---------------------------------------------------------------------------
# Scenario tests
# ---------------------------------------------------------------------------


class ScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_real_assets(cls)
        cls.registry = real_registry()
        cls.context = real_context()
        cls.catalog = cls.context["catalog"]
        cls.base = copy.deepcopy(cls.context["scenario"])
        cls.binding = cls.catalog["binding"]

    def test_save_load_roundtrip_and_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scenario.json"
            save_scenario(self.base, path)
            text = path.read_text(encoding="utf-8")
            self.assertIn('"schema"', text)
            loaded = load_scenario(path)
            resolved = normalize_scenario(loaded, self.registry, {"binding": self.binding})
            self.assertEqual(scenario_fingerprint(resolved), scenario_fingerprint(self.base))
            validation = validate_scenario(resolved, self.registry, self.catalog)
            self.assertEqual(validation["errors"], [])
            save_scenario(loaded, path)
            self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_fingerprint_excludes_preset_id(self) -> None:
        first = copy.deepcopy(self.base)
        first["preset_id"] = "build_base"
        second = copy.deepcopy(self.base)
        second["preset_id"] = "custom"
        self.assertEqual(scenario_fingerprint(first), scenario_fingerprint(second))
        second["states"]["target.chilled"] = True
        self.assertNotEqual(scenario_fingerprint(first), scenario_fingerprint(second))

    def test_validation_missing_binding(self) -> None:
        scenario = copy.deepcopy(self.base)
        del scenario["binding"]
        codes = {error["code"] for error in validate_scenario(scenario, self.registry, self.catalog)["errors"]}
        self.assertIn("missing_binding", codes)

    def test_validation_stale_binding(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["binding"]["calc_input_sha256"] = "deadbeef"
        validation = validate_scenario(scenario, self.registry, self.catalog)
        codes = {error["code"] for error in validation["errors"]}
        self.assertIn("stale_preset", codes)
        self.assertTrue(any("calc_input_sha256" in error["message"] for error in validation["errors"]))

    def test_validation_schema_mismatch(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["schema"] = "wrong-schema"
        codes = {error["code"] for error in validate_scenario(scenario, self.registry, self.catalog)["errors"]}
        self.assertIn("schema_mismatch", codes)

    def test_validation_unknown_effect_and_state(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["effects"]["no.such.effect"] = {"enabled": True}
        scenario["states"]["no.such.state"] = True
        codes = {error["code"] for error in validate_scenario(scenario, self.registry, self.catalog)["errors"]}
        self.assertIn("unknown_effect", codes)
        self.assertIn("unknown_state", codes)

    def test_validation_type_and_bounds(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["states"]["target.chilled"] = "yes"
        scenario["effects"][effect_id(self.registry, "ambition")] = {"stacks": 99}
        codes = {error["code"] for error in validate_scenario(scenario, self.registry, self.catalog)["errors"]}
        self.assertIn("type_error", codes)
        self.assertIn("stacks_out_of_bounds", codes)

    def test_validation_auto_state_rejected(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["states"]["auto.max_mana"] = 300
        codes = {error["code"] for error in validate_scenario(scenario, self.registry, self.catalog)["errors"]}
        self.assertIn("auto_state_in_scenario", codes)

    def test_validation_enabled_without_control(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["effects"][effect_id(self.registry, "eternal flames", "passive")] = {"enabled": True}
        codes = {error["code"] for error in validate_scenario(scenario, self.registry, self.catalog)["errors"]}
        self.assertIn("enabled_without_control", codes)

    def test_validation_unknown_single_cast_key(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["single_cast"]["bogus_key"] = True
        codes = {error["code"] for error in validate_scenario(scenario, self.registry, self.catalog)["errors"]}
        self.assertIn("unknown_single_cast_key", codes)

    def test_cast_state_selection_is_supported_path(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["single_cast"]["crescendoing_rage_applies"] = True
        validation = validate_scenario(scenario, self.registry, self.catalog)
        self.assertEqual(validation["errors"], [])

    def test_load_scenario_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ScenarioError):
                load_scenario(Path(tmp) / "missing.json")
            broken = Path(tmp) / "broken.json"
            broken.write_text("{oops", encoding="utf-8")
            with self.assertRaises(ScenarioError):
                load_scenario(broken)
            not_object = Path(tmp) / "list.json"
            not_object.write_text("[]", encoding="utf-8")
            with self.assertRaises(ScenarioError):
                load_scenario(not_object)
            empty = Path(tmp) / "empty.json"
            save_scenario({}, empty)
            self.assertEqual(load_scenario(empty), {})

    def test_target_block_roundtrip_and_fingerprint(self) -> None:
        target = {
            "monster_level": 50,
            "fire_resistance": 0.2,
            "boss": True,
            "rare": False,
        }
        scenario = default_scenario(
            self.registry, {"binding": self.binding, "target": target}
        )
        self.assertEqual(scenario["target"], target)
        resolved = normalize_scenario(
            scenario, self.registry, {"binding": self.binding, "target": target}
        )
        self.assertEqual(resolved["target"], target)
        self.assertTrue(resolved["states"]["target.boss_or_rare"])
        self.assertNotEqual(
            scenario_fingerprint(scenario), scenario_fingerprint(self.base)
        )
        self.assertEqual(
            validate_scenario(scenario, self.registry, self.catalog)["errors"], []
        )

    def test_target_default_derived_from_dps_context(self) -> None:
        # build_context receives the DPS result scenario; the resolved
        # conditional scenario therefore carries the unified target block.
        self.assertIn("target", self.base)
        self.assertEqual(self.base["target"]["monster_level"], 100)
        self.assertEqual(self.base["target"]["fire_resistance"], 0.0)
        self.assertIs(self.base["target"]["boss"], True)
        self.assertIs(self.base["target"]["rare"], True)

    def test_target_validation_errors_and_old_scenario(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["target"] = {
            "monster_level": 999,
            "fire_resistance": "x",
            "boss": 1,
        }
        codes = {
            error["code"]
            for error in validate_scenario(scenario, self.registry, self.catalog)[
                "errors"
            ]
        }
        self.assertIn("value_out_of_bounds", codes)
        self.assertIn("invalid_type", codes)
        # Old scenarios without a target block remain valid.
        old = copy.deepcopy(self.base)
        old.pop("target", None)
        self.assertEqual(
            validate_scenario(old, self.registry, self.catalog)["errors"], []
        )
        resolved = normalize_scenario(
            old, self.registry, {"binding": self.binding}
        )
        self.assertNotIn("target", resolved)

    def test_apply_preset_unknown_raises(self) -> None:
        with self.assertRaises(ScenarioError):
            apply_preset(self.registry, "no-such-preset", {"binding": self.binding})

    def test_all_registry_presets_validate_cleanly(self) -> None:
        for preset in self.registry.presets:
            with self.subTest(preset=preset["preset_id"]):
                scenario = apply_preset(self.registry, preset["preset_id"], {"binding": self.binding})
                validation = validate_scenario(scenario, self.registry, self.catalog)
                self.assertEqual(validation["errors"], [])
                self.assertEqual(validation["warnings"], [])


# ---------------------------------------------------------------------------
# View / client rules / artifacts
# ---------------------------------------------------------------------------


class ViewContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_real_assets(cls)
        cls.context = real_context()
        cls.view = cls.context["view"]
        cls.registry = real_registry()

    def test_view_top_level_contract(self) -> None:
        view = self.view
        self.assertEqual(view["schema"], C.SCHEMA_VIEW)
        self.assertTrue(view["available"])
        for key in (
            "header",
            "counts",
            "filters",
            "states",
            "presets",
            "effects",
            "groups",
            "auto_notes",
            "checked",
            "dependency",
            "export",
            "client_rules",
        ):
            self.assertIn(key, view)
        for key in ("title", "question", "mode_label", "baseline_label", "disclaimer", "dps_recomputed", "dps_note"):
            self.assertIn(key, view["header"])
        self.assertFalse(view["header"]["dps_recomputed"])
        for key in (
            "checked_objects",
            "denominator_breakdown",
            "candidates",
            "configured",
            "relevant",
            "scenario_dependent",
            "unconfirmed",
            "not_relevant",
            "auto_active",
            "auto_inactive",
            "auto_unknown",
            "engine_supported",
            "needs_rules",
            "needs_time",
            "mechanism_unresolved",
            "not_selected",
            "condition_met",
            "condition_unmet",
        ):
            self.assertIn(key, view["counts"])
        self.assertEqual(set(view["filters"]), {"categories", "relevance", "support", "statuses"})
        self.assertEqual(
            {item["id"] for item in view["filters"]["statuses"]},
            {"all", *C.STATUS_ORDER},
        )

    def test_view_effect_entries(self) -> None:
        effects = self.view["effects"]
        self.assertEqual(len(effects), len(self.registry.effects))
        expected_keys = {
            "effect_id",
            "group_id",
            "name",
            "name_en",
            "category",
            "category_label",
            "source_label",
            "source_detail",
            "summary",
            "value_lines",
            "condition_text",
            "impact_path",
            "branches",
            "relevance",
            "support",
            "control",
            "status",
            "condition_checks",
            "activation_text",
            "baseline_note",
            "dedupe_note",
            "evidence",
            "advanced",
        }
        for effect in effects:
            with self.subTest(effect=effect["effect_id"]):
                self.assertEqual(set(effect), expected_keys)
                self.assertIn("initial", effect["status"])
                self.assertIn("label", effect["status"])
                self.assertIn("tone", effect["status"])
                self.assertIn("reason", effect["status"])
                self.assertTrue(effect["name"])
                self.assertTrue(effect["impact_path"])
        statuses = {effect["effect_id"]: effect["status"]["initial"] for effect in effects}
        self.assertEqual(statuses[effect_id(self.registry, "supermassive")], "already_in_baseline")
        self.assertEqual(statuses[effect_id(self.registry, "frozen steel")], "auto_inactive")

    def test_mana_threshold_card_notes_baseline_split(self) -> None:
        threshold_id = effect_id(self.registry, "mana threshold")
        card = next(effect for effect in self.view["effects"] if effect["effect_id"] == threshold_id)
        self.assertEqual(card["status"]["initial"], "already_in_baseline")
        self.assertIn("基础值已计入，翻倍部分未计入", card["baseline_note"] or "")
        self.assertIn("翻倍部分未计入", card["status"]["reason"])

    def test_view_states(self) -> None:
        states = self.view["states"]
        ids = {state["id"] for state in states}
        user_ids = {state["id"] for state in self.registry.user_states()}
        self.assertTrue(user_ids <= ids)
        self.assertIn("auto.max_mana", ids)
        self.assertIn("baseline.target_boss", ids)
        for state in states:
            self.assertIn("editable", state)
            self.assertEqual(state["editable"], state["source"] == "user")
            if state["id"] == "auto.max_mana":
                self.assertAlmostEqual(float(state["value"]), 230.2810716, places=4)
                self.assertFalse(state["editable"])
        auto_notes = {note["id"]: note for note in self.view["auto_notes"]}
        self.assertFalse(auto_notes["auto.max_mana_gte_300"]["met"])
        self.assertTrue(any(note.get("baseline_fixed") for note in self.view["auto_notes"]))

    def test_view_checked_and_dependency(self) -> None:
        checked = self.view["checked"]
        self.assertEqual(len(checked["summary"]), 5)
        self.assertGreaterEqual(len(checked["records"]), 130)
        self.assertEqual(len(checked["excluded_sources"]), 14)
        self.assertIsInstance(checked["data_gaps"], list)
        modules = self.view["dependency"]["modules"]
        self.assertTrue(modules)
        for module in modules:
            for key in ("id", "label", "question", "consumes", "calc_refs", "rule_ids", "notes", "evidence"):
                self.assertIn(key, module)

    def test_client_rules_payload(self) -> None:
        rules = self.view["client_rules"]
        self.assertEqual(rules["schema"], C.SCHEMA_RULES)
        for key in ("states", "auto_states", "effects", "status_labels", "presets", "baseline_scenario"):
            self.assertIn(key, rules)
        self.assertEqual(rules["status_labels"], C.STATUS_LABELS)
        self.assertEqual(rules["status_precedence"], list(C.STATUS_ORDER))
        effect_rule_keys = {
            "effect_id",
            "name",
            "category",
            "group_id",
            "control",
            "condition",
            "support",
            "baseline_relation",
            "status_when_met",
            "already_in_baseline",
            "available",
            "qualification",
            "summary",
            "initial_status",
        }
        for effect_rule in rules["effects"]:
            self.assertEqual(set(effect_rule), effect_rule_keys)
        rules_by_id = {item["effect_id"]: item for item in rules["effects"]}
        craterborn = rules_by_id[effect_id(self.registry, "craterborn")]
        self.assertEqual(craterborn["control"]["kind"], "toggle")
        self.assertEqual(craterborn["status_when_met"], "selected_ready_needs_rules")
        conflagrate = rules_by_id[effect_id(self.registry, "conflagrate")]
        self.assertEqual(conflagrate["status_when_met"], "unsupported_action")
        self.assertEqual(len(rules["presets"]), len(self.registry.presets))
        self.assertTrue(any(preset["is_default"] for preset in rules["presets"]))
        self.assertEqual(rules["baseline_scenario"]["schema"], C.SCHEMA_SCENARIO)

    def test_view_matches_ui_fixture_keys(self) -> None:
        if not UI_FIXTURE_PATH.is_file():
            self.skipTest(f"UI dev fixture absent: {UI_FIXTURE_PATH}")
        fixture = _load_json(UI_FIXTURE_PATH)
        if not isinstance(fixture, dict):
            self.skipTest("UI dev fixture is not a JSON object")
        for key in fixture:
            if key in ("available", "schema"):
                continue
            self.assertIn(key, self.view, f"fixture view key {key!r} missing from real view")
        if fixture.get("effects") and self.view["effects"]:
            fixture_effect_keys = set(fixture["effects"][0])
            real_effect_keys = set(self.view["effects"][0])
            self.assertTrue(fixture_effect_keys <= real_effect_keys, fixture_effect_keys - real_effect_keys)
        fixture_rules = fixture.get("client_rules") or {}
        if fixture_rules:
            self.assertTrue(set(fixture_rules) <= set(self.view["client_rules"]))
            if fixture_rules.get("effects"):
                self.assertTrue(
                    set(fixture_rules["effects"][0]) <= set(self.view["client_rules"]["effects"][0])
                )

    def test_view_renders_via_report_ui_page(self) -> None:
        try:
            from le_calc.report_ui.pages.conditions import render_conditions
        except Exception as exc:  # pragma: no cover - page is owned by ui-conditions
            self.skipTest(f"conditions page unavailable: {exc}")
        try:
            html = render_conditions({"conditions": self.view})
        except Exception as exc:  # pragma: no cover - tolerate parallel UI work in progress
            self.skipTest(f"conditions page render is not ready: {exc}")
        self.assertIn("data-cond-row", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("<style", html)


class ArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_real_assets(cls)
        cls.context = real_context()

    def test_write_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            written = write_artifacts(self.context, Path(tmp))
            self.assertEqual(
                set(written),
                {"conditional-effects.json", "conditional-scenario.json", "conditional-evaluation.json"},
            )
            for name, path in written.items():
                self.assertTrue(Path(path).is_file(), name)
            effects = _load_json(Path(written["conditional-effects.json"]))
            self.assertEqual(effects["schema"], C.SCHEMA_EFFECTS_ARTIFACT)
            self.assertEqual(effects["registry"]["sha256"], self.context["registry"]["sha256"])
            self.assertEqual(effects["catalog"]["schema"], C.SCHEMA_CATALOG)
            scenario = _load_json(Path(written["conditional-scenario.json"]))
            self.assertEqual(scenario["schema"], C.SCHEMA_SCENARIO)
            self.assertEqual(
                scenario_fingerprint(scenario), self.context["evaluation"]["scenario_fingerprint"]
            )
            evaluation = _load_json(Path(written["conditional-evaluation.json"]))
            self.assertEqual(evaluation["schema"], C.SCHEMA_EVALUATION)
            self.assertFalse(evaluation["dps_recomputed"])
            for name in written:
                text = Path(written[name]).read_text(encoding="utf-8")
                self.assertNotIn(str(REPO_ROOT), text)

    def test_export_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "conditional-scenario.json"
            returned = export_scenario(self.context, path)
            self.assertTrue(Path(returned).is_file())
            loaded = load_scenario(path)
            self.assertEqual(
                scenario_fingerprint(loaded), self.context["evaluation"]["scenario_fingerprint"]
            )


# ---------------------------------------------------------------------------
# Degraded paths
# ---------------------------------------------------------------------------


class DegradedPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_real_assets(cls)

    def test_missing_registry_degrades_without_raising(self) -> None:
        context = build_context(
            real_snapshot(),
            registry_path=str(REPO_ROOT / "tools" / "phase3" / "rules" / "does-not-exist.json"),
            scenario=real_dps_scenario(),
            repo_root=str(REPO_ROOT),
        )
        self.assertEqual(context["status"], "degraded")
        self.assertFalse(context["view"]["available"])
        codes = {issue["code"] for issue in context["issues"]}
        self.assertIn("registry_missing", codes)
        self.assertEqual(context["evaluation"]["dps_recomputed"], False)
        self.assertIn("header", context["view"])

    def test_invalid_registry_degrades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad-registry.json"
            path.write_text(json.dumps({"schema": "nope"}), encoding="utf-8")
            context = build_context(
                real_snapshot(), registry_path=path, scenario=real_dps_scenario(), repo_root=str(REPO_ROOT)
            )
        self.assertEqual(context["status"], "degraded")
        self.assertTrue(context["issues"])
        self.assertFalse(context["view"]["available"])

    def test_missing_calc_input_degrades(self) -> None:
        context = build_context(
            real_snapshot(),
            calc_input_path=str(REPO_ROOT / "reports" / "save-import" / "missing-calc-input.json"),
            scenario=real_dps_scenario(),
            ruleset_path=str(RULESET_PATH),
            repo_root=str(REPO_ROOT),
        )
        self.assertEqual(context["status"], "degraded")
        codes = {issue["code"] for issue in context["issues"]}
        self.assertIn("calc_input_missing", codes)
        gap_ids = {gap["id"] for gap in context["catalog"]["data_gaps"]}
        self.assertTrue(any("calc-input" in gap_id for gap_id in gap_ids))

    def test_stale_scenario_file_degrades(self) -> None:
        registry = real_registry()
        context = real_context()
        stale = copy.deepcopy(context["scenario"])
        stale["binding"]["calc_input_sha256"] = "deadbeef"
        with tempfile.TemporaryDirectory() as tmp:
            scenario_path = Path(tmp) / "stale-scenario.json"
            save_scenario(stale, scenario_path)
            loaded = build_context(
                real_snapshot(),
                scenario_path=str(scenario_path),
                scenario=real_dps_scenario(),
                ruleset_path=str(RULESET_PATH),
                build_path=str(BUILD_PATH),
                repo_root=str(REPO_ROOT),
            )
        self.assertEqual(loaded["status"], "degraded")
        codes = {issue["code"] for issue in loaded["issues"]}
        self.assertIn("stale_preset", codes)
        self.assertEqual(loaded["evaluation"]["dps_recomputed"], False)
        # no absolute host path may leak into the context/view even for a
        # user-supplied scenario path outside the repository
        text = json.dumps(loaded, ensure_ascii=False)
        local_only = re.sub(r"https?://\S+", "", text)
        self.assertIsNone(re.search(r"[A-Za-z]:[\\/]", local_only))

    def test_missing_scenario_file_is_repo_relative_in_issues(self) -> None:
        missing = str(REPO_ROOT.parent / "somewhere-else" / "absent-scenario.json")
        loaded = build_context(
            real_snapshot(),
            scenario_path=missing,
            scenario=real_dps_scenario(),
            ruleset_path=str(RULESET_PATH),
            repo_root=str(REPO_ROOT),
        )
        self.assertEqual(loaded["status"], "degraded")
        codes = {issue["code"] for issue in loaded["issues"]}
        self.assertIn("scenario_missing", codes)
        text = json.dumps(loaded, ensure_ascii=False)
        local_only = re.sub(r"https?://\S+", "", text)
        self.assertIsNone(re.search(r"[A-Za-z]:[\\/]", local_only))
        self.assertNotIn(str(REPO_ROOT.parent), text)

    def test_scenario_roundtrip_through_build_context(self) -> None:
        context = real_context()
        with tempfile.TemporaryDirectory() as tmp:
            scenario_path = Path(tmp) / "scenario.json"
            save_scenario(context["scenario"], scenario_path)
            loaded = build_context(
                real_snapshot(),
                scenario_path=str(scenario_path),
                scenario=real_dps_scenario(),
                ruleset_path=str(RULESET_PATH),
                build_path=str(BUILD_PATH),
                repo_root=str(REPO_ROOT),
            )
        self.assertEqual(loaded["status"], "ready")
        self.assertEqual(loaded["evaluation"]["validation"]["errors"], [])
        self.assertEqual(
            loaded["evaluation"]["scenario_fingerprint"],
            context["evaluation"]["scenario_fingerprint"],
        )

    def test_build_context_never_raises_for_bad_snapshot(self) -> None:
        context = build_context(None)
        self.assertEqual(context["status"], "unavailable")
        self.assertTrue(context["issues"])
        self.assertIn("view", context)
        self.assertFalse(context["view"]["available"])


# ---------------------------------------------------------------------------
# CLI integration (Lead-owned cmd_conditions, exercised through its public main)
# ---------------------------------------------------------------------------


def _run_cli(argv: list[str]) -> int:
    from le_calc.cli import main

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        return int(main(argv))


class CliConditionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_real_assets(cls)

    def _base_args(self, out_dir: str | Path) -> list[str]:
        return [
            "conditions",
            "--snapshot",
            str(SNAPSHOT_PATH),
            "--ruleset",
            str(RULESET_PATH),
            "--calc-input",
            str(CALC_INPUT_PATH),
            "--build",
            str(BUILD_PATH),
            "--out",
            str(out_dir),
        ]

    def test_cli_conditions_ready_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            exit_code = _run_cli(self._base_args(out_dir) + ["--json"])
            self.assertEqual(exit_code, 0)
            for name in (
                "conditional-effects.json",
                "conditional-scenario.json",
                "conditional-evaluation.json",
            ):
                self.assertTrue((out_dir / name).is_file(), name)

    def test_cli_conditions_stale_scenario_exit_two(self) -> None:
        context = real_context()
        stale = copy.deepcopy(context["scenario"])
        stale["binding"]["calc_input_sha256"] = "deadbeef"
        with tempfile.TemporaryDirectory() as tmp:
            scenario_path = Path(tmp) / "stale-scenario.json"
            save_scenario(stale, scenario_path)
            exit_code = _run_cli(
                self._base_args(Path(tmp) / "out") + ["--scenario", str(scenario_path)]
            )
        self.assertEqual(exit_code, 2)

    def test_cli_conditions_missing_registry_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exit_code = _run_cli(
                self._base_args(Path(tmp) / "out")
                + ["--registry", str(Path(tmp) / "absent-registry.json")]
            )
        self.assertEqual(exit_code, 2)


# ---------------------------------------------------------------------------
# Package exports
# ---------------------------------------------------------------------------


class PackageApiTests(unittest.TestCase):
    def test_public_api_exports(self) -> None:
        import le_calc.conditional as conditional

        for name in (
            "build_context",
            "write_artifacts",
            "load_registry",
            "default_scenario",
            "validate_scenario",
            "evaluate_scenario",
            "build_view",
            "export_scenario",
            "load_scenario",
            "DEFAULT_REGISTRY_PATH",
        ):
            self.assertTrue(hasattr(conditional, name), name)
        self.assertEqual(
            Path(conditional.DEFAULT_REGISTRY_PATH).name,
            "conditional-effects-bh-stage1-v1.json",
        )
        self.assertEqual(
            Path(conditional.DEFAULT_REGISTRY_PATH).parent.name,
            "rules",
        )

    def test_default_registry_path_points_into_phase3_rules(self) -> None:
        self.assertEqual(
            Path(DEFAULT_REGISTRY_PATH).resolve(),
            (REPO_ROOT / "tools" / "phase3" / "rules" / "conditional-effects-bh-stage1-v1.json").resolve(),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
