"""Hit-model input adapter: Flame Reave (fr11mv) case -> :class:`model.Snapshot`.

Owner: fr-engine (task-2).  Consumers: ``hit_damage``/``hit_simulate``/
``hit_buffs``/``hit_report``/``cli`` (hit-snapshot, hit-run).

What it does
------------
Reads exactly the frozen case inputs and returns one *static* snapshot:

* ``case.json`` (``le-case-v1``): paths, build binding, case-level structure
  expectations, main skill id, monster level.
* ``calculation-input-v1`` (the frozen calculator export) — the source of truth
  for the pinned build revision and for every equipment/idol/blessing/passive
  effect line.
* ``data/current.json`` -> release manifest -> ``data.sqlite`` (read-only
  phase-1 catalog): class base attributes, tree nodes/effects/effect values,
  skill components, item-base ``attackRate``/``implicits``, quest attribute
  points.
* the published build revision (``revisions/<calc_input.revision_id>/build.json``)
  for the ``build_meta`` fingerprint chain (same shape as the Black Hole
  snapshot, see docs/architecture.md §7).

The adapter never writes anything, never touches the network and never mutates
the catalog.  Fatal contract violations raise :class:`HitInputError`.
Non-fatal observations are recorded in ``snapshot.issues``.

Output contract (DESIGN §5.1)
-----------------------------
Top level is :class:`le_calc.model.Snapshot`::

    build_meta, character, attributes, resistances, derived, issues,
    excluded_sources, contributions

``derived`` adds the hit-model blocks::

    model ("hit"), skill, weapon, hit_count, node_effects, applied_states,
    mana, attribute_detail, unique_descriptions, excluded_non_dps

Exit points
-----------
``build_snapshot(case, *, calc_input_path=None, dataset_path=None,
repo_root=None) -> model.Snapshot``
    ``case`` may be a path (str/Path) or an already parsed dict.
``build_snapshot_dict(...) -> dict``
``dump_snapshot(snapshot, path) -> Path`` / ``snapshot_json(snapshot) -> str``
``load_case(case, *, repo_root=None) -> dict``
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from . import model
from . import contribution_mapping as generic_mapping
from .catalog import Catalog, REPO_ROOT, cached_file_sha256, file_sha256
from .condition_codes import DEFAULT_HIT_STATES
from .monster_dr import dr_for_level

HIT_MODEL = "hit"
CALC_SCHEMA_VERSION = "calculation-input-v1"
DEFAULT_DATASET_CURRENT = Path("data") / "current.json"

#: 双持附加伤害合成策略（fr-hit-facts R2 candidates）。
#: "sum" = candidate A（参考运行时口径：两把武器各自附加伤害都进入池）；
#: "average" = candidate B（每次命中的平均）。
WEAPON_ADDED_POLICIES = ("sum", "average")
DEFAULT_WEAPON_ADDED_POLICY = "sum"

#: passive stats whose planner semantics are still unresolved (fr-hit-facts OQ6)
#: (node_id, stat_index) -> exclusion reason
# user ruling 2026-11-07: include the OQ6 crit passives in the crit chance pool.
# (The per-attribute scaling formula is still unverified; snapshot values are used.)
PASSIVE_UNRESOLVED_SEMANTICS: dict[tuple[str, int], str] = {}

# ---------------------------------------------------------------------------
# coreDB property ids (names confirmed against dataset `stats` table)
# ---------------------------------------------------------------------------

PROP_DAMAGE = 0
PROP_ATTACK_SPEED = 2
PROP_CAST_SPEED = 3
PROP_CRIT_CHANCE = 4
PROP_CRIT_MULTIPLIER = 5
PROP_DAMAGE_TAKEN = 6
PROP_HEALTH = 7
PROP_MANA = 8
PROP_MANA_REGEN = 18
PROP_STRENGTH = 19
PROP_VITALITY = 20
PROP_INTELLIGENCE = 21
PROP_DEXTERITY = 22
PROP_ATTUNEMENT = 23
PROP_WARD = 39
PROP_WARD_DECAY = 119
PROP_ALL_ATTRIBUTES = 46
PROP_PENETRATION = 59
PROP_MANA_COST = 66
PROP_MANA_EFFICIENCY = 69
PROP_COOLDOWN_RECOVERY = 70
PROP_ADAPTIVE_SPELL_DAMAGE = 41
PROP_OFFHAND_STAT_EFFECT = 98

ATTRIBUTE_PROPERTIES: dict[int, str] = {
    PROP_STRENGTH: "strength",
    PROP_VITALITY: "vitality",
    PROP_INTELLIGENCE: "intelligence",
    PROP_DEXTERITY: "dexterity",
    PROP_ATTUNEMENT: "attunement",
}

#: property -> stat label used when the bd.json stat name is unavailable
PROPERTY_LABELS: dict[int, str] = {
    0: "Damage",
    2: "Attack Speed",
    3: "Cast Speed",
    4: "Critical Strike Chance",
    5: "Critical Strike Multiplier",
    6: "Damage Taken",
    7: "Health",
    8: "Mana",
    18: "Mana Regen / Mana Gain",
    19: "Strength",
    20: "Vitality",
    21: "Intelligence",
    22: "Dexterity",
    23: "Attunement",
    39: "Ward Gained",
    41: "Adaptive Spell Damage",
    46: "All Attributes",
    59: "Penetration",
    66: "Mana Cost",
    69: "Mana Efficiency",
    70: "Cooldown Recovery Speed",
    75: "Endurance",
    92: "Ward Per Second",
    119: "Ward Decay Threshold",
    -1: "unmapped property (-1)",
}

# tags (model.AT_BITS)
TAG_PHYSICAL = 1
TAG_FIRE = 8
TAG_ELEMENTAL = 128
TAG_SPELL = 256
TAG_MELEE = 512
TAG_DOT = 4096
TAG_MINION = 8192
TAG_HIT = 8388608

DAMAGE_TYPE_TAGS: tuple[str, ...] = (
    "Physical", "Fire", "Cold", "Lightning", "Void", "Necrotic", "Poison",
)
ELEMENTAL_TAGS = frozenset({"Fire", "Cold", "Lightning"})

RESISTANCE_BY_NAME: dict[str, str] = {
    "fire resistance": "fire",
    "cold resistance": "cold",
    "lightning resistance": "lightning",
    "physical resistance": "physical",
    "void resistance": "void",
    "necrotic resistance": "necrotic",
    "poison resistance": "poison",
    "elemental resistance": "elemental",
    "all resistances": "all",
}

REASON_DEFENSIVE = "defensive stat; does not affect Flame Reave hit damage in this model"
REASON_UTILITY = "utility/behaviour stat; does not affect Flame Reave hit damage in this model"
REASON_OUT_OF_SCOPE = "out-of-scope mechanic for the Flame Reave hit model"

#: exact stat-name special cases (data quirks in the frozen export)
_STAT_DOUBLED_FIRE_PREFIX = "increased fire damage. this effect is doubled"
_STAT_GLOBAL_CONDITIONAL = "global conditional damage"
_STAT_OFFHAND_EXALTED = "increased effectiveness of stats on an offhand exalted weapon"

#: 历史 hit 路径的兼容映射策略：只产出原词表内的 stat_key，未映射行沿用原文案。
#: 统一 dps 路径使用 ``contribution_mapping.GENERIC_POLICY``（DoT-only 保留）。
HIT_MAPPING_POLICY = generic_mapping.MappingPolicy(
    name="fr-hit-legacy",
    extended_stats=False,
    dot_only_damage="exclude",
    dot_only_reason=(
        "DoT-only modifier (tags include DoT without Hit); "
        "Flame Reave hit damage does not inherit DoT modifiers"
    ),
    reason_templates={
        generic_mapping.COVERAGE_DEFENSIVE: REASON_DEFENSIVE,
        generic_mapping.COVERAGE_UTILITY: REASON_UTILITY,
        generic_mapping.COVERAGE_OUT_OF_SCOPE: REASON_OUT_OF_SCOPE,
    },
    legacy_evidence_prefix="archive://flame-reave/inputs/",
    adaptive_added_note=(
        " [adaptive added damage: may apply as any type, settled as Fire in this "
        "single-component model (user rule 2026-11-07)]"
    ),
    skill_level_reason=(
        "skill level only gates skill-node points; FR is already modelled "
        "with 27 points (user rule 2026-11-07). Level does not scale damage."
    ),
    legacy_classifier=True,
    collapse_unknown_to_out_of_scope=True,
)

# ---------------------------------------------------------------------------
# Skill-tree semantics for the equipped Flame Reave tree (fr11mv)
#
# Only per-node *semantics* live here (which effect index is which modifier and
# how it maps into the snapshot vocabulary).  The numeric values are always
# read from the pinned dataset (`nodes` / `effects` / `effect_values`); this
# table is game-data interpretation, not a build-specific number table.
# A selected node whose effect index is not declared here fails closed so a
# different build cannot silently inherit these semantics.
# ---------------------------------------------------------------------------

#: role values: stat | info | ignored
FR_SKILL_NODE_SEMANTICS: dict[str, dict[int, dict[str, Any]]] = {
    "2": {
        0: {"stat_key": model.STAT_ATTACK_SPEED, "operation": "increased",
            "damage_tags": [], "unit": "percent", "role": "stat"},
        1: {"stat_key": model.STAT_MANA_COST, "operation": "increased",
            "damage_tags": [], "unit": "percent", "role": "stat",
            "note": "negative value = reduced Flame Reave mana cost "
                    "(cost = (base+flat) * (1+increased); assumption)"},
        2: {"stat_key": "range", "operation": "increased", "damage_tags": [],
            "unit": "percent", "role": "ignored",
            "exclusion_reason": "range/width has no effect on hit damage"},
    },
    "3": {
        0: {"stat_key": "rhythm_stacks_on_use", "operation": "added",
            "damage_tags": [], "unit": "flat", "role": "info",
            "exclusion_reason": "Rhythm of Fire stack source; modelled as a "
                                "rotation state in ruleset.effects / scenario"},
        1: {"stat_key": "aoe_width", "operation": "increased", "damage_tags": [],
            "unit": "percent", "role": "ignored",
            "exclusion_reason": "range/width has no effect on hit damage"},
        2: {"stat_key": "range", "operation": "increased", "damage_tags": [],
            "unit": "percent", "role": "ignored",
            "exclusion_reason": "range/width has no effect on hit damage"},
        3: {"stat_key": model.STAT_DAMAGE, "operation": "more",
            "damage_tags": [], "unit": "percent", "role": "stat",
            "condition_code": "rhythm_consume",
            "group": "node:fr11mv:3",
            "note": "consuming 12 Rhythm of Fire stacks: +130% more damage"},
    },
    "4": {
        0: {"stat_key": "rhythm_stacks_on_surge", "operation": "added",
            "damage_tags": [], "unit": "flat", "role": "info",
            "exclusion_reason": "Rhythm of Fire stack source (other skill)"},
        1: {"stat_key": "rhythm_stacks_on_fireball", "operation": "added",
            "damage_tags": [], "unit": "flat", "role": "info",
            "exclusion_reason": "Rhythm of Fire stack source (other skill)"},
        2: {"stat_key": "rhythm_stacks_on_mana_strike", "operation": "added",
            "damage_tags": [], "unit": "flat", "role": "info",
            "exclusion_reason": "Rhythm of Fire stack source (other skill)"},
        3: {"stat_key": "rhythm_stacks_on_firebrand", "operation": "added",
            "damage_tags": [], "unit": "flat", "role": "info",
            "exclusion_reason": "Rhythm of Fire stack source (other skill)"},
    },
    "6": {
        0: {"stat_key": "frenzy_chance_on_hit", "operation": "added",
            "damage_tags": [], "unit": "percent", "role": "info",
            "exclusion_reason": "buff application chance; modelled in ruleset.effects"},
        1: {"stat_key": model.STAT_ADDED_DAMAGE, "operation": "added",
            "damage_tags": ["Fire"], "unit": "flat", "role": "stat",
            "condition_code": "frenzy",
            "note": "Flame Reave fire damage while Frenzy is active"},
        2: {"stat_key": "frenzy_duration_s", "operation": "added",
            "damage_tags": [], "unit": "seconds", "role": "info",
            "exclusion_reason": "buff duration; modelled in ruleset.effects"},
    },
    "9": {
        0: {"stat_key": model.STAT_DAMAGE, "operation": "more",
            "damage_tags": ["Fire"], "unit": "percent", "role": "stat",
            "group": "node:fr11mv:9",
            "note": "Heat Wave: more fire damage per point"},
    },
    "12": {
        0: {"stat_key": model.STAT_DAMAGE, "operation": "more",
            "damage_tags": ["Melee"], "unit": "percent", "role": "stat",
            "group": "node:fr11mv:12",
            "note": "Arcane Severance: more melee damage per point"},
        1: {"stat_key": "added_melee_per_4_added_spell", "operation": "added",
            "damage_tags": ["Melee"], "unit": "flat", "role": "derived",
            "note": "converted below into an added_melee_damage contribution "
                    "based on total added spell damage"},
    },
    "13": {
        0: {"stat_key": model.STAT_DAMAGE, "operation": "more",
            "damage_tags": ["Melee"], "unit": "percent", "role": "stat",
            "group": "node:fr11mv:13",
            "note": "Ember Infusion: more melee damage per point"},
    },
    "16": {
        0: {"stat_key": "hit_count_returns", "operation": "added",
            "damage_tags": [], "unit": "flat", "role": "info",
            "exclusion_reason": "hit-count flag; consumed by derived.hit_count"},
        1: {"stat_key": "cooldown_s", "operation": "added",
            "damage_tags": [], "unit": "seconds", "role": "info",
            "exclusion_reason": "skill cooldown; consumed by derived.skill/rotation"},
    },
    "18": {
        0: {"stat_key": "hit_count_returns", "operation": "added",
            "damage_tags": [], "unit": "flat", "role": "info",
            "exclusion_reason": "hit-count flag; consumed by derived.hit_count"},
        1: {"stat_key": model.STAT_MANA_COST, "operation": "added",
            "damage_tags": [], "unit": "flat", "role": "stat",
            "note": "Flame Caller: +6 mana cost"},
    },
    "20": {
        0: {"stat_key": model.STAT_CRIT_CHANCE, "operation": "added",
            "damage_tags": [], "unit": "percent", "role": "stat"},
        1: {"stat_key": "aoe_width", "operation": "increased", "damage_tags": [],
            "unit": "percent", "role": "ignored",
            "exclusion_reason": "range/width has no effect on hit damage"},
    },
    "23": {
        0: {"stat_key": model.STAT_DAMAGE, "operation": "more",
            "damage_tags": ["Melee"], "unit": "percent", "role": "stat",
            "condition_code": "ignited",
            "group": "node:fr11mv:23",
            "note": "Scorching Tide: more melee damage against ignited enemies"},
    },
    "24": {
        0: {"stat_key": model.STAT_CRIT_CHANCE, "operation": "more",
            "damage_tags": [], "unit": "percent", "role": "stat",
            "condition_code": "ignited",
            "group": "node:fr11mv:24",
            "note": "Chaos Flare: more crit chance against ignited enemies"},
    },
    "25": {
        0: {"stat_key": "range", "operation": "increased", "damage_tags": [],
            "unit": "percent", "role": "ignored",
            "exclusion_reason": "range/width has no effect on hit damage"},
    },
}

#: nodes that contribute to the per-cast hit count (DESIGN §3 R1)
FR_HIT_COUNT_NODES = {"16": 2, "18": 1}  # node 16: two additional hits; node 18: one return


class HitInputError(ValueError):
    """Fatal hit snapshot input-contract violation."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _read_json_object(path: Path, what: str) -> dict[str, Any]:
    if not path.is_file():
        raise HitInputError(f"{what} not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HitInputError(f"{what} is not valid JSON ({path}): {exc}") from exc
    if not isinstance(data, dict):
        raise HitInputError(f"{what} must be a JSON object: {path}")
    return data


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (root / path)


def _num(value: Any, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return default if value is None else float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_or_none(value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _evidence(kind: str, detail: str, locator: Optional[str] = None) -> model.Evidence:
    return model.Evidence(kind=kind, detail=detail, locator=locator)


def _catalog_locator(dataset_id: str, sql: str) -> str:
    return f"data/releases/{dataset_id}/data.sqlite#{sql}"


def decode_tag_names(bitmask: Any) -> list[str]:
    """Decode an LE Tools AT tag bitmask (int or numeric string) into tag names."""
    return generic_mapping.decode_tag_names(bitmask)


def load_case(case: str | Path | dict[str, Any], *, repo_root: str | Path | None = None) -> dict[str, Any]:
    """Load a ``le-case-v1`` document (path or dict) against the repo root."""
    base = Path(repo_root).resolve() if repo_root is not None else REPO_ROOT
    if isinstance(case, (str, Path)):
        document = _read_json_object(_resolve(base, case), "case configuration")
    elif isinstance(case, dict):
        document = dict(case)
    else:  # pragma: no cover - defensive
        raise HitInputError(f"case must be a path or dict, got {type(case).__name__}")
    for key in ("case_id", "model", "main_skill", "build", "inputs", "dataset"):
        if key not in document:
            raise HitInputError(f"case configuration missing required key: {key}")
    if document.get("model") != HIT_MODEL:
        raise HitInputError(
            f"case.model is {document.get('model')!r}; this adapter only serves "
            f"model={HIT_MODEL!r}"
        )
    return document


# ---------------------------------------------------------------------------
# Frozen input loading
# ---------------------------------------------------------------------------


def _load_dataset(
    case: dict[str, Any],
    base: Path,
    dataset_path: str | Path | None,
) -> tuple[dict[str, Any], dict[str, Any], Path, dict[str, Any]]:
    """Return ``(data_current, dataset_manifest, database_path, dataset_meta)``."""
    pointer_path = _resolve(base, dataset_path) if dataset_path is not None else _resolve(base, case["dataset"])
    if pointer_path.suffix.lower() in (".sqlite", ".db", ".sqlite3"):
        database = pointer_path.resolve()
        current_path = base / DEFAULT_DATASET_CURRENT
        current = _read_json_object(current_path, "data current pointer")
        manifest_path = _resolve(base, str(current.get("manifest") or ""))
        manifest = _read_json_object(manifest_path, "dataset manifest")
    else:
        current = _read_json_object(pointer_path, "dataset pointer")
        database = _resolve(base, str(current.get("database") or "")).resolve()
        manifest_path = _resolve(base, str(current.get("manifest") or ""))
        manifest = _read_json_object(manifest_path, "dataset manifest")
    if not database.is_file():
        raise HitInputError(f"dataset database not found: {database}")
    expected = case.get("dataset_expected") or {}
    checks = [
        ("dataset_id", current.get("dataset_id"), expected.get("dataset_id")),
        ("logical_content_sha256", current.get("logical_content_sha256"), expected.get("logical_content_sha256")),
        ("file_sha256", current.get("file_sha256"), expected.get("file_sha256")),
    ]
    for name, actual, case_expected in checks:
        if case_expected is not None and actual != case_expected:
            raise HitInputError(
                f"dataset {name} mismatch: data/current.json={actual!r} case.dataset_expected={case_expected!r}"
            )
    actual_file_sha = cached_file_sha256(database)
    if current.get("file_sha256") and actual_file_sha != current.get("file_sha256"):
        raise HitInputError(
            f"dataset database hash mismatch: actual={actual_file_sha} "
            f"pointer={current.get('file_sha256')} ({database})"
        )
    dataset_meta: dict[str, Any] = {}
    try:
        with Catalog(database, root=base) as catalog:
            dataset_meta = catalog.dataset_meta()
    except Exception:  # pragma: no cover - meta is optional
        dataset_meta = {}
    return current, manifest, database, dataset_meta


def _load_build(
    case: dict[str, Any],
    calc: dict[str, Any],
    base: Path,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """Load the pinned build revision and cross-check it against the calc input."""
    build_cfg = dict(case.get("build") or {})
    build_dir = _resolve(base, build_cfg.get("build_dir") or "data/builds")
    current_path = build_dir / "current.json"
    current = _read_json_object(current_path, "build current pointer") if current_path.is_file() else {}
    revision_id = str(calc.get("revision_id") or build_cfg.get("revision_id") or "")
    if not revision_id:
        raise HitInputError("calculation input has no revision_id")
    revision_dir = build_dir / "revisions" / revision_id
    build_json_path = (base / str(build_cfg["build_spec"])) if build_cfg.get("build_spec") else (
        revision_dir / "build.json"
    )
    revision = _read_json_object(build_json_path, "published revision build.json")
    meta = (revision.get("meta") or {}).get("revision") or {}
    content_sha = calc.get("content_sha256")
    checks = (
        ("revision_id", meta.get("revision_id"), revision_id),
        ("content_sha256", meta.get("content_sha256"), content_sha),
        ("revision_seq", meta.get("revision_seq"), calc.get("revision_seq")),
    )
    for name, actual, expected in checks:
        if expected is not None and actual != expected:
            raise HitInputError(
                f"build revision {name} mismatch: build.json={actual!r} calc_input={expected!r} "
                f"({build_json_path})"
            )
    if build_cfg.get("content_sha256") and content_sha and build_cfg["content_sha256"] != content_sha:
        raise HitInputError(
            f"case.build.content_sha256={build_cfg['content_sha256']!r} != calc_input.content_sha256={content_sha!r}"
        )
    return revision, current, build_json_path


def _load_calculation_input(path: Path) -> dict[str, Any]:
    calc = _read_json_object(path, "calculation input")
    schema = calc.get("schema_version")
    if schema != CALC_SCHEMA_VERSION:
        raise HitInputError(f"calculation input schema {schema!r} != {CALC_SCHEMA_VERSION!r} ({path})")
    return calc


def _check_case_structure(
    case: dict[str, Any],
    calc: dict[str, Any],
) -> dict[str, Any]:
    """Validate ``case.snapshot_expectations`` against the calc input (fail-closed)."""
    expectations = dict(case.get("snapshot_expectations") or {})
    if not expectations:
        raise HitInputError("case.snapshot_expectations is required for a hit case")
    skills = calc.get("skills") or {}
    specializations = list(skills.get("specializations") or [])
    passive_tree = calc.get("passive_tree") or {}
    counts = {
        "items": len(calc.get("items") or []),
        "mods": len(calc.get("mods") or []),
        "passive_nodes": len(passive_tree.get("nodes") or []),
        "passive_points": passive_tree.get("points_total"),
        "specializations": len(specializations),
        "skill_tree_nodes_total": sum(len(spec.get("nodes") or []) for spec in specializations),
    }
    ability_id = str((case.get("main_skill") or {}).get("ability_id") or "")
    fr_spec = _specialization_for(specializations, ability_id)
    if fr_spec is None:
        raise HitInputError(f"main skill {ability_id!r} has no specialization in the calc input")
    counts["fr11mv_points_total"] = fr_spec.get("points_total")
    problems: list[str] = []
    for key, expected in expectations.items():
        if key in ("comment", "note", "notes"):
            continue
        if key not in counts:
            problems.append(f"unknown expectation {key!r}")
            continue
        actual = counts[key]
        if actual != expected:
            problems.append(f"{key}: case expects {expected!r}, calc input has {actual!r}")
    if problems:
        raise HitInputError(
            "case.snapshot_expectations mismatch (" + "; ".join(problems) + ")"
        )
    return counts


def _specialization_for(specializations: Iterable[dict[str, Any]], ability_id: str) -> Optional[dict[str, Any]]:
    if not ability_id:
        return None
    for spec in specializations:
        owner = ((spec.get("owner_skill") or {}).get("entity_id") or "")
        tree = ((spec.get("tree") or {}).get("entity_id") or "")
        if owner.endswith(f":{ability_id}") or tree.endswith(f":{ability_id}"):
            return spec
    return None


# ---------------------------------------------------------------------------
# Contribution mapping helpers
# ---------------------------------------------------------------------------

_ITEM_SOURCE_BY_PREFIX = (
    ("equipment.idol_altar", "idol_altar", "Idol Altar"),
    ("equipment.", "equipment", "Equipment"),
    ("idols.", "idol", "Idol"),
    ("blessings.", "blessing", "Blessing"),
)


def _source_kind_for_instance(instance_id: str) -> tuple[str, str]:
    for prefix, kind, label in _ITEM_SOURCE_BY_PREFIX:
        if instance_id.startswith(prefix):
            return kind, label
    return "equipment", "Equipment"


def _item_lookup(calc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for item in calc.get("items") or []:
        instance_id = item.get("instance_id")
        if instance_id:
            lookup[str(instance_id)] = item
    return lookup


def _item_label(item: Optional[dict[str, Any]]) -> str:
    if not item:
        return ""
    display = item.get("display_name") or ""
    base = item.get("base_name") or item.get("item_type") or ""
    if display and base and display != base:
        return f"{display} ({base})"
    return display or base


def _condition_status(code: Optional[str], states: dict[str, Any]) -> str:
    from .condition_codes import evaluate_condition  # 共享条件求值，避免依赖命中计算模块

    if not code:
        return model.CONDITION_ALWAYS
    value = evaluate_condition(code, states)
    if value is None:
        return model.CONDITION_UNKNOWN
    return model.CONDITION_SATISFIED if value else model.CONDITION_NOT_SATISFIED


def _make_contribution(
    *,
    contribution_id: str,
    source_kind: str,
    source_label: str,
    source_path: str,
    stat_name: str,
    stat_key: str,
    operation: str,
    value: float,
    unit: str = "flat",
    damage_tags: Optional[list[str]] = None,
    target: Optional[str] = None,
    condition_code: Optional[str] = None,
    group: Optional[str] = None,
    included: bool = True,
    exclusion_reason: Optional[str] = None,
    value_source: str = "source",
    status: str = model.STATUS_CONFIRMED,
    evidence: Optional[list[model.Evidence]] = None,
    notes: Optional[str] = None,
    raw: Optional[dict[str, Any]] = None,
    states: Optional[dict[str, Any]] = None,
) -> model.Contribution:
    return model.Contribution(
        contribution_id=contribution_id,
        source_kind=source_kind,
        source_label=source_label,
        source_path=source_path,
        stat_name=stat_name,
        stat_key=stat_key,
        operation=operation,
        value=float(value),
        unit=unit,
        damage_tags=list(damage_tags or []),
        target=target,
        condition_code=condition_code,
        condition_status=_condition_status(condition_code, states or DEFAULT_HIT_STATES),
        group=group,
        included=included,
        exclusion_reason=exclusion_reason,
        value_source=value_source,
        status=status,
        evidence=evidence or [],
        notes=notes,
        raw=raw,
    )


def _unsupported(
    *,
    contribution_id: str,
    source_kind: str,
    source_label: str,
    source_path: str,
    stat_name: str,
    operation: str = "flag",
    value: float = 0.0,
    unit: str = "flat",
    reason: str = REASON_OUT_OF_SCOPE,
    status: str = model.STATUS_CONFIRMED,
    evidence: Optional[list[model.Evidence]] = None,
    raw: Optional[dict[str, Any]] = None,
) -> model.Contribution:
    return _make_contribution(
        contribution_id=contribution_id,
        source_kind=source_kind,
        source_label=source_label,
        source_path=source_path,
        stat_name=stat_name,
        stat_key=model.STAT_UNSUPPORTED,
        operation=operation,
        value=value,
        unit=unit,
        included=False,
        exclusion_reason=reason,
        status=status,
        evidence=evidence,
        raw=raw,
    )


def _damage_tags_for(tags: Optional[int]) -> list[str]:
    return decode_tag_names(tags)


def _is_dot_only(tags: Optional[int]) -> bool:
    if not tags:
        return False
    return bool(tags & TAG_DOT) and not bool(tags & TAG_HIT)


def _damage_operation(modifier_type: Optional[int]) -> tuple[str, str]:
    """Map the export's modifier_type to (operation, unit)."""
    if modifier_type == 0:
        return "added", "flat"
    if modifier_type == 1:
        return "increased", "percent"
    if modifier_type == 2:
        return "more", "percent"
    return "flag", "flat"


def _mod_evidence(mod: dict[str, Any], dataset_id: str, locator: str) -> list[model.Evidence]:
    evidence = [
        _evidence(
            "catalog",
            "calculation-input-v1 modifier line (affix-db recomputed export)",
            f"archive://flame-reave/inputs/{Path(locator).name}" if locator else None,
        )
    ]
    provenance = mod.get("provenance") or {}
    if provenance.get("source_path"):
        evidence.append(
            _evidence(
                "catalog",
                f"build source path {provenance.get('source_path')}",
                _catalog_locator(dataset_id, f"build spec {provenance.get('source_path')}"),
            )
        )
    return evidence


def _legacy_hit_quirk_rows(
    mod: dict[str, Any],
    *,
    name: str,
    lowered: str,
    item: Optional[dict[str, Any]],
    dataset_id: str,
    states: dict[str, Any],
    calc_input_label: str,
) -> Optional[list[model.Contribution]]:
    """Flame Reave 命中模型保留的词条数据怪癖（兼容边界，不属于通用映射）。

    这里集中了依赖 FR 规则集或 FR 技能名的特殊解释：Army of Skin 条件行、
    Whetstone Gavel 词条、冻结目标暴伤、药水暴击词条以及 ``Flame Reave Mana
    Cost``。统一 dps 路径不经过这些分支，由 ``contribution_mapping`` 的通用
    分类记录为未映射/待规则化。
    """
    instance_id = str(mod.get("item_instance_id") or "")
    source_kind, _ = _source_kind_for_instance(instance_id)
    source_label = _item_label(item) or instance_id
    source_path = (mod.get("provenance") or {}).get("source_path") or f"data.{instance_id}"
    mod_id = str(mod.get("mod_id") or "")
    contribution_id = f"mod:{mod_id}"
    base_evidence = _mod_evidence(mod, dataset_id, calc_input_label)
    raw = {
        "mod_id": mod_id,
        "item_instance_id": instance_id,
        "property": mod.get("property"),
        "tags": mod.get("tags"),
        "special_tag": mod.get("special_tag"),
        "modifier_type": mod.get("modifier_type"),
        "display_value": mod.get("display_value"),
        "readable_text": mod.get("readable_text"),
    }
    value = _num(mod.get("value_numeric"))
    stat_unit = (mod.get("stat") or {}).get("unit") or "flat"
    value_status = mod.get("value_status")
    status = model.STATUS_CONFIRMED if value_status in (None, "actual") else model.STATUS_DERIVED

    def make(**kwargs: Any) -> model.Contribution:
        payload = dict(
            contribution_id=contribution_id,
            source_kind=source_kind,
            source_label=source_label,
            source_path=source_path,
            stat_name=name,
            operation="flag",
            value=value,
            unit=stat_unit,
            evidence=base_evidence,
            notes=mod.get("readable_text"),
            raw=raw,
            value_source=mod.get("value_source") or "source",
            status=status,
            states=states,
        )
        payload.update(kwargs)
        return _make_contribution(**payload)

    def unsupported(**kwargs: Any) -> model.Contribution:
        payload = dict(
            contribution_id=contribution_id,
            source_kind=source_kind,
            source_label=source_label,
            source_path=source_path,
            stat_name=name,
            operation="flag",
            value=0.0,
            unit="flat",
            reason=REASON_OUT_OF_SCOPE,
            status=model.STATUS_CONFIRMED,
            evidence=base_evidence,
            raw=raw,
        )
        payload.update(kwargs)
        return _unsupported(**payload)

    if lowered == _STAT_GLOBAL_CONDITIONAL:
        return [
            unsupported(
                reason=(
                    "conditional 'more Melee Damage to High Health Enemies' "
                    "(Army of Skin roll) is modelled through ruleset.effects "
                    "army_of_skin_high_health_more (enemy_high_health state); "
                    "the footer row is kept informational only"
                ),
                status=model.STATUS_DERIVED,
            )
        ]
    if lowered == _STAT_OFFHAND_EXALTED:
        return [
            unsupported(
                reason=(
                    "Whetstone Gavel unique modifier: offhand is an Exalted weapon under "
                    "the display-rarity rule (T6+ affixes), so the condition IS satisfied; "
                    "which offhand stats are scaled by +97% effectiveness is still pending "
                    "(ruleset.effects offhand_exalted_effect)"
                ),
                status=model.STATUS_UNRESOLVED,
            )
        ]
    if "critical strike multiplier against frozen" in lowered:
        return [
            unsupported(
                reason="conditional on the target being frozen; target state not modelled (baseline)",
                status=model.STATUS_UNRESOLVED,
            )
        ]
    if lowered.startswith("critical strike chance applies") or lowered.startswith(
        "critical strike multiplier applies"
    ):
        return [unsupported(reason=REASON_UTILITY + " (potion crit only)")]
    if name == "Flame Reave Mana Cost":
        return [
            make(
                stat_key=model.STAT_MANA_COST,
                operation="added",
                value=value,
                unit="flat",
                target="fr11mv",
            )
        ]
    return None


def map_mod(
    mod: dict[str, Any],
    *,
    item: Optional[dict[str, Any]] = None,
    dataset_id: str = "",
    states: Optional[dict[str, Any]] = None,
    calc_input_label: str = "calculation-input.affix-db.corrected.json",
) -> list[model.Contribution]:
    """Map one frozen ``calculation-input`` modifier line into hit contributions.

    兼容入口：数据怪癖留在本模块，其余通用属性归一化委托
    :func:`contribution_mapping.map_mod`（``extended_stats=False`` 保历史词表）。
    """
    states = states or DEFAULT_HIT_STATES
    name = ((mod.get("stat") or {}).get("name") or "").strip()
    if mod.get("kind") != "description" and name:
        quirk = _legacy_hit_quirk_rows(
            mod,
            name=name,
            lowered=name.lower(),
            item=item,
            dataset_id=dataset_id,
            states=states,
            calc_input_label=calc_input_label,
        )
        if quirk is not None:
            return quirk
    return generic_mapping.map_mod(
        mod,
        item=item,
        dataset_id=dataset_id,
        states=states,
        calc_input_label=calc_input_label,
        policy=HIT_MAPPING_POLICY,
    )


def _tree_by_source_id(catalog: Catalog, source_tree_id: str) -> dict[str, Any]:
    """兼容委托：目录树解析统一在 ``contribution_mapping``。"""
    try:
        return generic_mapping.tree_by_source_id(catalog, source_tree_id)
    except generic_mapping.MappingError as exc:
        raise HitInputError(str(exc))


def _nodes_by_index(catalog: Catalog, tree_entity_id: str) -> dict[str, dict[str, Any]]:
    return generic_mapping.nodes_by_index(catalog, tree_entity_id)


def _skill_node_effect_values(
    catalog: Catalog,
    node_entity_id: str,
) -> dict[int, list[dict[str, Any]]]:
    """兼容委托：节点 stat_mod 值分组统一在 ``contribution_mapping``。"""
    return generic_mapping.skill_node_effect_values(catalog, node_entity_id)


def _skill_node_display_name(
    catalog: Catalog,
    node: dict[str, Any],
    bd_node: Optional[dict[str, Any]],
) -> str:
    if bd_node and bd_node.get("name"):
        return str(bd_node["name"])
    extensions = node.get("extensions") or {}
    key = extensions.get("nodeNameKey") if isinstance(extensions, dict) else None
    if key:
        translated = catalog.translation_by_key(str(key))
        if translated:
            return str(translated)
    return f"node {node.get('node_index')}"


def _bd_trees(bd: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """bd.json specializations/passives keyed by tree id (labels only)."""
    by_tree: dict[str, dict[str, Any]] = {}
    for spec in bd.get("specializations") or []:
        tree_id = str(spec.get("tree_id") or "")
        nodes = {str(node.get("node_id")): node for node in spec.get("nodes") or []}
        by_tree[tree_id] = {
            "kind": "skill",
            "nodes": nodes,
            "points_total": spec.get("points_total"),
            "base_level": spec.get("base_level"),
            "name": spec.get("name"),
        }
    passive = bd.get("passive_tree") or {}
    if passive.get("tree_id"):
        by_tree[str(passive["tree_id"])] = {
            "kind": "passive",
            "nodes": {str(node.get("node_id")): node for node in passive.get("nodes") or []},
            "points_total": passive.get("points_total"),
            "name": passive.get("name"),
        }
    return by_tree


def _node_stat_label(
    bd_node: Optional[dict[str, Any]],
    index: int,
    property_id: Optional[int],
) -> str:
    if bd_node:
        stats = bd_node.get("stats") or []
        if 0 <= index < len(stats):
            stat = stats[index] or {}
            if stat.get("name"):
                return str(stat["name"])
    if property_id is not None:
        return PROPERTY_LABELS.get(int(property_id), f"property {property_id}")
    return f"stat #{index}"


def _no_scaling_total(value: float, points: int, raw: dict[str, Any]) -> float:
    """兼容委托：noScaling 点数缩放统一在 ``contribution_mapping``。"""
    return generic_mapping.no_scaling_total(value, points, raw)


def _class_base(catalog: Catalog, class_entity_id: str) -> dict[str, Any]:
    try:
        return generic_mapping.class_base(catalog, class_entity_id)
    except generic_mapping.MappingError as exc:
        raise HitInputError(str(exc))


def _quest_attribute_points(catalog: Catalog) -> tuple[int, Optional[str]]:
    return generic_mapping.quest_attribute_points(catalog)


def _base_contributions(
    catalog: Catalog,
    calc: dict[str, Any],
    dataset_id: str,
) -> list[model.Contribution]:
    """职业基础属性 + 任务属性点（兼容委托）。"""
    try:
        return generic_mapping.base_contributions(catalog, calc, dataset_id)
    except generic_mapping.MappingError as exc:
        raise HitInputError(str(exc))


def _mod_contributions(
    calc: dict[str, Any],
    *,
    dataset_id: str,
    states: dict[str, Any],
    calc_input_label: str,
) -> tuple[list[model.Contribution], list[dict[str, Any]]]:
    """兼容委托：词条遍历统一在 ``contribution_mapping``，映射走 ``map_mod``。"""
    return generic_mapping.mod_contributions(
        calc,
        dataset_id=dataset_id,
        states=states,
        calc_input_label=calc_input_label,
        map_mod_fn=map_mod,
    )


def _passive_tree_id(calc: dict[str, Any]) -> str:
    return generic_mapping.passive_tree_id(calc)


def _condition_matches(condition: dict[str, Any], *, dual_wield: bool, mana_cost: float) -> tuple[Optional[bool], Optional[str]]:
    """Evaluate one planner passive condition -> (result, code)."""
    if "dualWield" in condition:
        expected = bool(condition["dualWield"])
        return (dual_wield == expected), "dual_wield"
    if "meleeAbilityManaCost" in condition:
        threshold = float(condition["meleeAbilityManaCost"])
        return (mana_cost >= threshold), "melee_ability_mana_cost"
    return None, None


def _passive_contributions(
    catalog: Catalog,
    calc: dict[str, Any],
    *,
    dataset_id: str,
    states: dict[str, Any],
    dual_wield: bool,
    mana_cost: float,
    bd_trees: dict[str, dict[str, Any]],
) -> list[model.Contribution]:
    tree_id = _passive_tree_id(calc)
    if not tree_id:
        raise HitInputError("calculation input passive_tree has no tree entity id")
    payload = catalog.passive_bonus_payload(tree_id)
    stats_by_node = payload.get("stats") or {}
    tree = _tree_by_source_id(catalog, tree_id)
    nodes = _nodes_by_index(catalog, tree["entity_id"])
    bd_nodes = (bd_trees.get(tree_id) or {}).get("nodes") or {}
    locator = (
        f"data/releases/{dataset_id}/data.sqlite#source_records "
        f"entity_kind=passive_bonus source_scope=planner.passiveBonuses source_id={tree_id}"
    )
    contributions: list[model.Contribution] = []
    for entry in (calc.get("passive_tree") or {}).get("nodes") or []:
        node_id = str(entry.get("node_id"))
        points = int(entry.get("points") or 0)
        if points <= 0:
            continue
        stats = stats_by_node.get(node_id) or []
        bd_node = bd_nodes.get(node_id)
        node_name = (bd_node or {}).get("name") or f"passive node {node_id}"
        node_row = nodes.get(node_id) or {}
        for index, stat in enumerate(stats):
            if not isinstance(stat, dict):
                continue
            property_id = stat.get("property")
            tags = stat.get("tags")
            stat_type = int(stat.get("type") or 0)
            value = _num(stat.get("value"))
            raw = {"node_id": node_id, "index": index, "stat": dict(stat)}
            operation = {0: "added", 1: "increased", 2: "more"}.get(stat_type, "flag")
            conditions = stat.get("conditions") or []
            condition_results: list[bool] = []
            condition_codes: list[str] = []
            for condition in conditions:
                result, code = _condition_matches(
                    condition, dual_wield=dual_wield, mana_cost=mana_cost
                )
                if result is None:
                    condition_results = []
                    condition_codes = [f"passive_condition:{json.dumps(condition, sort_keys=True)}"]
                    break
                condition_results.append(result)
                if code:
                    condition_codes.append(code)
            condition_code = None
            if condition_codes:
                condition_code = "+".join(condition_codes)
            condition_satisfied = all(condition_results) if condition_results else None
            stat_key, tags_list, target = _passive_stat_mapping(property_id, stat_type, tags)
            stat_name = _node_stat_label(bd_node, index, property_id)
            contribution_id = f"passive:{node_id}:{index}"
            evidence = [
                _evidence(
                    "catalog",
                    f"planner.passiveBonuses stats[{node_id!r}][{index}]",
                    locator,
                )
            ]
            raw.update(
                {
                    "points": points,
                    "noScaling": stat.get("noScaling"),
                    "noScalingPointThreshold": stat.get("noScalingPointThreshold"),
                }
            )
            if stat_key == model.STAT_UNSUPPORTED:
                contributions.append(
                    _unsupported(
                        contribution_id=contribution_id,
                        source_kind="passive_node",
                        source_label=f"{node_name} [passive node {node_id}]",
                        source_path=f"data.passive_tree.nodes.{node_id}",
                        stat_name=stat_name,
                        operation=operation,
                        value=value,
                        unit="flat",
                        reason=(
                            "passive stat is outside the Flame Reave hit damage model "
                            f"(property {property_id!r}, planner type {stat_type})"
                        ),
                        status=model.STATUS_CONFIRMED,
                        evidence=evidence,
                        raw=raw,
                    )
                )
                continue
            total = _no_scaling_total(value, points, stat)
            unresolved_semantics = PASSIVE_UNRESOLVED_SEMANTICS.get((node_id, index))
            is_conditional = condition_satisfied is False or (
                condition_code is not None and condition_satisfied is None
            )
            contributions.append(
                _make_contribution(
                    contribution_id=contribution_id,
                    source_kind="passive_node",
                    source_label=f"{node_name} [passive node {node_id}]",
                    source_path=f"data.passive_tree.nodes.{node_id}",
                    stat_name=stat_name,
                    stat_key=stat_key,
                    operation=operation,
                    value=total,
                    unit="percent" if stat_type in (1, 2) else "flat",
                    damage_tags=tags_list,
                    target=target,
                    condition_code=condition_code,
                    group=f"passive:{node_id}" if stat_type == 2 else None,
                    included=(condition_satisfied is not False or condition_code is None)
                    and unresolved_semantics is None,
                    exclusion_reason=(
                        unresolved_semantics
                        if unresolved_semantics is not None
                        else (
                            None
                            if condition_satisfied is not False or condition_code is None
                            else f"condition not satisfied: {condition_code}"
                        )
                    ),
                    value_source="derived",
                    status=(
                        model.STATUS_UNRESOLVED
                        if unresolved_semantics is not None
                        else model.STATUS_DERIVED
                    ),
                    evidence=evidence,
                    states=states,
                    notes=(
                        "planner.passiveBonuses normalized stat; value = per-point value "
                        f"{value:g} x {points} point(s)"
                        + (" (noScaling threshold applied)" if stat.get("noScaling") else "")
                    ),
                    raw=raw,
                )
            )
            if condition_code and condition_satisfied is None:
                contributions[-1].status = model.STATUS_UNRESOLVED
    return contributions


def _passive_stat_mapping(
    property_id: Any,
    stat_type: int,
    tags: Any,
) -> tuple[str, list[str], Optional[str]]:
    """兼容委托：被动 stat 词表映射统一在 ``contribution_mapping``（历史词表）。"""
    return generic_mapping.map_passive_stat(property_id, stat_type, tags)


def _skill_node_contributions(
    catalog: Catalog,
    calc: dict[str, Any],
    case: dict[str, Any],
    *,
    dataset_id: str,
    states: dict[str, Any],
    bd_trees: dict[str, dict[str, Any]],
) -> tuple[list[model.Contribution], list[dict[str, Any]], dict[str, Any]]:
    ability_id = str((case.get("main_skill") or {}).get("ability_id") or "")
    specializations = list((calc.get("skills") or {}).get("specializations") or [])
    spec = _specialization_for(specializations, ability_id)
    if spec is None:
        raise HitInputError(f"main skill {ability_id!r} has no specialization in the calc input")
    tree = _tree_by_source_id(catalog, ability_id)
    nodes = _nodes_by_index(catalog, tree["entity_id"])
    bd_nodes = (bd_trees.get(ability_id) or {}).get("nodes") or {}
    locator_base = f"data/releases/{dataset_id}/data.sqlite#trees source_tree_id={ability_id}"
    contributions: list[model.Contribution] = []
    node_effects: list[dict[str, Any]] = []
    extras: dict[str, Any] = {
        "added_melee_per_four_added_spell": None,
        "cooldown_s": None,
        "returns": [],
        "rhythm_stacks_on_use": None,
    }
    for entry in spec.get("nodes") or []:
        node_id = str(entry.get("node_id"))
        points = int(entry.get("points") or 0)
        if points <= 0:
            continue
        node = nodes.get(node_id)
        if node is None:
            raise HitInputError(f"fr11mv tree node {node_id!r} not found in catalog")
        bd_node = bd_nodes.get(node_id)
        node_name = _skill_node_display_name(catalog, node, bd_node)
        semantics = FR_SKILL_NODE_SEMANTICS.get(node_id)
        if semantics is None:
            raise HitInputError(
                f"no hit semantics declared for fr11mv tree node {node_id!r} ({node_name}); "
                "declare it in FR_SKILL_NODE_SEMANTICS before building a snapshot"
            )
        effect_values = _skill_node_effect_values(catalog, node["entity_id"])
        undeclared = sorted(set(effect_values) - set(semantics))
        if undeclared:
            raise HitInputError(
                f"fr11mv tree node {node_id!r} has undeclared stat indices {undeclared}; "
                "declare their semantics in FR_SKILL_NODE_SEMANTICS"
            )
        node_effect_entry: dict[str, Any] = {
            "node_id": int(node_id) if node_id.isdigit() else node_id,
            "name": node_name,
            "points": points,
            "max_points": node.get("max_points"),
            "node_type": node.get("node_type"),
            "effects": [],
        }
        for index in sorted(semantics):
            semantic = semantics[index]
            value_rows = effect_values.get(index) or []
            value_row = value_rows[0] if value_rows else None
            raw = (value_row or {}).get("raw") or {}
            value = _num((value_row or {}).get("value"))
            total = _no_scaling_total(value, points, raw)
            property_id = raw.get("property")
            stat_label = _node_stat_label(bd_node, index, property_id)
            role = semantic.get("role", "stat")
            effect_entry = {
                "stat_index": index,
                "stat_name": stat_label,
                "stat_key": semantic.get("stat_key"),
                "operation": semantic.get("operation", "added"),
                "value_per_point": value,
                "value_total": total,
                "unit": semantic.get("unit", "flat"),
                "damage_tags": list(semantic.get("damage_tags") or []),
                "condition_code": semantic.get("condition_code"),
                "role": role,
                "included": role == "stat",
                "group": semantic.get("group"),
                "note": semantic.get("note"),
                "catalog_effect_id": (value_row or {}).get("effect_id"),
                "property": property_id,
            }
            node_effect_entry["effects"].append(effect_entry)
            if role == "stat":
                stat_key = str(semantic["stat_key"])
                contribution_id = f"skill:fr11mv:{node_id}:{index}"
                contributions.append(
                    _make_contribution(
                        contribution_id=contribution_id,
                        source_kind="skill_node",
                        source_label=f"{node_name} [Flame Reave tree node {node_id}]",
                        source_path=f"data.specializations.{ability_id}.nodes.{node_id}",
                        stat_name=stat_label,
                        stat_key=stat_key,
                        operation=str(semantic.get("operation", "added")),
                        value=total,
                        unit=str(semantic.get("unit", "flat")),
                        damage_tags=list(semantic.get("damage_tags") or []),
                        target=ability_id,
                        condition_code=semantic.get("condition_code"),
                        group=semantic.get("group"),
                        value_source="derived",
                        status=model.STATUS_DERIVED,
                        evidence=[
                            _evidence(
                                "catalog",
                                f"node {node_id} ({node_name}) effect index {index}",
                                f"{locator_base}#node={node_id}",
                            )
                        ],
                        notes=(
                            f"value {value:g} x {points} point(s) = {total:g}; "
                            f"semantics declared by FR_SKILL_NODE_SEMANTICS"
                        ),
                        raw={"node_id": node_id, "stat_index": index, "property": property_id},
                        states=states,
                    )
                )
            elif role == "derived":
                extras["added_melee_per_four_added_spell"] = {
                    "node_id": node_id,
                    "points": points,
                    "value_per_4_spell_damage": total,
                    "note": semantic.get("note"),
                }
            elif role == "info":
                key = str(semantic.get("stat_key") or "")
                if "cooldown" in key:
                    extras["cooldown_s"] = total
                if "hit_count" in key:
                    extras["returns"].append(node_id)
                if key == "rhythm_stacks_on_use":
                    extras["rhythm_stacks_on_use"] = total
        node_effects.append(node_effect_entry)
    return contributions, node_effects, extras


EXALTED_EXCLUDED_BASE_TYPES = frozenset(set(range(25, 34)) | {34, 41})


def _display_rarity(
    *,
    raw_rarity: Optional[str],
    base_type_id: Optional[int],
    affixes: list[dict[str, Any]],
    unique: bool,
) -> str:
    """Displayed item rarity (save raw byte is not the displayed rarity).

    Rule from ``tools/save_import/scripts/build_mappings.py``:
    normal items are Exalted when any affix tier0 >= 5 (T6+) and the base type is
    not in the excluded set; otherwise fall back to the decoded rarity code.
    """
    if unique:
        return str(raw_rarity or "unique")
    if base_type_id not in EXALTED_EXCLUDED_BASE_TYPES and any(
        affix.get("tier0") is not None and int(affix["tier0"]) >= 5 for affix in affixes
    ):
        return "exalted"
    return str(raw_rarity or "normal")


def _weapon_section(
    calc: dict[str, Any],
    catalog: Catalog,
    dataset_id: str,
    *,
    added_policy: str = DEFAULT_WEAPON_ADDED_POLICY,
) -> dict[str, Any]:
    items = _item_lookup(calc)
    slots: list[dict[str, Any]] = []
    added_by_slot: dict[str, list[dict[str, Any]]] = {}
    for instance_id in ("equipment.weapon1", "equipment.weapon2"):
        item = items.get(instance_id)
        if not item:
            continue
        base_entity_id = str(item.get("base_entity_id") or "")
        base_row = catalog.entity(base_entity_id) if base_entity_id else None
        extensions = (base_row or {}).get("extensions") or {}
        raw_rarity = (item.get("rarity") or {}).get("code")
        affix_rows = []
        for mod in item.get("mods") or []:
            if mod.get("source") != "affix":
                continue
            affix = mod.get("affix") or {}
            affix_rows.append(
                {
                    "mod_id": mod.get("mod_id"),
                    "name": affix.get("name"),
                    "slot": affix.get("affix_slot"),
                    "tier": affix.get("source_tier"),
                    "tier0": affix.get("tier_index"),
                    "channel": mod.get("channel"),
                    "text": mod.get("readable_text"),
                    "value": _num(mod.get("value_numeric")),
                    "property": mod.get("property"),
                }
            )
        implicit_rows = [
            {
                "mod_id": mod.get("mod_id"),
                "text": mod.get("readable_text"),
                "value": _num(mod.get("value_numeric")),
                "property": mod.get("property"),
                "tags": decode_tag_names(mod.get("tags")),
            }
            for mod in item.get("mods") or []
            if mod.get("source") == "implicit"
        ]
        base_type_id = None
        try:
            base_type_id = int(str(base_entity_id).rsplit(":", 1)[-1])
        except (TypeError, ValueError):
            base_type_id = None
        display_rarity = _display_rarity(
            raw_rarity=raw_rarity,
            base_type_id=base_type_id,
            affixes=affix_rows,
            unique=bool(item.get("unique")),
        )
        base: dict[str, Any] = {
            "slot": instance_id.split(".")[-1],
            "instance_id": instance_id,
            "name": item.get("display_name"),
            "base_name": item.get("base_name"),
            "item_type": item.get("item_type"),
            "rarity": display_rarity,
            "raw_rarity": raw_rarity,
            "base_type_id": base_type_id,
            "base_entity_id": base_entity_id,
            "definition_entity_id": item.get("definition_entity_id"),
            "attack_rate": _num(extensions.get("attackRate")),
            "added_weapon_range": _num(extensions.get("addedWeaponRange")),
            "implicits": list(extensions.get("implicits") or []),
            "implicit_rows": implicit_rows,
            "affixes": affix_rows,
            "added": [],
        }
        for mod in item.get("mods") or []:
            stat = mod.get("stat") or {}
            if stat.get("name") != "Damage":
                continue
            if mod.get("modifier_type") != 0 or mod.get("source") != "implicit":
                continue
            property_id = mod.get("property")
            if property_id not in (PROP_DAMAGE,):
                continue
            tags_list = decode_tag_names(mod.get("tags"))
            base["added"].append(
                {
                    "damage_type": _damage_type_from_tags(tags_list) or "untyped",
                    "value": _num(mod.get("value_numeric")),
                    "damage_tags": tags_list,
                    "source": "implicit",
                    "mod_id": mod.get("mod_id"),
                    "readable_text": mod.get("readable_text"),
                }
            )
        added_by_slot[base["slot"]] = [dict(entry) for entry in base["added"]]
        if base_entity_id:
            base["evidence"] = _catalog_locator(
                dataset_id, f"entities entity_id={base_entity_id}"
            )
        slots.append(base)
    rates = [slot["attack_rate"] for slot in slots if slot.get("attack_rate")]
    average_attack_rate = (sum(rates) / len(rates)) if rates else 0.0
    dual_wield = len([slot for slot in slots if slot.get("attack_rate")]) >= 2
    # user ruling 2026-11-07: dual wield = both weapons apply, added damage summed.
    policy_status = {
        "sum": model.STATUS_CONFIRMED,
        "average": model.STATUS_ASSUMED,
    }[added_policy]
    return {
        "dual_wield": dual_wield,
        "slots": slots,
        "average_attack_rate": average_attack_rate,
        "added_by_slot": added_by_slot,
        "added_damage_policy": added_policy,
        "added_damage_policy_note": (
            "sum: all equipped weapon added damage enters the pool "
            "(user rule 2026-11-07: dual wield = both weapons apply, directly summed)"
            if added_policy == "sum"
            else "average: per-hit average of dual-wield weapon contributions "
            "(alternative only; superseded by user rule 2026-11-07)"
        ),
        "added_damage_policy_status": policy_status,
        "attack_rate_model": (
            "skill as x speedMultiplier (reference runtime; weapon attackRate not read)"
            if True
            else "average weapon attackRate"
        ),
        "attack_rate_model_status": model.STATUS_CONFIRMED,
    }


def _damage_type_from_tags(tags_list: Iterable[str]) -> Optional[str]:
    for tag in tags_list:
        if tag in DAMAGE_TYPE_TAGS:
            return tag
    return None


def _skill_section(
    catalog: Catalog,
    case: dict[str, Any],
    calc: dict[str, Any],
    *,
    dataset_id: str,
    extras: dict[str, Any],
    bd_doc: dict[str, Any],
) -> dict[str, Any]:
    ability_id = str((case.get("main_skill") or {}).get("ability_id") or "")
    row = catalog.skill_by_name(str((case.get("main_skill") or {}).get("name") or "Flame Reave"))
    if str(row.get("source_skill_id")) != ability_id:
        rows = [
            skill
            for skill in catalog.skills()
            if str(skill.get("source_skill_id")) == ability_id
        ]
        if len(rows) != 1:
            raise HitInputError(f"catalog skill {ability_id!r} not found exactly once")
        row = rows[0]
    extensions = row.get("extensions") or {}
    components = catalog.skill_components(row["entity_id"])
    hit_components = [c for c in components if c.get("component_type") == "DamageEnemyOnHit"]
    if not hit_components:
        raise HitInputError(f"skill {ability_id!r} has no DamageEnemyOnHit component")
    component = hit_components[0]
    config = _json_or_none(component.get("config")) or {}
    base_stats = config.get("baseDamageStats") or {}
    damage_type = component.get("damage_type") or "Fire"
    hit = bool(base_stats.get("hit", True))
    attribute_scaling = extensions.get("attributeScaling") or {}
    attribute_map = {}
    for index, entries in attribute_scaling.items():
        try:
            attribute = model.ATTRIBUTE_ORDER[int(index)]
        except (ValueError, IndexError):
            continue
        increased = 0.0
        for entry in entries or []:
            if entry.get("property") == PROP_DAMAGE:
                increased += _num(entry.get("increasedValue"))
        if increased:
            attribute_map[attribute] = increased
    bd_trees = _bd_trees(bd_doc)
    base_damage = [
        {"damage_type": damage_type, "value": _num(component.get("base_damage"))}
    ]
    cooldown_s = extras.get("cooldown_s")
    spec = _specialization_for(
        list((calc.get("skills") or {}).get("specializations") or []), ability_id
    )
    level_info = _skill_level_info(calc, bd_trees, ability_id)
    return {
        "ability_id": ability_id,
        "entity_id": row["entity_id"],
        "name": row.get("canonical_name") or case["main_skill"].get("name"),
        "class": ((calc.get("character") or {}).get("class") or {}).get("name"),
        "mastery": ((calc.get("character") or {}).get("mastery") or {}).get("name"),
        "tags": decode_tag_names(row.get("tags_raw")),
        "damage_tags": [damage_type],
        "base_damage": base_damage,
        "added_damage_scaling": _num(base_stats.get("addedDamageScaling")),
        "added_damage_effectiveness_percent": _num(component.get("added_damage_effectiveness")),
        "crit_chance": _num(base_stats.get("critChance"), 0.05),
        "crit_multiplier": _num(base_stats.get("critMultiplier"), 2.0),
        "mana_cost": _num(row.get("mana_cost")),
        "cast_time_s": _num(row.get("cast_time_ms")) / 1000.0 if row.get("cast_time_ms") else None,
        "use_duration_s": _num(extensions.get("useDuration"))
        or (_num(row.get("cast_time_ms")) / 1000.0 if row.get("cast_time_ms") else None),
        "use_delay_s": _num(extensions.get("useDelay")),
        "attack_speed_base_planner": _num(extensions.get("as")),
        "speed_scaler": extensions.get("speedScaler"),
        "speed_multiplier": _num(extensions.get("speedMultiplier"), 1.0),
        "max_charges": extensions.get("maxCharges"),
        "cooldown_s": cooldown_s,
        "hit": hit,
        "level": level_info["level"],
        "base_level": level_info.get("base_level"),
        "level_status": level_info["status"],
        "level_note": level_info.get("level_note"),
        "level_modifiers": level_info["modifiers"],
        "attribute_scaling": attribute_map,
        "specialization_points_total": (spec or {}).get("points_total"),
        "component_locator": component.get("locator"),
        "evidence": _catalog_locator(
            dataset_id, f"skills entity_id={row['entity_id']}; skill_components index=0"
        ),
    }


def _load_bd_optional(case: dict[str, Any], calc: dict[str, Any], dataset_id: str) -> dict[str, Any]:
    """Load ``case.inputs.bd`` for human-readable labels (never for numbers)."""
    inputs = case.get("inputs") or {}
    bd_path = inputs.get("bd")
    if not bd_path:
        return {}
    base = Path(REPO_ROOT)
    try:
        path = Path(bd_path)
        if not path.is_absolute():
            path = base / path
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):  # pragma: no cover - labels only
        return {}


def _skill_level_info(
    calc: dict[str, Any],
    bd_trees: dict[str, dict[str, Any]],
    ability_id: str,
) -> dict[str, Any]:
    base_level = None
    for spec in (calc.get("skills") or {}).get("specializations") or []:
        owner = ((spec.get("owner_skill") or {}).get("entity_id") or "")
        tree = ((spec.get("tree") or {}).get("entity_id") or "")
        if owner.endswith(f":{ability_id}") or tree.endswith(f":{ability_id}"):
            base_level = (bd_trees.get(ability_id) or {}).get("base_level") or spec.get("source_level")
    modifiers: list[dict[str, Any]] = []
    for mod in calc.get("mods") or []:
        stat = mod.get("stat") or {}
        if stat.get("name") != "Level of Skills":
            continue
        modifiers.append(
            {
                "mod_id": mod.get("mod_id"),
                "item_instance_id": mod.get("item_instance_id"),
                "value": mod.get("value_numeric"),
                "tags": decode_tag_names(mod.get("tags")),
                "readable_text": mod.get("readable_text"),
            }
        )
    # user ruling 2026-11-07: actual level 27 (offline save shows 20 + level mods);
    # level only gates skill-node points, it does not scale damage.
    points_total = (bd_trees.get(ability_id) or {}).get("points_total")
    effective_level = int(points_total) if points_total else base_level
    return {
        "level": effective_level,
        "base_level": base_level,
        "status": model.STATUS_CONFIRMED,
        "level_note": (
            "level 27 only affects the number of available skill nodes; it does not "
            "scale skill damage (user rule 2026-11-07). The FR tree is built with 27 points."
        ),
        "modifiers": modifiers,
    }


def _derive_resistances(contributions: Iterable[model.Contribution]) -> dict[str, dict[str, Any]]:
    """Player-side resistance totals (percentage points, cap 75)."""
    names = ["fire", "cold", "lightning", "physical", "void", "necrotic", "poison"]
    source_pools = {"elemental": 0.0, "all": 0.0}
    totals = {name: 0.0 for name in names}
    for contribution in contributions:
        if not contribution.active or contribution.stat_key != model.STAT_RESISTANCE:
            continue
        target = contribution.target
        if target in totals:
            totals[target] += contribution.value * 100.0
        elif target in source_pools:
            source_pools[target] += contribution.value * 100.0
    result: dict[str, dict[str, Any]] = {}
    for name in names:
        total = totals[name]
        if name in ("fire", "cold", "lightning"):
            total += source_pools["elemental"]
        total += source_pools["all"]
        cap = 75.0
        result[name] = {
            "total": round(total, 9),
            "cap": cap,
            "capped": round(min(total, cap), 9),
            "overcapped": round(max(0.0, total - cap), 9),
        }
    for name in ("elemental", "all"):
        cap = 75.0
        result[name] = {
            "total": round(source_pools[name], 9),
            "cap": cap,
            "capped": round(min(source_pools[name], cap), 9),
            "overcapped": round(max(0.0, source_pools[name] - cap), 9),
        }
    return result


def _estimate_mana(
    contributions: Iterable[model.Contribution],
    *,
    class_base: dict[str, Any],
    level: int,
    attributes: dict[str, float],
    skill_mana_cost: float,
) -> dict[str, Any]:
    """Approximate max mana and Flame Reave mana cost (both marked derived/assumed)."""
    contributions = list(contributions)
    max_mana_flat = _num(class_base.get("baseMana")) + _num(class_base.get("manaPerLevel")) * level
    max_mana_flat += 2.0 * attributes.get("attunement", 0.0)
    flat_rows: list[dict[str, Any]] = []
    increased = 0.0
    increased_rows: list[dict[str, Any]] = []
    for contribution in contributions:
        if not contribution.active:
            continue
        if contribution.stat_key == model.STAT_MANA:
            if contribution.operation == "added":
                max_mana_flat += contribution.value
                flat_rows.append({"contribution_id": contribution.contribution_id, "value": contribution.value})
            elif contribution.operation == "increased":
                increased += contribution.value
                increased_rows.append({"contribution_id": contribution.contribution_id, "value": contribution.value})
    mana_max = max_mana_flat * (1.0 + increased)
    flat_cost = 0.0
    cost_increased = 0.0
    cost_rows: list[dict[str, Any]] = []
    for contribution in contributions:
        if contribution.stat_key != model.STAT_MANA_COST:
            continue
        if contribution.operation == "added":
            flat_cost += contribution.value
            cost_rows.append({"contribution_id": contribution.contribution_id, "operation": "added", "value": contribution.value})
        elif contribution.operation == "increased":
            cost_increased += contribution.value
            cost_rows.append({"contribution_id": contribution.contribution_id, "operation": "increased", "value": contribution.value})
    mana_cost = max(0.0, (skill_mana_cost + flat_cost) * (1.0 + cost_increased))
    return {
        "mana_max_estimate": round(mana_max, 6),
        "mana_max_flat": round(max_mana_flat, 6),
        "mana_max_increased": round(increased, 6),
        "mana_max_rows": flat_rows + increased_rows,
        "mana_ge_300": bool(mana_max >= 300.0),
        "skill_mana_cost_base": skill_mana_cost,
        "skill_mana_cost_flat_added": round(flat_cost, 6),
        "skill_mana_cost_increased": round(cost_increased, 6),
        "skill_mana_cost_estimate": round(mana_cost, 6),
        "skill_mana_cost_rows": cost_rows,
        "status": model.STATUS_ASSUMED,
        "note": (
            "max mana = (class base + manaPerLevel*level + 2*Attunement + flat) * (1+increased); "
            "skill cost = (base + flat) * (1+increased); both formulas are assumptions "
            "(reference planner runtime) and only feed the mana_ge_300 / mana>=10 conditions"
        ),
    }


def _attribute_detail(
    contributions: Iterable[model.Contribution],
    class_base: dict[str, Any],
) -> dict[str, Any]:
    """兼容委托：属性汇总统一在 ``contribution_mapping``。"""
    return generic_mapping.attribute_detail(contributions, class_base)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def build_snapshot(
    case: str | Path | dict[str, Any],
    *,
    calc_input_path: str | Path | None = None,
    dataset_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    states: dict[str, Any] | None = None,
    weapon_added_policy: str = DEFAULT_WEAPON_ADDED_POLICY,
    verify_input_sha256: bool = True,
) -> model.Snapshot:
    """Build the frozen static hit snapshot for one Flame Reave case.

    ``weapon_added_policy`` selects the fr-hit-facts R2 candidate for how the two
    equipped weapons' added damage combine: ``"sum"`` (candidate A, default,
    reference-runtime consistent) or ``"average"`` (candidate B).
    """
    if weapon_added_policy not in WEAPON_ADDED_POLICIES:
        raise HitInputError(
            f"weapon_added_policy must be one of {WEAPON_ADDED_POLICIES}, got {weapon_added_policy!r}"
        )
    base = Path(repo_root).resolve() if repo_root is not None else REPO_ROOT
    case_doc = load_case(case, repo_root=base)
    states = dict(DEFAULT_HIT_STATES) if states is None else {**DEFAULT_HIT_STATES, **states}
    inputs = case_doc.get("inputs") or {}
    calc_path = (
        _resolve(base, calc_input_path)
        if calc_input_path is not None
        else _resolve(base, str(inputs.get("calculation_input") or ""))
    )
    if not calc_path.is_file():
        raise HitInputError(f"calculation input not found: {calc_path}")
    calc = _load_calculation_input(calc_path)
    calc_sha = file_sha256(calc_path)
    if verify_input_sha256:
        expected = (case_doc.get("input_sha256") or {}).get("calculation_input")
        if expected and expected != calc_sha:
            raise HitInputError(
                f"calculation input sha256 mismatch: file={calc_sha!r} case.input_sha256={expected!r}"
            )
    dataset_current, dataset_manifest, database_path, dataset_meta = _load_dataset(
        case_doc, base, dataset_path
    )
    dataset_id = str(dataset_current.get("dataset_id") or calc.get("dataset_id") or "")
    if calc.get("dataset_id") != dataset_current.get("dataset_id"):
        raise HitInputError(
            f"calc_input.dataset_id={calc.get('dataset_id')!r} != dataset pointer "
            f"{dataset_current.get('dataset_id')!r}"
        )
    if dataset_meta.get("target_game_version") and calc.get("game_version") and (
        dataset_meta["target_game_version"] != calc.get("game_version")
    ):
        raise HitInputError(
            f"game version mismatch: calc={calc.get('game_version')!r} dataset="
            f"{dataset_meta.get('target_game_version')!r}"
        )
    revision, build_current, build_json_path = _load_build(case_doc, calc, base)
    build_sha = file_sha256(build_json_path)
    current_json_path = _resolve(base, str(case_doc.get("dataset") or DEFAULT_DATASET_CURRENT))
    if current_json_path.suffix.lower() in (".sqlite", ".db", ".sqlite3"):
        current_json_path = base / DEFAULT_DATASET_CURRENT
    current_sha = file_sha256(current_json_path) if current_json_path.is_file() else None
    structure_counts = _check_case_structure(case_doc, calc)

    # -- verified frozen-input fingerprints --------------------------------
    if verify_input_sha256:
        _verify_case_input_hashes(case_doc, base)

    bd_doc = _load_bd_optional(case_doc, calc, dataset_id)
    bd_trees = _bd_trees(bd_doc)

    with Catalog(database_path, root=base) as catalog:
        character = calc.get("character") or {}
        class_entity_id = ((character.get("class") or {}).get("entity_id")) or ""
        class_base = _class_base(catalog, class_entity_id)
        level = int(_num(character.get("level")))
        monster_level = int(_num((case_doc.get("target") or {}).get("monster_level")
                                 or case_doc.get("monster_level")
                                 or (case_doc.get("target") or {}).get("monster_level")))
        if monster_level <= 0:
            monster_level = 100
        # weapon data is needed before passive conditions (dual wield) and mana
        weapon = _weapon_section(
            calc, catalog, dataset_id, added_policy=weapon_added_policy
        )
        dual_wield = bool(weapon.get("dual_wield"))
        base_contributions = _base_contributions(catalog, calc, dataset_id)
        contributions, unique_descriptions = _mod_contributions(
            calc,
            dataset_id=dataset_id,
            states=states,
            calc_input_label=calc_path.name,
        )
        skill_contributions, node_effects, skill_extras = _skill_node_contributions(
            catalog, calc, case_doc, dataset_id=dataset_id, states=states, bd_trees=bd_trees
        )
        skill = _skill_section(
            catalog, case_doc, calc, dataset_id=dataset_id, extras=skill_extras, bd_doc=bd_doc
        )
        skill_base_mana_cost = _num(skill.get("mana_cost"))
        # first pass: everything except passive conditions that need the skill mana cost
        preliminary = base_contributions + contributions + skill_contributions
        mana = _estimate_mana(
            preliminary,
            class_base=class_base,
            level=level,
            attributes=_attribute_detail(preliminary, class_base)["attributes"],
            skill_mana_cost=skill_base_mana_cost,
        )
        mana_cost_estimate = float(mana["skill_mana_cost_estimate"])
        # resolved build/target facts that passive conditions reference
        resolved_states = {
            **states,
            "dual_wield": dual_wield,
            "melee_ability_mana_cost": bool(mana_cost_estimate >= 10.0),
            "mana_ge_300": bool(mana["mana_max_estimate"] >= 300.0),
        }
        passive_contributions = _passive_contributions(
            catalog,
            calc,
            dataset_id=dataset_id,
            states=resolved_states,
            dual_wield=dual_wield,
            mana_cost=mana_cost_estimate,
            bd_trees=bd_trees,
        )
        contributions = base_contributions + contributions + passive_contributions + skill_contributions

        # dual-wield added-damage composition policy (fr-hit-facts R2 candidates)
        contributions = _apply_weapon_added_policy(
            contributions,
            weapon=weapon,
            calc=calc,
            policy=weapon_added_policy,
        )

        # Whetstone Gavel: offhand Exalted -> all offhand stats get +97% effectiveness
        contributions, offhand_effect = _apply_offhand_exalted_effect(
            contributions, weapon=weapon, calc=calc
        )
        weapon["offhand_exalted_effect"] = offhand_effect

        # attributes after passives (attribute scaling needs the final values)
        attribute_detail = _attribute_detail(contributions, class_base)
        attributes = attribute_detail["attributes"]

        # skill attribute scaling: +X% increased damage per point of the scaling attributes
        scaling_rows = []
        scaling_total = 0.0
        for attribute, per_point in (skill.get("attribute_scaling") or {}).items():
            value = float(attributes.get(attribute, 0.0)) * float(per_point)
            if value:
                scaling_rows.append({"attribute": attribute, "per_point": per_point, "value": value})
                scaling_total += value
        if scaling_total:
            contributions.append(
                _make_contribution(
                    contribution_id="skill:fr11mv:attribute_scaling",
                    source_kind="derived",
                    source_label="Flame Reave attribute scaling",
                    source_path="data.specializations.fr11mv.attributeScaling",
                    stat_name="increased Damage (attribute scaling)",
                    stat_key=model.STAT_DAMAGE,
                    operation="increased",
                    value=scaling_total,
                    unit="percent",
                    damage_tags=[],
                    value_source="derived",
                    status=model.STATUS_DERIVED,
                    evidence=[
                        _evidence(
                            "catalog",
                            "skill extensions.attributeScaling {2: Int +4%/point, 3: Dex +4%/point}",
                            skill.get("evidence"),
                        )
                    ],
                    notes=(
                        "derived: sum(final attribute value x per-point increased damage); "
                        + ", ".join(f"{row['attribute']}={row['value']:.6g}" for row in scaling_rows)
                    ),
                    raw={"rows": scaling_rows},
                )
            )

        # Arcane Severance conversion: added melee damage based on added spell damage
        conversion = skill_extras.get("added_melee_per_four_added_spell")
        if conversion:
            added_spell = _added_spell_damage_total(contributions)
            converted = (added_spell / 4.0) * float(conversion["value_per_4_spell_damage"])
            if converted:
                contributions.append(
                    _make_contribution(
                        contribution_id="derived:fr11mv:arcane_severance_added_melee",
                        source_kind="derived",
                        source_label="Arcane Severance [FR tree node 12]",
                        source_path="data.specializations.fr11mv.nodes.12",
                        stat_name="added melee damage (per 4 added spell damage)",
                        stat_key=model.STAT_ADDED_DAMAGE,
                        operation="added",
                        value=converted,
                        unit="flat",
                        damage_tags=["Melee"],
                        value_source="derived",
                        status=model.STATUS_ASSUMED,
                        evidence=[
                            _evidence(
                                "catalog",
                                "node 12 'Melee Damage per 4 Spell Damage' normalized value",
                                f"data/releases/{dataset_id}/data.sqlite#trees source_tree_id=fr11mv",
                            )
                        ],
                        notes=(
                            f"derived: added spell damage total {added_spell:g} / 4 x "
                            f"{conversion['value_per_4_spell_damage']:g} = {converted:g}; "
                            "damage type of the converted melee damage is assumed Fire"
                        ),
                        raw={"added_spell_damage_total": added_spell, **conversion},
                    )
                )

        # hit count / cooldown / rotation metadata
        hit_count = _hit_count_section(skill_contributions, skill_extras, skill)
        attack_speed_rows = _attack_speed_rows(contributions)
        mana = _estimate_mana(
            contributions,
            class_base=class_base,
            level=level,
            attributes=attributes,
            skill_mana_cost=skill_base_mana_cost,
        )
        mana["mana_ge_300"] = bool(mana["mana_max_estimate"] >= 300.0)
        resolved_states["mana_ge_300"] = mana["mana_ge_300"]
        resolved_states["melee_ability_mana_cost"] = bool(
            mana["skill_mana_cost_estimate"] >= 10.0
        )

        # -- resistances ---------------------------------------------------
        resistances = _derive_resistances(contributions)

        # -- derived block -------------------------------------------------
        derived: dict[str, Any] = {
            "model": HIT_MODEL,
            "skill": skill,
            "weapon": weapon,
            "hit_count": hit_count,
            "node_effects": node_effects,
            "applied_states": dict(resolved_states),
            "mana": mana,
            "attribute_detail": attribute_detail,
            "unique_descriptions": unique_descriptions,
            "excluded_non_dps": {
                "count": calc.get("excluded_non_dps"),
                "mod_ids": list(calc.get("excluded_non_dps_mod_ids") or []),
                "properties": list(calc.get("excluded_non_dps_properties") or []),
                "note": "out-of-DPS source properties excluded before the export (not counted twice)",
            },
            "attack_speed": _attack_speed_section(attack_speed_rows),
            "target": {
                "boss_or_rare": bool((case_doc.get("target") or {}).get("boss_or_rare", True)),
                "monster_level": monster_level,
                "monster_damage_reduction": dr_for_level(monster_level),
                "monster_dr_factor": 1.0 - dr_for_level(monster_level),
                "fire_resistance": 0.0,
                "resistance_status": model.STATUS_ASSUMED,
            },
            "structure_counts": structure_counts,
            "snapshot_expectations": dict(case_doc.get("snapshot_expectations") or {}),
            "game_version": case_doc.get("game_version") or calc.get("game_version"),
        }
        # attribute contributions are also exposed as a derived summary for reports
        derived["attributes_increased_damage_fraction"] = scaling_total

        # -- issues --------------------------------------------------------
        issues = _collect_snapshot_issues(
            case_doc=case_doc,
            calc=calc,
            skill=skill,
            hit_count=hit_count,
            weapon=weapon,
            contributions=contributions,
            mana=mana,
            dataset_id=dataset_id,
        )
        excluded_sources = _excluded_sources(case_doc, calc, skill)

    # -- build_meta (same shape as the Black Hole snapshot) ----------------
    build_meta = _build_meta(
        case=case_doc,
        calc=calc,
        calc_path=calc_path,
        calc_sha=calc_sha,
        dataset_current=dataset_current,
        dataset_meta=dataset_meta,
        dataset_manifest=dataset_manifest,
        build_current=build_current,
        revision=revision,
        build_json_path=build_json_path,
        build_sha=build_sha,
        database_path=database_path,
        database_sha=cached_file_sha256(database_path),
        current_sha=current_sha,
        base=base,
    )
    derived["model"] = HIT_MODEL
    derived["snapshot_schema"] = "le-hit-snapshot-v1"

    return model.Snapshot(
        build_meta=build_meta,
        character={
            "level": level,
            "class": ((calc.get("character") or {}).get("class") or {}).get("name"),
            "mastery": ((calc.get("character") or {}).get("mastery") or {}).get("name"),
            "class_entity_id": ((calc.get("character") or {}).get("class") or {}).get("entity_id"),
            "mastery_entity_id": ((calc.get("character") or {}).get("mastery") or {}).get("entity_id"),
            "class_base": {
                key: class_base.get(key)
                for key in (
                    "baseHealth", "baseMana", "healthPerLevel", "manaPerLevel",
                    "baseStrength", "baseDexterity", "baseIntelligence",
                    "baseAttunement", "baseVitality",
                )
            },
            "quest_attribute_points": int(
                next(
                    (
                        row.value
                        for row in contributions
                        if row.contribution_id == "quest:attribute:all"
                    ),
                    0,
                )
            ),
        },
        attributes={key: float(value) for key, value in attributes.items()},
        resistances=resistances,
        contributions=contributions,
        issues=issues,
        derived=derived,
        excluded_sources=excluded_sources,
    )


def _verify_case_input_hashes(case: dict[str, Any], base: Path) -> None:
    inputs = case.get("inputs") or {}
    expected = case.get("input_sha256") or {}
    for key, rel in inputs.items():
        if not rel or key not in expected:
            continue
        path = _resolve(base, str(rel))
        if not path.is_file():
            continue
        actual = file_sha256(path)
        if actual != expected[key]:
            raise HitInputError(
                f"case input_sha256 mismatch for {key}: file={actual!r} case={expected[key]!r} ({path})"
            )


def _offhand_exalted_factor(calc: dict[str, Any]) -> Optional[float]:
    """Whetstone Gavel unique roll: +97% increased effectiveness of offhand stats."""
    for item in calc.get("items") or []:
        if not isinstance(item, dict) or item.get("instance_id") != "equipment.weapon1":
            continue
        for mod in item.get("mods") or []:
            if mod.get("property") != PROP_OFFHAND_STAT_EFFECT:
                continue
            if mod.get("source") not in ("unique", "unique_mod"):
                continue
            return _num(mod.get("value_numeric")) or None
    return None


def _apply_offhand_exalted_effect(
    contributions: list[model.Contribution],
    *,
    weapon: dict[str, Any],
    calc: dict[str, Any],
) -> tuple[list[model.Contribution], dict[str, Any]]:
    """Scale every offhand item stat by (1 + Whetstone roll) when offhand is Exalted."""
    slots = {slot.get("slot"): slot for slot in weapon.get("slots") or [] if isinstance(slot, dict)}
    offhand = slots.get("weapon2") or {}
    factor = _offhand_exalted_factor(calc)
    if str(offhand.get("rarity")) != "exalted" or not factor or factor <= 0:
        return contributions, {}
    multiplier = 1.0 + float(factor)
    scaled: list[str] = []
    for contribution in contributions:
        if not (
            contribution.raw
            and str(contribution.raw.get("item_instance_id") or "") == "equipment.weapon2"
        ):
            continue
        contribution.value = contribution.value * multiplier
        contribution.value_source = "derived_offhand_exalted_effect"
        contribution.notes = (
            (contribution.notes or "")
            + f" [Whetstone offhand Exalted: x{multiplier:g} effectiveness on all offhand stats]"
        ).strip()
        scaled.append(contribution.contribution_id)
    return contributions, {
        "status": model.STATUS_CONFIRMED,
        "offhand_rarity": offhand.get("rarity"),
        "unique_roll": float(factor),
        "multiplier": multiplier,
        "scaled_contribution_count": len(scaled),
        "scaled_contribution_ids": scaled,
        "note": (
            "Whetstone Gavel: offhand Exalted -> all offhand stats get increased "
            "effectiveness (user rule 2026-11-07)."
        ),
    }


def _apply_weapon_added_policy(
    contributions: list[model.Contribution],
    *,
    weapon: dict[str, Any],
    calc: dict[str, Any],
    policy: str,
) -> list[model.Contribution]:
    """Apply the dual-wield added-damage composition policy (fr-hit-facts R2).

    ``sum``     -> candidate A (reference-runtime consistent): every equipped
                   weapon's added damage enters the pool unchanged.
    ``average`` -> candidate B: divide each weapon-slot added contribution by
                   the number of weapon slots (per-hit average).
    """
    if policy not in WEAPON_ADDED_POLICIES:
        raise HitInputError(
            f"weapon_added_policy must be one of {WEAPON_ADDED_POLICIES}, got {policy!r}"
        )
    if not weapon.get("dual_wield"):
        return contributions
    slots = {slot["instance_id"] for slot in weapon.get("slots") or []}
    if len(slots) < 2:
        return contributions
    out: list[model.Contribution] = []
    for contribution in contributions:
        if not (
            contribution.stat_key == model.STAT_ADDED_DAMAGE
            and contribution.raw
            and str(contribution.raw.get("item_instance_id") or "") in slots
            and contribution.operation == "added"
        ):
            out.append(contribution)
            continue
        if policy == "average":
            contribution.value = contribution.value / len(slots)
            contribution.value_source = "derived_dual_wield_average"
            contribution.notes = (
                (contribution.notes or "")
                + f" [dual-wield per-hit average (alternative, superseded by user rule 2026-11-07): "
                f"divided by {len(slots)} weapon slots]"
            ).strip()
            contribution.status = model.STATUS_ASSUMED
        else:
            contribution.value_source = "derived_dual_wield_sum"
            contribution.notes = (
                (contribution.notes or "")
                + " [dual-wield: both weapons apply, added damage summed (user rule 2026-11-07)]"
            ).strip()
            contribution.status = model.STATUS_CONFIRMED
        if contribution.raw is not None:
            contribution.raw["weapon_added_policy"] = policy
            contribution.raw["weapon_slots"] = len(slots)
        out.append(contribution)
    return out


def _added_spell_damage_total(contributions: Iterable[model.Contribution]) -> float:
    total = 0.0
    for contribution in contributions:
        if not contribution.active:
            continue
        if contribution.stat_key != model.STAT_ADDED_DAMAGE or contribution.operation != "added":
            continue
        tags = set(contribution.damage_tags)
        if "Spell" in tags and not (tags - {"Spell"}):
            total += contribution.value
    return total


def _hit_count_section(
    skill_contributions: list[model.Contribution],
    extras: dict[str, Any],
    skill: dict[str, Any],
) -> dict[str, Any]:
    returns = [str(node) for node in (extras.get("returns") or [])]
    # node 16 Reflash: "expands and returns a second time. This can hit the same
    # enemy two additional times" -> 3 hits per cast total.
    # node 18 Flame Caller alone: one return -> 2 hits per cast.  With both, the
    # Reflash description already covers the additional hits (facts R1: 3).
    # user ruling 2026-11-07: 4 hits per cast = base hit + Reflash (2 returns) +
    # Flame Caller (1 return); extra hits keep the 50% less damage penalty.
    if "16" in returns and "18" in returns:
        per_cast = 4
    elif "16" in returns:
        per_cast = 3
    elif "18" in returns:
        per_cast = 2
    else:
        per_cast = 1
    damage_weights = [1.0] + [0.5] * (per_cast - 1)
    weight_sum = sum(damage_weights)
    return {
        "per_cast": per_cast,
        "base_hits": 1,
        "returns": per_cast - 1,
        "sources": [int(node) if node.isdigit() else node for node in returns],
        "damage_weights": damage_weights,
        "damage_weight_sum": weight_sum,
        "per_hit_average_factor": weight_sum / per_cast if per_cast else 0.0,
        "distance_assumption": (
            "melee range = no falloff (default); ranged falloff is NOT modeled in "
            "this ruleset (only the scenario state target_in_melee_range is "
            "declared; melee-range assumption)."
        ),
        "status": model.STATUS_CONFIRMED,
        "label": "Flame Reave hits per cast against one target (base + return hits, with damage weights)",
        "note": (
            "user ruling 2026-11-07: 4 hits per cast (1 base + Reflash 2 returns + "
            "Flame Caller 1 return); extra hits have the 50% less damage penalty -> "
            "weights [1.0, 0.5, 0.5, 0.5], effective 2.5 first-hit equivalents. "
            "The hit count is configurable via scenario.hit_override."
        ),
        "override_key": "scenario.hit_override",
        "evidence": [
            {
                "kind": "catalog",
                "detail": "fr11mv tree nodes 16/18 description+altText; dataset source_records skill_tree_node fr11mv 16/18",
                "locator": "data/releases/phase1-dataset-003/data.sqlite#source_records entity_kind=skill_tree_node source_scope=fr11mv",
            }
        ],
    }


def _attack_speed_rows(contributions: Iterable[model.Contribution]) -> list[model.Contribution]:
    return [
        contribution
        for contribution in contributions
        if contribution.stat_key == model.STAT_ATTACK_SPEED
    ]


def _attack_speed_section(rows: list[model.Contribution]) -> dict[str, Any]:
    increased = sum(
        row.value
        for row in rows
        if row.active and row.operation == "increased" and row.included
    )
    more_groups: dict[str, float] = {}
    for row in rows:
        if not row.active or not row.included or row.operation != "more":
            continue
        group = row.group or row.contribution_id
        more_groups[group] = more_groups.get(group, 0.0) + row.value
    more_product = 1.0
    for value in more_groups.values():
        more_product *= 1.0 + value
    return {
        "increased_total": increased,
        "more_product": more_product,
        "more_groups": more_groups,
        "rows": [
            {
                "contribution_id": row.contribution_id,
                "operation": row.operation,
                "value": row.value,
                "damage_tags": list(row.damage_tags),
                "condition_code": row.condition_code,
            }
            for row in rows
            if row.included
        ],
        "note": "totals are recomputed by hit_simulate against the scenario states",
    }


def _basic_condition_count(
    contributions: Iterable[model.Contribution],
    *,
    state: str,
) -> int:
    return sum(
        1
        for contribution in contributions
        if contribution.condition_code and state in contribution.condition_code
    )


def _collect_snapshot_issues(
    *,
    case_doc: dict[str, Any],
    calc: dict[str, Any],
    skill: dict[str, Any],
    hit_count: dict[str, Any],
    weapon: dict[str, Any],
    contributions: list[model.Contribution],
    mana: dict[str, Any],
    dataset_id: str,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    def add(issue_id: str, severity: str, title: str, detail: str, scope: str) -> None:
        issues.append(
            {"issue_id": issue_id, "severity": severity, "title": title, "detail": detail, "scope": scope}
        )

    add(
        "issue-hit-001",
        "info",
        "命中次数与伤害权重",
        f"每次施放对同一目标 {hit_count['per_cast']} 次命中（base 1 + returns "
        f"{hit_count['returns']}，来源节点 {hit_count['sources']}）；Reflash/Flame Caller "
        f"altText 给出额外两击 50% less damage → 伤害权重 {hit_count.get('damage_weights')}。"
        "第 1 击按贴脸距离 1.0（距离衰减曲线未建模，OQ2）。",
        "hit_count",
    )
    add(
        "issue-hit-002",
        "info",
        "技能等级 27 只影响节点数量，不缩放伤害（用户确认 2026-11-07）",
        "离线存档显示 bug：基础 20 级 + 装备等级词条，实际 27 级。等级只决定可分配的技能节点数量，"
        "不改变技能伤害；FR 已按 27 点建树（points_total=27）。因此不需要 level scaling 伤害建模。",
        "skill_level",
    )
    add(
        "issue-hit-003",
        "info",
        "双持合成口径（用户确认 2026-11-07）",
        f"用户裁决：两把武器都生效，added damage 直接相加（不取平均）；当前 added_damage_policy="
        f"{weapon.get('added_damage_policy')!r}。候选 B（eps=平均 attackRate "
        f"{weapon.get('average_attack_rate'):g}、每击平均）仅作为备选口径记录，不用于主口径。",
        "weapon",
    )
    add(
        "issue-hit-004",
        "info",
        "攻速基础公式（用户确认 2026-11-07）",
        "用户裁决：base_aps = as × speedMultiplier（=1.1×speedMultiplier/useDuration）正确；"
        "最终 rate = base_aps × (1+Σincreased AS) × Π(1+more AS)，武器 attackRate 不参与。",
        "attack_speed",
    )
    add(
        "issue-hit-011b",
        "info",
        "Frozen Steel 附加冷伤害未生成独立组件",
        "Frozen Steel 被动 +4 melee cold damage 属于 Cold 组件；本轮口径为 fire_only 单组件，"
        "冷伤害未计入（不静默：在 ruleset R19 与本 issue 登记，OQ9 待定）。",
        "damage_model",
    )
    add(
        "issue-hit-011c",
        "info",
        "OQ6 暴击类被动已计入暴击率（用户确认 2026-11-07）",
        "Prodigy（+1% crit per 15 Int）与 Defender of Welryn（increased crit per Dex）按快照值计入 "
        "crit chance；per-attribute 缩放的精确公式仍未验证（R12/新 open question）。",
        "critical",
    )
    add(
        "issue-hit-005",
        "warning",
        "法力消耗为估算值",
        f"Flame Reave 法力消耗估算 {mana['skill_mana_cost_estimate']:g}"
        f"（base {skill.get('mana_cost')}，flat +{mana['skill_mana_cost_flat_added']:g}，"
        f"increased {mana['skill_mana_cost_increased']:g}）；仅用于 ≥10 mana 条件判定，"
        "资源恢复未模拟。",
        "resource",
    )
    add(
        "issue-hit-006",
        "info",
        "300 法力条件不满足",
        f"max mana 估算 {mana['mana_max_estimate']:g} < 300：4 条神像 'doubled if 300+ mana' "
        "按基础值计入，双倍加成列为 mana_ge_300 条件效果（默认 not_satisfied）。",
        "conditions",
    )
    dot_rows = [
        row
        for row in contributions
        if row.stat_key == model.STAT_UNSUPPORTED
        and row.exclusion_reason
        and (
            "DoT-only" in row.exclusion_reason
            or "Flay/Harvest" in row.exclusion_reason
            or "offhand is a Rare" in row.exclusion_reason
        )
    ]
    if dot_rows:
        add(
            "issue-hit-007",
            "info",
            "非命中/非本技能词条已排除",
            "DoT 专用（4152 tags）、feet 49% more Global Conditional Damage（Flay/Harvest）、"
            "weapon1 offhand-Exalted 词条均不进入 Flame Reave 命中，详见 contributions 的 "
            "exclusion_reason。",
            "mapping",
        )
    if calc.get("excluded_non_dps"):
        add(
            "issue-hit-008",
            "info",
            "export 已排除的非 DPS 属性",
            f"calculation input 标记 excluded_non_dps={calc.get('excluded_non_dps')}"
            f"，properties={calc.get('excluded_non_dps_properties')!r}（未计入 mods）。",
            "mapping",
        )
    add(
        "issue-hit-009",
        "info",
        "条件状态默认关闭",
        "baseline 状态（ruleset/legacy profile）：ignited=false, enchant_weapon_active=false, "
        "frenzy_active=false, rhythm_of_fire_stacks=0, firebrand_stacks=0, "
        "blade_weaving_stacks=0, mana_ge_300=false；build_base profile 可打开 "
        "enchant_weapon_active；其余条件由 scenario states 切换，效果定义在 ruleset.effects。",
        "conditions",
    )
    if any(
        row.condition_status == model.CONDITION_UNKNOWN and row.included
        for row in contributions
    ):
        unknown = [
            row.contribution_id
            for row in contributions
            if row.condition_status == model.CONDITION_UNKNOWN and row.included
        ]
        add(
            "issue-hit-011",
            "warning",
            "未能判定的条件词条",
            "以下 contribution 的条件无法用 baseline states 判定，默认不进入命中池并保留在"
            f"筛查中：{unknown}",
            "conditions",
        )
    add(
        "issue-hit-010",
        "info",
        "目标侧假设",
        "目标抗性按 0 处理；穿透 ×(1+pen)；怪物 DR 按 monster_level 查表；"
        "Boss/稀有目标标记只影响需要该条件的词条。",
        "target",
    )
    return issues


def _excluded_sources(
    case: dict[str, Any],
    calc: dict[str, Any],
    skill: dict[str, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ability_id = skill.get("ability_id")
    for spec in (calc.get("skills") or {}).get("specializations") or []:
        owner = ((spec.get("owner_skill") or {}).get("entity_id") or "")
        spec_id = owner.rsplit(":", 1)[-1] if owner else (spec.get("tree") or {}).get("entity_id", "")
        if spec_id and ability_id and spec_id.endswith(str(ability_id)):
            continue
        out.append(
            {
                "source_id": f"skill_tree:{spec_id}",
                "label": (spec.get("tree") or {}).get("name") or spec_id,
                "reason": "other specialization; only its conditional buffs are declared in ruleset.effects",
                "category": "scope",
            }
        )
    scope = calc.get("scope") or {}
    for feature in scope.get("excluded_features") or []:
        out.append(
            {
                "source_id": feature.get("feature"),
                "label": feature.get("feature"),
                "reason": feature.get("reason"),
                "category": "scope",
            }
        )
    out.append(
        {
            "source_id": "ailments:ignite",
            "label": "Ignite DoT",
            "reason": "hit model scope: only direct hit damage is computed; ignite damage is not part of this scope",
            "category": "scope",
        }
    )
    return out


def _build_meta(
    *,
    case: dict[str, Any],
    calc: dict[str, Any],
    calc_path: Path,
    calc_sha: str,
    dataset_current: dict[str, Any],
    dataset_meta: dict[str, Any],
    dataset_manifest: dict[str, Any],
    build_current: dict[str, Any],
    revision: dict[str, Any],
    build_json_path: Path,
    build_sha: str,
    database_path: Path,
    database_sha: str,
    current_sha: Optional[str],
    base: Path,
) -> dict[str, Any]:
    revision_meta = (revision.get("meta") or {}).get("revision") or {}
    source_payload = (revision.get("meta") or {}).get("source_payload") or {}
    dataset_manifest_db = dataset_manifest.get("database") or {}
    build_cfg = case.get("build") or {}
    return {
        "build": {
            "build_id": calc.get("build_id") or build_cfg.get("build_id"),
            "revision_id": calc.get("revision_id"),
            "content_sha256": calc.get("content_sha256"),
            "revision_seq": calc.get("revision_seq"),
            "previous_revision_id": revision_meta.get("previous_revision_id"),
        },
        "dataset": {
            "dataset_id": dataset_current.get("dataset_id") or calc.get("dataset_id"),
            "target_game_version": dataset_meta.get("target_game_version") or dataset_current.get("target_game_version"),
            "logical_content_sha256": dataset_current.get("logical_content_sha256")
            or dataset_manifest_db.get("logical_content_sha256"),
            "file_sha256": dataset_current.get("file_sha256") or database_sha,
        },
        "calc_input": {
            "path": _relative(base, calc_path),
            "sha256": calc_sha,
            "mod_count": len(calc.get("mods") or []),
            "unresolved_inputs": len(calc.get("unresolved_inputs") or []),
            "schema_version": calc.get("schema_version"),
            "generated_at": calc.get("generated_at"),
        },
        "revision_selection": {
            "pinned_from": "calculation-input.revision_id",
            "revision_id": calc.get("revision_id"),
            "current_pointer_revision_id": build_current.get("revision_id"),
            "current_pointer_drift": build_current.get("revision_id") != calc.get("revision_id"),
        },
        "build_spec_sha256": build_sha,
        "content_sha256": calc.get("content_sha256"),
        "calc_input_sha256": calc_sha,
        "database_sha256": database_sha,
        "dataset_logical_content_sha256": dataset_current.get("logical_content_sha256"),
        "current_pointer_sha256": current_sha,
        "source_raw_sha256": source_payload.get("raw_sha256") or build_cfg.get("build_spec_sha256"),
        "game_version": calc.get("game_version") or case.get("game_version"),
        "revision_published_at": revision_meta.get("published_at"),
        "database_path": _relative(base, database_path),
        "build_spec_path": _relative(base, build_json_path),
    }


# ---------------------------------------------------------------------------
# Serialisation helpers (caller/Lead decides when to write files)
# ---------------------------------------------------------------------------


def snapshot_document(snapshot: model.Snapshot | dict[str, Any]) -> dict[str, Any]:
    if isinstance(snapshot, model.Snapshot):
        return snapshot.to_dict()
    if isinstance(snapshot, dict):
        return dict(snapshot)
    raise HitInputError(f"snapshot must be model.Snapshot or dict, got {type(snapshot).__name__}")


def build_snapshot_dict(
    case: str | Path | dict[str, Any],
    *,
    calc_input_path: str | Path | None = None,
    dataset_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    states: dict[str, Any] | None = None,
    verify_input_sha256: bool = True,
) -> dict[str, Any]:
    """``build_snapshot`` that returns the JSON-safe document dict."""
    snapshot = build_snapshot(
        case,
        calc_input_path=calc_input_path,
        dataset_path=dataset_path,
        repo_root=repo_root,
        states=states,
        verify_input_sha256=verify_input_sha256,
    )
    return snapshot_document(snapshot)


def snapshot_json(snapshot: model.Snapshot | dict[str, Any]) -> str:
    """Deterministic snapshot JSON text (sorted keys, UTF-8, trailing newline)."""
    return json.dumps(snapshot_document(snapshot), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def dump_snapshot(
    snapshot: model.Snapshot | dict[str, Any],
    path: str | Path,
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(snapshot_json(snapshot).encode("utf-8"))
    return target


__all__ = [
    "CALC_SCHEMA_VERSION",
    "DEFAULT_HIT_STATES",
    "FR_SKILL_NODE_SEMANTICS",
    "HIT_MODEL",
    "HitInputError",
    "build_snapshot",
    "build_snapshot_dict",
    "decode_tag_names",
    "dump_snapshot",
    "load_case",
    "map_mod",
    "snapshot_document",
    "snapshot_json",
]
