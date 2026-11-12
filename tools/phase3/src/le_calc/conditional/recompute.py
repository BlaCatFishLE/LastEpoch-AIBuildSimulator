"""Conditional Buff input channel: feed conditional proposals into the damage engine.

Contract: ``docs/contracts/calc-channel.md`` (schema ``bh-conditional-recompute-v1``)
and ``docs/contracts/conditional-interface.md`` sections 7-9 (scenario /
evaluation / proposed-contribution shapes).

Design boundaries
-----------------
* This module is a **consumer**: it never edits ``report.py``, ``cli.py``, the
  registry JSON or the frozen inputs.  The Lead attaches the result to the run
  artefacts.
* Inputs are deep-copied; neither the snapshot nor the ruleset nor the scenario
  is mutated.
* The baseline is the legacy DPS口径: original snapshot + the ruleset scenario
  built exactly like ``report.build_result`` (target DR included).  With
  ``include_auto=False`` and a build-base scenario it must reproduce the legacy
  ``result.json`` single/loop numbers.
* Conditional contributions are :class:`le_calc.model.Contribution` rows with
  ``source_kind="conditional_buff"``, ``included=True`` and
  ``condition_status="satisfied"``; the damage engine consumes them through the
  normal snapshot path.  Engine branches are selected with ``damage_tags``
  (contract section 3): body components carry ``Spell``, Ignite carries
  ``DoT``/``Ailment`` and never ``Spell``.
* Single-cast vs loop channels: ``CastState`` proposals (Crescendoing Rage,
  Preparation) are applied to the single-cast output only and reported as
  skipped for loops (a loop needs per-cast phase simulation).

The public API is :func:`recompute_scenario`.  It never raises for data
problems; degraded inputs return ``status="skipped"``/``"error"`` plus readable
``warnings``.
"""

from __future__ import annotations

import copy
from typing import Any, Optional

from .. import model, monster_dr
from ..damage import BODY_COLD_TAGS, BODY_FIRE_TAGS, IGNITE_TAGS, tags_match
from ..report import DEFAULT_SCENARIO_NOTE
from ..ruleset import Ruleset
from ..simulate import run_loop, run_single_cast
from .evaluate import evaluate_scenario
from .registry import Registry
from .scenario import normalize_scenario, scenario_fingerprint, validate_scenario

#: Output schema id (contract section 2).
SCHEMA = "bh-conditional-recompute-v1"

#: Snapshot source kind for conditional contributions.
SOURCE_KIND_CONDITIONAL = "conditional_buff"

#: Prepared-scenario document / glyph sub-model schema ids.
PREPARED_SCHEMA = "bh-conditional-prepared-scenario-v1"
GLYPH_MODEL_SCHEMA = "bh-conditional-glyph-model-v1"

#: Apex of Thought + Glyph of Dominion conversion channel.
APEX_EFFECT_ID = "equipment.apex_glyph_conversion"
APEX_MODIFIER_ID = "equipment.apex_glyph_conversion.ignite_to_fire_shred"
GLYPH_STATE_ID = "player.standing_on_glyph_of_dominion"
#: Bosses/players take 60% less effect from Fire Resistance Shred -> x0.4.
#: The registry has no value for this multiplier yet (Lead can add
#: ``fire_res_shred_boss_effectiveness`` to the Apex values later; the loader
#: below prefers such a registry value when present).
GLYPH_BOSS_EFFECTIVENESS = 0.4

GLYPH_ASSUMPTION_STACKS = (
    "Glyph 模式 shred 覆盖率假设：Heat Wave 光环每 0.5s 判定、Fire Resistance Shred "
    "基础持续 4s（× Apex 自身 +52% 异常持续 ≈ 6.08s），观察期内按维持 10 层（满层）估算；"
    "不模拟层数爬升、断档与移动。"
)
GLYPH_ASSUMPTION_HEAT_WAVE = (
    "Glyph 模式：Apex 把站桩期间 100% 的 Ignite Chance 转为 Fire Resistance Shred Chance，"
    "因此 Heat Wave 不再施加 Ignite（引擎内 heat_wave points 置 0 → BH Ignite 伤害按 0 结算），"
    "转换出的 shred 以目标火焰抗性降低表示（与火焰穿透在目标抗性上相加）。"
)
GLYPH_ASSUMPTION_BOSS = (
    "Fire Resistance Shred 对 Boss 目标（target.boss=true）60% less effective → ×0.4；"
    "稀有但非 Boss（boss=false, rare=true）以及普通目标按 ×1.0（R8 独立标志）。"
)
GLYPH_ASSUMPTION_MANUAL_STATE = (
    "当前构筑没有 Glyph of Dominion 技能来源；“站在 Glyph 上”由用户在条件场景中人工声明，"
    "不是引擎可验证的构筑属性。"
)

#: Registry ``stat_key`` -> engine ``model.STAT_*`` vocabulary.
STAT_ALIASES = {
    "damage": model.STAT_DAMAGE,
    "added_damage": model.STAT_ADDED_DAMAGE,
    "penetration": model.STAT_PENETRATION,
    "cast_speed": model.STAT_CAST_SPEED,
    "ailment_duration": model.STAT_AILMENT_DURATION,
    "cooldown_recovery_speed": model.STAT_COOLDOWN_RECOVERY,
    "skill_duration": model.STAT_SKILL_DURATION_INCREASED,
}

#: stat keys whose value is scoped by ``damage_tags`` in the damage engine.
TAG_SCOPED_STATS = frozenset(
    {model.STAT_DAMAGE, model.STAT_ADDED_DAMAGE, model.STAT_PENETRATION}
)

#: operations the damage engine understands (``model.Contribution``).
SUPPORTED_OPERATIONS = ("added", "increased", "more")

#: computation-module branch -> engine component ids it maps onto.
_BRANCH_COMPONENTS = {
    "body": ("body-fire", "body-cold"),
    "body.aggregate": ("body-fire", "body-cold"),
    "body.fire": ("body-fire",),
    "body_fire": ("body-fire",),
    "body.cold": ("body-cold",),
    "body_cold": ("body-cold",),
    "ignite": ("ignite",),
    "ignite.application": ("ignite",),
    "ailment": ("ignite",),
}

#: engine component id -> tag profile (used to prove that chosen tags express
#: exactly the requested branches and nothing else).
_ENGINE_COMPONENTS = {
    "body-fire": BODY_FIRE_TAGS,
    "body-cold": BODY_COLD_TAGS,
    "ignite": IGNITE_TAGS,
}

_ALL_COMPONENTS = frozenset(_ENGINE_COMPONENTS)

#: branchless/tagless damage would leak onto every component; these are the
#: minimal tag sets that target one component subset.
_CANONICAL_TAGS = {
    frozenset(_ALL_COMPONENTS): [],
    frozenset({"body-fire", "body-cold"}): ["Spell"],
    frozenset({"body-fire"}): ["Fire"],
    frozenset({"body-cold"}): ["Cold"],
    frozenset({"ignite"}): ["Ailment"],
    frozenset({"body-fire", "ignite"}): ["Fire"],
}

#: extra restriction tags tried when a proposal's own tags over-apply.
_RESTRICTION_TAGS = (["Ailment"], ["Spell"], ["DoT"], ["Fire"], ["Cold"])


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return number


def _pct(delta: float, base: float) -> float:
    return (delta / base) if base else 0.0


def _percent_change_delta(base: float, scenario: float) -> tuple[float, float]:
    delta = scenario - base
    return delta, _pct(delta, base)


def _matched_components(tags: list[str]) -> frozenset[str]:
    """Components a modifier with ``tags`` applies to (engine semantics)."""
    return frozenset(
        component_id
        for component_id, profile in _ENGINE_COMPONENTS.items()
        if tags_match(tags, profile)
    )


def _desired_components(branches: Any) -> frozenset[str]:
    desired: set[str] = set()
    for branch in branches or []:
        for component_id in _BRANCH_COMPONENTS.get(str(branch), ()):
            desired.add(component_id)
    return frozenset(desired)


def _resolve_damage_tags(
    raw_tags: Any, branches: Any
) -> tuple[list[str], str]:
    """Return ``(tags, note)`` that make the engine apply exactly ``branches``.

    Contract rule (section 3): branches only ``body.fire``/``body.cold`` and
    empty tags -> ``["Spell"]``; ignite-only -> ``["Ailment"]``; body+ignite
    keeps the original tags.  Rather than trusting the rule blindly the chosen
    tag set is verified against the three engine components; an inexpressible
    branch set returns an empty tag list plus a reason (caller skips it).
    """
    desired = _desired_components(branches)
    raw = [str(tag) for tag in (raw_tags or []) if tag]
    if not desired:
        return [], "no damage branch declared"
    candidates: list[list[str]] = []
    if raw:
        candidates.append(list(raw))
    canonical = _CANONICAL_TAGS.get(desired)
    if canonical is not None and canonical not in candidates:
        candidates.append(list(canonical))
    if raw:
        for extra in _RESTRICTION_TAGS:
            merged = list(raw) + [tag for tag in extra if tag not in raw]
            if merged not in candidates:
                candidates.append(merged)
    for candidate in candidates:
        if _matched_components(candidate) == desired:
            return candidate, ""
    return [], "branches cannot be expressed by engine damage_tags"


def _registry_effect(registry: Any, effect_id: str) -> Optional[dict[str, Any]]:
    try:
        effect = registry.effect(effect_id)
    except Exception:  # pragma: no cover - defensive; not a registry-like object
        return None
    return effect if isinstance(effect, dict) else None


def _registry_identity(registry: Any) -> dict[str, Any]:
    try:
        return {
            "id": str(registry.registry_id),
            "sha256": registry.sha256,
        }
    except Exception:  # pragma: no cover - defensive
        return {"id": None, "sha256": None}


def _binding_from_catalog(catalog: Any) -> dict[str, Any]:
    if isinstance(catalog, dict) and isinstance(catalog.get("binding"), dict):
        return dict(catalog["binding"])
    return {}


# ---------------------------------------------------------------------------
# Baseline scenario (legacy口径, mirrors report.build_result)
# ---------------------------------------------------------------------------


def _resolve_monster_level(
    ruleset: Ruleset, monster_level: Optional[int], warnings: list[str]
) -> tuple[int, dict[str, Any]]:
    default = int((ruleset.target_side.get("monster_dr") or {}).get("default_level", 0))
    selected = default if monster_level is None else monster_level
    try:
        info = monster_dr.describe(int(selected))
        return int(info["monster_level"]), info
    except Exception as exc:  # invalid level / missing data -> documented fallback
        warnings.append(
            f"monster_level={selected!r} 不可用（{exc}）；回退到规则包默认等级 {default}。"
        )
        info = monster_dr.describe(default)
        return int(info["monster_level"]), info


def _engine_scenario(
    ruleset: Ruleset,
    mr_info: dict[str, Any],
    *,
    target_conditions: Optional[dict[str, Any]] = None,
    target_params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build the DPS scenario exactly like ``report.build_result``.

    This is duplicated (not imported) because ``report.py`` is Lead-owned and
    must stay untouched; the unit tests pin the two constructions together by
    comparing the baseline numbers with the frozen legacy ``result.json``.

    ``target_params`` applies the unified conditional-scenario target block
    (fire resistance / independent boss+rare flags) on top of the ruleset
    target side; both the baseline and the scenario environment use the same
    target parameters so their comparison is like-for-like (R1).
    """
    scenario = copy.deepcopy(dict(ruleset.scenario))
    scenario["target"] = {
        "target_id": f"{ruleset.target_side.get('scenario_id', 'scenario')}-target",
        "label": ruleset.target_side.get("target_label", "synthetic target"),
        "boss_tag": bool(ruleset.target_side.get("boss_tag", True)),
        "rare_tag": bool(ruleset.target_side.get("rare_tag", True)),
        "resistances": dict(ruleset.target_side.get("base_resistances", {})),
        "negative_resistance_allowed": bool(
            ruleset.target_side.get("negative_resistance_allowed", True)
        ),
        "notes": DEFAULT_SCENARIO_NOTE,
    }
    if isinstance(target_params, dict):
        resistances = scenario["target"].get("resistances")
        resistances = dict(resistances) if isinstance(resistances, dict) else {}
        resistances["fire"] = float(target_params.get("fire_resistance", 0.0) or 0.0)
        scenario["target"]["resistances"] = resistances
        scenario["target"]["boss_tag"] = bool(target_params.get("boss", True))
        scenario["target"]["rare_tag"] = bool(target_params.get("rare", True))
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
    if mr_info["monster_level"] == 0:
        level_label = "、无怪物 DR"
    else:
        level_label = (
            f"、怪物等级 {mr_info['monster_level']}"
            f"（DR {mr_info['monster_damage_reduction'] * 100:.0f}%）"
        )
    scenario.setdefault("scenario_id", ruleset.target_side.get("scenario_id", "scenario"))
    scenario.setdefault("name", "Boss、0 抗性、双星全覆盖、中心 1 米内")
    if not scenario.get("name", "").endswith(level_label):
        scenario["name"] = scenario["name"] + level_label
    if target_conditions is not None:
        # Conditional scenario: user-selected target states override the
        # ruleset tree-more conditions (Supermassive / Weakened Reality).
        current = scenario.get("target_conditions")
        current = dict(current) if isinstance(current, dict) else {}
        for key, value in target_conditions.items():
            current[str(key)] = bool(value)
        scenario["target_conditions"] = current
    scenario["coverage"] = {
        "both_stars_cover_target": bool(
            scenario["target_conditions"].get("both_stars_cover", True)
        ),
        "center_condition_met": bool(
            scenario["target_conditions"].get("target_within_center_1m", True)
        ),
        "theoretical": True,
        "note": "双星全覆盖与中心条件同时满足是理论最优场景，不代表真实轨道物理可达。",
    }
    return scenario


# ---------------------------------------------------------------------------
# Conditional scenario state -> target-condition overrides
# ---------------------------------------------------------------------------


def _copy_ruleset(
    ruleset: Ruleset, *, fire_resistance: Optional[float] = None
) -> Ruleset:
    """Deep-copied ruleset (the prepared scenario never aliases caller data).

    ``fire_resistance`` is written into ``target_side.base_resistances.fire``
    because the Ignite target-multiplier path reads the ruleset target side
    (not the scenario target); this keeps Ignite consistent with the unified
    scenario target (R1).
    """
    data = copy.deepcopy(ruleset.data)
    if fire_resistance is not None:
        target_side = data.get("target_side")
        if isinstance(target_side, dict):
            resistances = target_side.get("base_resistances")
            if not isinstance(resistances, dict):
                resistances = {}
                target_side["base_resistances"] = resistances
            resistances["fire"] = float(fire_resistance)
    return Ruleset(data=data, path=ruleset.path, sha256=ruleset.sha256)


def _scenario_ruleset(
    ruleset: Ruleset,
    target_conditions: Optional[dict[str, Any]],
    assumptions: list[str],
    *,
    disable_heat_wave: bool = False,
    fire_resistance: Optional[float] = None,
) -> Ruleset:
    """Derive a deep-copied ruleset for the conditional scenario engine run.

    Two scenario-specific engine gaps are handled here:

    * ``damage.compute_body`` filters ``ruleset.tree_more_effects`` by
      ``condition_met``, but the Ignite inheritance path
      (``ignite._ignite_scaling``) copies inherited skill-tree "more" rows
      without consulting the scenario.  Removing the disabled conditioned nodes
      makes target-condition overrides effective on both body and Ignite
      (e.g. Supermassive +100% more drops when ``target.boss_or_rare`` is
      unchecked).
    * Glyph mode converts all Heat Wave Ignite chance into Fire Resistance
      Shred, so the engine's Heat Wave application is zeroed (points=0 →
      ``ignite_total`` becomes 0) while the shred is modelled on the scenario
      target resistance.

    The caller's ruleset and the baseline口径 stay untouched.
    """
    data = copy.deepcopy(ruleset.data)
    if fire_resistance is not None:
        target_side = data.get("target_side")
        if isinstance(target_side, dict):
            resistances = target_side.get("base_resistances")
            if not isinstance(resistances, dict):
                resistances = {}
                target_side["base_resistances"] = resistances
            resistances["fire"] = float(fire_resistance)
    nodes_changed = False
    if target_conditions:
        disabled_codes = {
            str(code) for code, value in target_conditions.items() if value is False
        }
        if disabled_codes:
            for node in data.get("skill_tree_nodes") or []:
                if not isinstance(node, dict):
                    continue
                effects = node.get("effects")
                if not isinstance(effects, list):
                    continue
                kept = [
                    effect
                    for effect in effects
                    if not (
                        isinstance(effect, dict)
                        and str(effect.get("condition_code") or "") in disabled_codes
                    )
                ]
                if len(kept) != len(effects):
                    node["effects"] = kept
                    nodes_changed = True
    if nodes_changed:
        assumptions.append(
            "目标条件重算对 Ignite 继承的技能树 more 使用规则包派生副本："
            "条件未满足的节点（如 Supermassive / Weakened Reality）在场景运行中移除；"
            "调用方 ruleset 与基线口径不变。"
        )
    if disable_heat_wave:
        ignite = data.get("ignite")
        if isinstance(ignite, dict):
            heat_wave = ignite.get("heat_wave")
            if not isinstance(heat_wave, dict):
                heat_wave = {}
                ignite["heat_wave"] = heat_wave
            heat_wave["points"] = 0
            heat_wave["chance_per_point_per_uncapped_fire_res"] = 0.0
            heat_wave["conditional_zeroed_by"] = "equipment.apex_glyph_conversion"
        data["conditional_zeroed_ignite_application"] = True
        assumptions.append(GLYPH_ASSUMPTION_HEAT_WAVE)
    return Ruleset(data=data, path=ruleset.path, sha256=ruleset.sha256)


def _target_condition_overrides(
    normalized_scenario: dict[str, Any],
    target_params: dict[str, Any],
    warnings: list[str],
    assumptions: list[str],
) -> dict[str, bool]:
    """Engine target conditions derived from unified target + user states.

    ``target_is_boss_or_rare`` is derived from the independent ``boss`` /
    ``rare`` target flags ("boss OR rare", R8); the center/both-stars states
    stay user-declared conditional flags.
    """
    states = normalized_scenario.get("states")
    states = states if isinstance(states, dict) else {}

    def read(state_id: str) -> bool:
        value = states.get(state_id)
        if isinstance(value, bool):
            return value
        return True  # registry build-base default: theoretical best-case target

    overrides = {
        "target_is_boss_or_rare": bool(
            target_params.get("boss", True) or target_params.get("rare", True)
        ),
        "target_within_center_1m": read("target.within_center_1m"),
        "both_stars_cover": read("target.both_stars_cover"),
    }
    if not overrides["both_stars_cover"]:
        warnings.append(
            "target.both_stars_cover=false：双星覆盖是结构/轨道条件，固定条件伤害引擎不消费该状态"
            "（无法建模 Structural 变化）；本次重算数值未因该条件调整，仅记录状态。"
        )
        assumptions.append(
            "both_stars_cover=false 不被引擎消费：引擎只按 penetration / increased / more 与 "
            "target_conditions.target_is_boss_or_rare / target_within_center_1m 结算；"
            "双星覆盖变化不在固定条件重算范围内。"
        )
    return overrides


# ---------------------------------------------------------------------------
# Apex of Thought + Glyph of Dominion (Fire Resistance Shred conversion)
# ---------------------------------------------------------------------------


def _effect_value_constant(
    effect: dict[str, Any], keys: tuple[str, ...]
) -> tuple[Optional[float], Optional[str]]:
    """Resolve a constant registry ``values`` entry by candidate keys."""
    for value in effect.get("values") or []:
        if not isinstance(value, dict):
            continue
        key = str(value.get("key") or "")
        if key not in keys:
            continue
        number = _as_float(value.get("constant"))
        if number is not None:
            return number, key
    return None, None


def _apex_glyph_config(registry: Any) -> dict[str, Any]:
    """Fire Resistance Shred parameters (registry-first, documented fallbacks)."""
    effect = _registry_effect(registry, APEX_EFFECT_ID) or {}
    per_stack, per_key = _effect_value_constant(
        effect, ("fire_res_shred_per_stack", "per_stack", "shred_per_stack")
    )
    max_stacks, max_key = _effect_value_constant(
        effect, ("fire_res_shred_max_stacks", "max_stacks", "shred_max_stacks")
    )
    duration, duration_key = _effect_value_constant(
        effect, ("fire_res_shred_duration_s", "duration_s", "shred_duration_s")
    )
    boss, boss_key = _effect_value_constant(
        effect,
        (
            "fire_res_shred_boss_effectiveness",
            "boss_effectiveness",
            "boss_effect_multiplier",
        ),
    )
    return {
        "per_stack": per_stack if per_stack is not None else 0.05,
        "per_stack_source": f"registry:{per_key}" if per_key else "default:0.05",
        "max_stacks": int(round(max_stacks)) if max_stacks is not None else 10,
        "max_stacks_source": f"registry:{max_key}" if max_key else "default:10",
        "duration_s": duration if duration is not None else 4.0,
        "duration_source": f"registry:{duration_key}" if duration_key else "default:4.0",
        "boss_effectiveness": (
            boss if boss is not None else GLYPH_BOSS_EFFECTIVENESS
        ),
        "boss_effectiveness_source": (
            f"registry:{boss_key}" if boss_key else f"constant:{GLYPH_BOSS_EFFECTIVENESS}"
        ),
    }


def _apex_ailment_duration_increase(
    snapshot: model.Snapshot,
) -> tuple[float, list[str]]:
    """Sum the Apex item's own ``ailment_duration`` rows (global +52% in build).

    Only Apex-of-Thought rows are attributed to the converted shred duration
    (4s × 1.52 ≈ 6.08s per the registry research note); global ailment-duration
    sources are not extrapolated because the shred-duration interaction is not
    confirmed.  Returns ``(increase, contribution_ids)``.
    """
    total = 0.0
    rows: list[str] = []
    try:
        contributions = list(snapshot.contributions)
    except Exception:  # pragma: no cover - defensive; not a Snapshot
        return 0.0, []
    for contribution in contributions:
        try:
            if not contribution.active:
                continue
        except Exception:  # pragma: no cover - defensive
            continue
        if contribution.stat_key != model.STAT_AILMENT_DURATION:
            continue
        if contribution.operation not in ("increased", "added"):
            continue
        contribution_id = str(contribution.contribution_id or "")
        if not contribution_id.startswith("mod:equipment.head"):
            continue
        total += float(contribution.value)
        rows.append(contribution_id)
    return total, rows


def _catalog_effect_available(
    catalog: Any, effect_id: str
) -> Optional[bool]:
    """``qualification.available`` for one effect (``None`` when unknown)."""
    if not isinstance(catalog, dict):
        return None
    for entry in catalog.get("effects") or []:
        if not isinstance(entry, dict) or entry.get("effect_id") != effect_id:
            continue
        qualification = entry.get("qualification")
        if isinstance(qualification, dict) and "available" in qualification:
            return bool(qualification.get("available"))
        return None
    return None


def _glyph_model_disabled(reason: str) -> dict[str, Any]:
    return {
        "schema": GLYPH_MODEL_SCHEMA,
        "enabled": False,
        "reason": reason,
        "effect_id": APEX_EFFECT_ID,
        "modifier_id": APEX_MODIFIER_ID,
        "state_id": GLYPH_STATE_ID,
        "requires": [GLYPH_STATE_ID, APEX_EFFECT_ID],
        "heat_wave_disabled": False,
        "ignite_application": "unchanged",
    }


def _resolve_glyph_mode(
    *,
    snapshot: model.Snapshot,
    ruleset: Ruleset,
    registry: Any,
    catalog: Any,
    normalized_scenario: dict[str, Any],
    target_conditions: dict[str, bool],
    target_params: dict[str, Any],
    engine_scenario: dict[str, Any],
    assumptions: list[str],
    warnings: list[str],
) -> tuple[bool, dict[str, Any]]:
    """Detect the Glyph conversion state and apply the shred model if active.

    Glyph mode requires ``player.standing_on_glyph_of_dominion == true`` AND the
    Apex effect enabled/available (the Apex toggle *is* the glyph-state control,
    so enabling Apex normalizes the shared state to true).  When active:

    * the scenario target fire resistance is lowered by the shred amount
      (additive with fire penetration, matching the engine's
      ``effective = resistance - penetration`` formula);
    * the engine ruleset copy later zeroes Heat Wave ignite chance (done by the
      caller through ``_scenario_ruleset(disable_heat_wave=True)``);
    * the model metadata is returned for the page/artifacts.

    Returns ``(enabled, glyph_model)``; mutates only ``engine_scenario`` and the
    caller-provided assumption/warning lists.
    """
    if not isinstance(registry, Registry):
        return False, _glyph_model_disabled("registry_unavailable")
    effect = _registry_effect(registry, APEX_EFFECT_ID)
    if effect is None:
        return False, _glyph_model_disabled("effect_not_registered")
    availability = _catalog_effect_available(catalog, APEX_EFFECT_ID)
    if availability is False:
        return False, _glyph_model_disabled("not_available")
    states = normalized_scenario.get("states")
    states = states if isinstance(states, dict) else {}
    effects_config = normalized_scenario.get("effects")
    effects_config = effects_config if isinstance(effects_config, dict) else {}
    apex_config = effects_config.get(APEX_EFFECT_ID)
    apex_enabled = (
        bool(apex_config.get("enabled"))
        if isinstance(apex_config, dict)
        else False
    )
    glyph_state = states.get(GLYPH_STATE_ID) is True
    if not apex_enabled:
        return False, _glyph_model_disabled("apex_not_selected")
    if not glyph_state:
        return False, _glyph_model_disabled("glyph_state_off")

    config = _apex_glyph_config(registry)
    # R8: shred uses the independent boss flag only (rare-but-not-boss = x1.0);
    # Supermassive keeps using the combined boss OR rare condition.
    boss = bool(target_params.get("boss", True))
    rare = bool(target_params.get("rare", True))
    boss_effectiveness = (
        float(config["boss_effectiveness"]) if boss else 1.0
    )
    max_stacks = int(config["max_stacks"])
    per_stack = float(config["per_stack"])
    total_shred = per_stack * max_stacks * boss_effectiveness
    apex_duration_increase, duration_rows = _apex_ailment_duration_increase(snapshot)
    effective_duration_s = float(config["duration_s"]) * (
        1.0 + apex_duration_increase
    )

    target = engine_scenario.get("target")
    target = target if isinstance(target, dict) else {}
    engine_scenario["target"] = target
    resistances = target.get("resistances")
    resistances = resistances if isinstance(resistances, dict) else {}
    target["resistances"] = resistances
    fire_before = float(resistances.get("fire", 0.0) or 0.0)
    fire_after = fire_before - total_shred
    resistances["fire"] = fire_after
    target["fire_resistance_shred"] = total_shred

    heat_wave = ((ruleset.ignite or {}).get("heat_wave") or {})
    assumptions.extend(
        (
            GLYPH_ASSUMPTION_STACKS,
            GLYPH_ASSUMPTION_BOSS,
            GLYPH_ASSUMPTION_MANUAL_STATE,
        )
    )
    if config["boss_effectiveness_source"].startswith("constant:"):
        warnings.append(
            "Apex 注册表缺少 Fire Resistance Shred 的 boss effectiveness 数值，"
            f"使用代码常量 {GLYPH_BOSS_EFFECTIVENESS}（Boss/玩家 60% less effective）。"
        )
    glyph_model: dict[str, Any] = {
        "schema": GLYPH_MODEL_SCHEMA,
        "enabled": True,
        "reason": "glyph_conversion",
        "effect_id": APEX_EFFECT_ID,
        "modifier_id": APEX_MODIFIER_ID,
        "state_id": GLYPH_STATE_ID,
        "activation": "player_standing_on_glyph_of_dominion",
        "boss_target": boss,
        "rare_target": rare,
        "boss_or_rare": boss or rare,
        "boss_effectiveness": boss_effectiveness,
        "boss_effectiveness_source": config["boss_effectiveness_source"],
        "per_stack": per_stack,
        "per_stack_source": config["per_stack_source"],
        "max_stacks": max_stacks,
        "max_stacks_source": config["max_stacks_source"],
        "duration_s": float(config["duration_s"]),
        "duration_source": config["duration_source"],
        "ailment_duration_increase": apex_duration_increase,
        "ailment_duration_contribution_ids": duration_rows,
        "effective_duration_s": effective_duration_s,
        "stack_coverage": "max_stacks_maintained",
        "total_shred": total_shred,
        "target_fire_resistance_before": fire_before,
        "target_fire_resistance_after": fire_after,
        "heat_wave_points_before": int(heat_wave.get("points") or 0),
        "heat_wave_points_after": 0,
        "heat_wave_disabled": True,
        "ignite_application": "converted_to_fire_resistance_shred",
        "assumptions": [
            GLYPH_ASSUMPTION_STACKS,
            GLYPH_ASSUMPTION_HEAT_WAVE,
            GLYPH_ASSUMPTION_BOSS,
            GLYPH_ASSUMPTION_MANUAL_STATE,
        ],
    }
    return True, glyph_model


def _glyph_applied_entry(
    glyph_model: dict[str, Any], proposal_id: str
) -> dict[str, Any]:
    assumption_ids = [
        "assume:glyph-of-dominion-shred-coverage",
        "assume:glyph-of-dominion-boss-effectiveness",
        "assume:glyph-of-dominion-manual-state",
    ]
    return {
        "effect_id": APEX_EFFECT_ID,
        "modifier_id": APEX_MODIFIER_ID,
        "proposal_id": proposal_id,
        "stat_key": "resistance_shred",
        "engine_stat_key": "resistance_shred",
        "operation": "conversion",
        "value": glyph_model["total_shred"],
        "unit": "fraction",
        "target": "fire",
        "damage_tags": ["Fire"],
        "branches": ["ignite.application", "penetration.ignite"],
        "condition": GLYPH_STATE_ID,
        "status": "glyph_conversion",
        "assumption_note": GLYPH_ASSUMPTION_STACKS,
        "assumption_ids": assumption_ids,
        "assumption": GLYPH_ASSUMPTION_STACKS,
        "assumptions": assumption_ids,
        "scope": "both",
        "per_stack": glyph_model["per_stack"],
        "max_stacks": glyph_model["max_stacks"],
        "duration_s": glyph_model["duration_s"],
        "effective_duration_s": glyph_model["effective_duration_s"],
        "boss_effectiveness": glyph_model["boss_effectiveness"],
        "total_shred": glyph_model["total_shred"],
    }


# ---------------------------------------------------------------------------
# Ignite-inheritance assumption metadata (R4)
# ---------------------------------------------------------------------------

_IGNITE_WORDS = ("ignite", "点燃")
_UNCERTAINTY_WORDS = ("unconfirmed", "assumed", "unresolved", "未确认", "尚未确认", "待确认")


def _ignite_inheritance_assumption(
    effect: Optional[dict[str, Any]],
    modifier: Optional[dict[str, Any]],
    proposal: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Return assumption metadata when an ignite branch is consumed unconfirmed.

    The independent review (R4) requires that modifiers whose ignite-branch
    inheritance is still an open question (Chilled Touch, Throne of Ambition,
    Crescendoing Rage, ...) carry an explicit assumption in the applied result,
    while the experiment stays computable.  The registry itself is the source
    of truth: an assumption is emitted when the effect has research open
    questions about ignite (or the modifier note flags ignite as unconfirmed)
    and the proposal actually contains an ``ignite`` branch.
    """
    branches = [str(branch) for branch in (proposal.get("branches") or [])]
    if "ignite" not in branches:
        return None
    effect = effect or {}
    modifier = modifier or {}
    signals: list[dict[str, Any]] = []
    research = effect.get("research") if isinstance(effect.get("research"), dict) else {}
    for index, question in enumerate(research.get("open_questions") or []):
        if not isinstance(question, dict):
            continue
        text = " ".join(
            str(question.get(key) or "") for key in ("question", "probe")
        ).lower()
        if any(word in text for word in _IGNITE_WORDS):
            signals.append(
                {
                    "kind": "registry_research",
                    "path": f"research.open_questions[{index}]",
                    "status": str(question.get("status") or ""),
                    "question": str(question.get("question") or ""),
                }
            )
    note = " ".join(
        (
            str(modifier.get("note") or ""),
            str(proposal.get("engine_sink") or ""),
        )
    ).lower()
    if any(word in note for word in _IGNITE_WORDS) and any(
        word in note for word in _UNCERTAINTY_WORDS
    ):
        signals.append(
            {
                "kind": "modifier_note",
                "path": "modifier.note",
                "status": "unconfirmed",
                "question": str(modifier.get("note") or "")[:300],
            }
        )
    if not signals:
        return None
    modifier_id = str(proposal.get("modifier_id") or "")
    effect_id = str(proposal.get("effect_id") or "")
    assumption_id = f"assume:ignite-inheritance:{modifier_id}"
    return {
        "assumption_id": assumption_id,
        "effect_id": effect_id,
        "modifier_id": modifier_id,
        "branch": "ignite",
        "text": (
            "点燃继承未确认：注册表 research open questions 仍将本 modifier 的点燃分支列为未确认；"
            "当前实验算法按点燃分支计入（可在后续证据确认后转为正式规则）。"
        ),
        "evidence_keys": [signal["path"] for signal in signals],
        "signals": signals,
    }


# ---------------------------------------------------------------------------
# Proposal -> Contribution mapping (contract section 3 / 4)
# ---------------------------------------------------------------------------


def _skip(
    skipped: list[dict[str, Any]],
    *,
    effect_id: str,
    modifier_id: str,
    proposal_id: str,
    reason: str,
    detail: str,
    scope: str = "both",
    effect_status: str = "",
) -> None:
    skipped.append(
        {
            "effect_id": effect_id,
            "modifier_id": modifier_id,
            "proposal_id": proposal_id,
            "reason": reason,
            "detail": detail,
            "scope": scope,
            "effect_status": effect_status,
        }
    )


def _build_proposal_specs(
    proposals: list[dict[str, Any]],
    *,
    snapshot: model.Snapshot,
    registry: Any,
    normalized_scenario: dict[str, Any],
    include_auto: bool,
    warnings: list[str],
    assumptions: list[str],
    skipped: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate/map evaluation proposals into engine contribution specs.

    A spec carries the engine stat key/operation/value/tags plus a ``scopes``
    set (single / loops).  Rejected proposals land in ``skipped`` with a stable
    reason code (never silently dropped).
    """
    baseline_active_ids = {
        contribution.contribution_id
        for contribution in snapshot.contributions
        if contribution.active
    }
    specs: list[dict[str, Any]] = []
    sorted_proposals = sorted(
        (p for p in proposals if isinstance(p, dict)),
        key=lambda p: str(p.get("proposal_id") or p.get("modifier_id") or ""),
    )
    for proposal in sorted_proposals:
        effect_id = str(proposal.get("effect_id") or "")
        modifier_id = str(proposal.get("modifier_id") or "")
        proposal_id = str(proposal.get("proposal_id") or f"prop:{modifier_id}")
        effect_status = str(proposal.get("effect_status") or proposal.get("status") or "")
        requires = str(proposal.get("requires") or "")
        effect = _registry_effect(registry, effect_id)
        common = {
            "effect_id": effect_id,
            "modifier_id": modifier_id,
            "proposal_id": proposal_id,
            "effect_status": effect_status,
        }
        if not modifier_id:
            _skip(skipped, reason="invalid_proposal", detail="提案缺少 modifier_id", **common)
            continue
        baseline_relation = str((effect or {}).get("relevance", {}).get("baseline_relation") or "")
        if effect_status == "already_in_baseline" or baseline_relation == "already_in_baseline":
            _skip(
                skipped,
                reason="already_in_baseline",
                detail="该效果已计入基础来源账，拟议贡献不应出现；按契约跳过。",
                **common,
            )
            warnings.append(
                f"{effect_id}/{modifier_id}: 提案标记为 already_in_baseline，已跳过（不应重复计入）。"
            )
            continue
        if requires == "mechanism_unresolved":
            _skip(
                skipped,
                reason="mechanism_unresolved",
                detail="机制未确认（mechanism_unresolved），不写入伤害快照。",
                **common,
            )
            warnings.append(f"{effect_id}/{modifier_id}: mechanism_unresolved，已跳过。")
            continue
        if effect_status == "auto_active" and not include_auto:
            _skip(
                skipped,
                reason="auto_excluded",
                detail="include_auto=False：自动生效（auto_active）提案不计入。",
                **common,
            )
            continue

        raw_stat_key = str(proposal.get("stat_key") or "")
        mapped_stat = STAT_ALIASES.get(raw_stat_key)
        if mapped_stat is None:
            _skip(
                skipped,
                reason="unsupported_stat_key",
                detail=f"stat_key={raw_stat_key!r} 没有可消费的引擎通道（Conflagrate/Apex 类转换/flag）。",
                **common,
            )
            warnings.append(f"{effect_id}/{modifier_id}: 未支持 stat_key={raw_stat_key!r}，已跳过。")
            continue
        operation = str(proposal.get("operation") or "")
        if operation not in SUPPORTED_OPERATIONS:
            _skip(
                skipped,
                reason="unsupported_operation",
                detail=f"operation={operation!r} 不被伤害引擎消费。",
                **common,
            )
            warnings.append(f"{effect_id}/{modifier_id}: 未支持 operation={operation!r}，已跳过。")
            continue
        value = _as_float(proposal.get("value"))
        if value is None:
            _skip(
                skipped,
                reason="value_unresolved",
                detail="提案数值未解析（per_point/per_stack 缺少点数/层数来源）；需提供 catalog 后重算。",
                **common,
            )
            warnings.append(
                f"{effect_id}/{modifier_id}: 提案 value 未解析（需要 catalog 点数/层数），已跳过。"
            )
            continue

        target = proposal.get("target")
        raw_tags = proposal.get("damage_tags") or []
        branches = proposal.get("branches") or []
        assumption_note = ""
        if mapped_stat == model.STAT_AILMENT_DURATION:
            target_key = str(target or "").strip().lower()
            if target_key not in ("ignite", "all", ""):
                _skip(
                    skipped,
                    reason="ailment_duration_target_invalid",
                    detail=f"ailment_duration 的 target 必须是 ignite（当前 {target!r}）。",
                    **common,
                )
                warnings.append(
                    f"{effect_id}/{modifier_id}: ailment_duration target={target!r}，已跳过。"
                )
                continue
            tags = [str(tag) for tag in raw_tags if tag]
        elif mapped_stat in TAG_SCOPED_STATS:
            tags, tag_reason = _resolve_damage_tags(raw_tags, branches)
            if not tags and tag_reason:
                _skip(
                    skipped,
                    reason="branches_not_expressible",
                    detail=f"branches={branches!r}：{tag_reason}",
                    **common,
                )
                warnings.append(
                    f"{effect_id}/{modifier_id}: 分支无法用 damage_tags 表达，已跳过。"
                )
                continue
        else:
            # cast_speed / cooldown_recovery_speed / skill_duration: the engine
            # ignores damage_tags for these stats; keep the registry tags.
            tags = [str(tag) for tag in raw_tags if tag]

        # -- dedupe --------------------------------------------------------
        dedupe_refs = [
            str(item)
            for item in (proposal.get("dedupe_contribution_ids") or [])
            if item
        ]
        active_refs = sorted(set(dedupe_refs) & baseline_active_ids)
        if active_refs:
            _skip(
                skipped,
                reason="dedupe_already_active",
                detail=(
                    "dedupe_contribution_ids 指向基线已生效的贡献，避免重复计入："
                    + ", ".join(active_refs)
                ),
                **common,
            )
            warnings.append(
                f"{effect_id}/{modifier_id}: dedupe 引用已在基线生效（{', '.join(active_refs)}），已跳过。"
            )
            continue
        if dedupe_refs:
            assumption_note = (
                "dedupe 引用未在基线生效（"
                + ", ".join(dedupe_refs)
                + "）；按增量值计入，不重复叠加既有来源。"
            )
            assumptions.append(f"{effect_id}/{modifier_id}: {assumption_note}")

        # -- action/time scopes -------------------------------------------
        # A CastState control describes "this cast" (third cast / prepared
        # next spell); it can only enter the single-cast口径.  A loop would need
        # per-cast phase simulation, so it is reported skipped for loops.
        control: dict[str, Any] = {}
        try:
            control = registry.control_binding(effect or {})
        except Exception:  # pragma: no cover - defensive
            control = {}
        control_kind = str(control.get("kind") or "")
        scopes: set[str]
        if control_kind == "cast_state":
            cast_key = str(control.get("cast_state_id") or "")
            single_cast = normalized_scenario.get("single_cast")
            single_cast = single_cast if isinstance(single_cast, dict) else {}
            if not cast_key or not bool(single_cast.get(cast_key)):
                _skip(
                    skipped,
                    reason="cast_state_not_selected",
                    detail=f"单次施法状态 {cast_key!r} 未勾选；CastState 提案不生效。",
                    scope="single_cast",
                    **common,
                )
                continue
            scopes = {"single_cast"}
            loop_reason = (
                "needs_action_or_time_simulation"
                if requires == "needs_action_or_time_simulation"
                else "cast_state_single_cast_only"
            )
            _skip(
                skipped,
                reason=loop_reason,
                detail=(
                    "循环口径需要逐施法相位模拟（第 N 次施放 / 准备层数），"
                    "CastState 提案只计入单次施法。"
                ),
                scope="loops",
                **common,
            )
        elif requires == "needs_action_or_time_simulation":
            _skip(
                skipped,
                reason="needs_action_or_time_simulation",
                detail="需要动作/时间模拟的机制，固定条件重算不执行动作序列。",
                **common,
            )
            warnings.append(
                f"{effect_id}/{modifier_id}: 需要动作/时间模拟（非 CastState），已跳过。"
            )
            continue
        else:
            scopes = {"single_cast", "loops"}

        modifier = next(
            (
                candidate
                for candidate in (effect or {}).get("modifiers") or []
                if isinstance(candidate, dict)
                and str(candidate.get("modifier_id") or "") == modifier_id
            ),
            None,
        )
        inheritance_assumption = _ignite_inheritance_assumption(
            effect, modifier, proposal
        )
        spec_assumptions = (
            [inheritance_assumption] if inheritance_assumption else []
        )
        specs.append(
            {
                "effect_id": effect_id,
                "modifier_id": modifier_id,
                "proposal_id": proposal_id,
                "effect_status": effect_status,
                "requires": requires,
                "stat_key": mapped_stat,
                "registry_stat_key": raw_stat_key,
                "operation": operation,
                "value": value,
                "unit": str(proposal.get("unit") or "flat"),
                "target": target,
                "tags": tags,
                "branches": [str(branch) for branch in branches],
                "condition": proposal.get("condition"),
                "scopes": scopes,
                "assumption_note": assumption_note,
                "assumptions": spec_assumptions,
                "stat_name": str(proposal.get("stat_name") or raw_stat_key or modifier_id),
            }
        )
    return specs


def _contribution_from_spec(
    spec: dict[str, Any], registry: Any
) -> model.Contribution:
    effect_id = spec["effect_id"]
    modifier_id = spec["modifier_id"]
    contribution_id = f"conditional:{effect_id}:{modifier_id}"
    operation = spec["operation"]
    registry_identity = _registry_identity(registry)
    locator = None
    if registry_identity.get("id"):
        locator = f"registry:{registry_identity['id']}#modifiers/{modifier_id}"
    evidence = model.Evidence(
        kind="conditional_effect",
        detail=(
            f"{effect_id} / {modifier_id} · condition={spec.get('condition') or '—'}"
            f" · support={spec.get('requires') or '—'}"
        ),
        locator=locator,
    )
    return model.Contribution(
        contribution_id=contribution_id,
        source_kind=SOURCE_KIND_CONDITIONAL,
        source_label=f"{effect_id}: {spec['stat_name']}",
        source_path=f"conditional.{effect_id}",
        stat_name=spec["stat_name"],
        stat_key=spec["stat_key"],
        operation=operation,
        value=spec["value"],
        unit=spec["unit"],
        damage_tags=list(spec["tags"]),
        target=spec["target"],
        condition_code=str(spec.get("condition") or "") or None,
        condition_status=model.CONDITION_SATISFIED,
        group=(f"conditional:{modifier_id}" if operation == "more" else None),
        included=True,
        value_source="conditional_registry",
        status=model.STATUS_ASSUMED,
        evidence=[evidence],
        notes=spec.get("assumption_note") or None,
        raw={
            "proposal_id": spec["proposal_id"],
            "effect_status": spec["effect_status"],
            "requires": spec["requires"],
            "branches": list(spec["branches"]),
        },
    )


def _snapshot_with_specs(
    snapshot: model.Snapshot, specs: list[dict[str, Any]], mode: str, registry: Any
) -> model.Snapshot:
    patched = copy.deepcopy(snapshot)
    selected = sorted(
        (spec for spec in specs if mode in spec["scopes"]),
        key=lambda spec: f"conditional:{spec['effect_id']}:{spec['modifier_id']}",
    )
    for spec in selected:
        patched.contributions.append(_contribution_from_spec(spec, registry))
    return patched


def _applied_entries(
    specs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    for spec in sorted(
        specs, key=lambda item: f"conditional:{item['effect_id']}:{item['modifier_id']}"
    ):
        scope = (
            "both"
            if spec["scopes"] >= {"single_cast", "loops"}
            else sorted(spec["scopes"])[0]
        )
        assumptions = [
            assumption
            for assumption in spec.get("assumptions") or []
            if isinstance(assumption, dict)
        ]
        assumption_ids = [
            str(assumption.get("assumption_id") or "") for assumption in assumptions
        ]
        assumption_texts = [
            str(assumption.get("text") or "") for assumption in assumptions
        ]
        assumption_note = str(spec.get("assumption_note") or "")
        if assumption_texts:
            assumption_note = "；".join(
                [part for part in (assumption_note, *assumption_texts) if part]
            )
        applied.append(
            {
                "effect_id": spec["effect_id"],
                "modifier_id": spec["modifier_id"],
                "proposal_id": spec["proposal_id"],
                "stat_key": spec["registry_stat_key"],
                "engine_stat_key": spec["stat_key"],
                "operation": spec["operation"],
                "value": spec["value"],
                "unit": spec["unit"],
                "target": spec["target"],
                "damage_tags": list(spec["tags"]),
                "branches": list(spec["branches"]),
                "condition": spec.get("condition"),
                "status": spec["effect_status"],
                "assumption_note": assumption_note,
                "assumption_ids": assumption_ids,
                "assumption": assumption_texts[0] if assumption_texts else "",
                "assumptions": [str(assumption.get("assumption_id") or "") for assumption in assumptions],
                "scope": scope,
            }
        )
    return applied


def _evaluation_skipped_entries(
    evaluation: dict[str, Any],
    *,
    already_skipped: list[dict[str, Any]],
    applied: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Record evaluation-level unsupported effects that never became proposals.

    Action effects (Conflagrate) are reported as ``unsupported_action`` for
    every scenario and cannot produce proposals at all; the contract still
    wants them visible as skipped.  Unresolved mechanisms (Frozen Steel) only
    surface here when their conditions are met, and then they do produce a
    proposal which is skipped through the normal path -- so no duplicate entry
    is added for effects that already have a skip/applied record.
    """
    rows: list[dict[str, Any]] = []
    seen_effects = {str(entry.get("effect_id") or "") for entry in already_skipped}
    seen_effects |= {str(entry.get("effect_id") or "") for entry in applied}
    records = evaluation.get("effects")
    records = records if isinstance(records, list) else []
    for record in sorted(
        (item for item in records if isinstance(item, dict)),
        key=lambda item: str(item.get("effect_id") or ""),
    ):
        effect_id = str(record.get("effect_id") or "")
        if not effect_id or effect_id in seen_effects:
            continue
        if str(record.get("status") or "") != "unsupported_action":
            continue
        seen_effects.add(effect_id)
        rows.append(
            {
                "effect_id": effect_id,
                "modifier_id": "",
                "proposal_id": "",
                "reason": "needs_action_or_time_simulation",
                "detail": str(record.get("status_reason") or ""),
                "scope": "both",
                "effect_status": "unsupported_action",
            }
        )
    # ``unsupported`` is a second, filtered view; only add entries whose
    # effect is still unrepresented.
    unsupported = evaluation.get("unsupported")
    unsupported = unsupported if isinstance(unsupported, list) else []
    for entry in sorted(
        (item for item in unsupported if isinstance(item, dict)),
        key=lambda item: str(item.get("effect_id") or ""),
    ):
        effect_id = str(entry.get("effect_id") or "")
        if not effect_id or effect_id in seen_effects:
            continue
        seen_effects.add(effect_id)
        support = str(entry.get("support") or "")
        rows.append(
            {
                "effect_id": effect_id,
                "modifier_id": "",
                "proposal_id": "",
                "reason": (
                    "mechanism_unresolved"
                    if support == "mechanism_unresolved"
                    else "needs_action_or_time_simulation"
                ),
                "detail": str(entry.get("reason") or ""),
                "scope": "both",
                "effect_status": str(entry.get("status") or ""),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Channel support matrix (selection-independent, for the conditions page)
# ---------------------------------------------------------------------------

_SUPPORT_REASON_DETAILS = {
    "consumable": "该效果可由条件重算通道消费（选中/自动生效后生成条件贡献）。",
    "already_in_baseline": "已计入基线来源账（目标条件/快照已消费），条件重算不重复注入。",
    "mechanism_unresolved": "机制未确认，条件重算跳过。",
    "needs_action_or_time_simulation": "动作机制未支持，固定条件重算跳过。",
    "no_damage_modifiers": "没有可映射到伤害引擎的 modifier（仅表现/flag）。",
    "unsupported_stat_key": "没有可消费的引擎 stat_key 通道。",
    "unsupported_operation": "operation 不被伤害引擎消费。",
    "ailment_duration_target_invalid": "ailment_duration 的 target 不是 ignite。",
    "branches_not_expressible": "modifier 分支无法用 damage_tags 表达。",
    "glyph_conversion": (
        "站在 Glyph of Dominion 上时，Heat Wave 点燃几率转为 Fire Resistance Shred，"
        "条件重算以目标火抗降低（固定条件 shred 模型）消费。"
    ),
}


def _modifier_support_entry(modifier: dict[str, Any]) -> dict[str, Any]:
    stat_key_raw = str(modifier.get("stat_key") or "")
    operation = str(modifier.get("operation") or "")
    branches = [str(branch) for branch in (modifier.get("branches") or [])]
    raw_tags = [str(tag) for tag in (modifier.get("damage_tags") or []) if tag]
    target = modifier.get("target")
    entry: dict[str, Any] = {
        "modifier_id": str(modifier.get("modifier_id") or ""),
        "stat_key": stat_key_raw,
        "operation": operation,
        "engine_stat_key": None,
        "target": target,
        "damage_tags": list(raw_tags),
        "branches": branches,
        "supported": False,
        "skip_reason": "",
        "dedupe_contribution_ids": [
            str(item) for item in (modifier.get("dedupe_contribution_ids") or []) if item
        ],
    }
    engine_stat = STAT_ALIASES.get(stat_key_raw)
    if engine_stat is None:
        entry["skip_reason"] = "unsupported_stat_key"
        return entry
    if operation not in SUPPORTED_OPERATIONS:
        entry["skip_reason"] = "unsupported_operation"
        return entry
    if engine_stat == model.STAT_AILMENT_DURATION:
        if str(target or "").strip().lower() not in ("ignite", "all", ""):
            entry["skip_reason"] = "ailment_duration_target_invalid"
            return entry
        tags = list(raw_tags)
    elif engine_stat in TAG_SCOPED_STATS:
        tags, reason = _resolve_damage_tags(raw_tags, branches)
        if reason:
            entry["skip_reason"] = "branches_not_expressible"
            return entry
    else:
        tags = list(raw_tags)
    entry.update(
        {
            "engine_stat_key": engine_stat,
            "damage_tags": tags,
            "supported": True,
        }
    )
    return entry


def channel_support(registry: Any) -> dict[str, Any]:
    """Selection-independent support matrix for every registered effect.

    Used by the conditions page to show, per effect card, whether the
    cross-skill Buff input channel can consume the effect:

    * ``state="supported"`` -- a selected / auto-active proposal becomes a
      ``model.Contribution`` (``scope`` says ``both`` or ``single_cast``);
    * ``state="skipped"`` -- never consumed (``reason`` gives the stable code);
    * ``state="baseline"`` -- already counted by the legacy baseline, never
      re-injected.

    The mapping is keyed by ``effect_id`` and covers every registry effect in
    ``registry.effects`` (sorted for determinism), independent of the current
    scenario selection.  It never raises; a malformed registry yields ``{}``.
    """
    out: dict[str, Any] = {}
    try:
        effects = list(registry.effects)
    except Exception:  # pragma: no cover - defensive; not a registry-like object
        return out
    rows = sorted(
        (
            effect
            for effect in effects
            if isinstance(effect, dict) and effect.get("effect_id")
        ),
        key=lambda effect: str(effect.get("effect_id")),
    )
    for effect in rows:
        effect_id = str(effect.get("effect_id"))
        relevance = effect.get("relevance") or {}
        support = effect.get("support") or {}
        activation = effect.get("activation") or {}
        support_level = str(support.get("level") or "mechanism_unresolved")
        baseline_relation = str(relevance.get("baseline_relation") or "new")
        activation_kind = str(activation.get("kind") or "")
        control: dict[str, Any] = {}
        try:
            control = registry.control_binding(effect)
        except Exception:  # pragma: no cover - defensive
            control = {}
        control_kind = str(control.get("kind") or "none")
        scope = "single_cast" if control_kind == "cast_state" else "both"
        glyph_channel = effect_id == APEX_EFFECT_ID
        modifiers = [
            _modifier_support_entry(modifier)
            for modifier in effect.get("modifiers") or []
            if isinstance(modifier, dict)
        ]
        if glyph_channel:
            # The Apex conversion has no generic stat_key channel; it is
            # consumable through the dedicated Glyph shred model.
            for modifier_entry in modifiers:
                if (
                    modifier_entry["modifier_id"] == APEX_MODIFIER_ID
                    or modifier_entry["stat_key"] == "ailment_chance_conversion"
                ):
                    modifier_entry.update(
                        {
                            "engine_stat_key": "resistance_shred",
                            "supported": True,
                            "skip_reason": "",
                            "scope": "both",
                            "glyph_model": "fire_resistance_shred",
                        }
                    )
        for modifier_entry in modifiers:
            if modifier_entry["supported"]:
                modifier_entry["scope"] = "both" if glyph_channel else scope
            else:
                modifier_entry["scope"] = None
        supported_modifiers = [entry for entry in modifiers if entry["supported"]]
        if glyph_channel:
            state, reason, scope = "supported", "glyph_conversion", "both"
        elif baseline_relation == "already_in_baseline":
            state, reason = "baseline", "already_in_baseline"
            scope = None
        elif support_level == "mechanism_unresolved":
            state, reason = "skipped", "mechanism_unresolved"
        elif activation_kind == "action" or (
            support_level == "needs_action_or_time_simulation"
            and control_kind != "cast_state"
        ):
            state, reason = "skipped", "needs_action_or_time_simulation"
        elif not modifiers:
            state, reason = "skipped", "no_damage_modifiers"
        elif supported_modifiers:
            state, reason = "supported", "consumable"
        else:
            state = "skipped"
            reason = next(
                (
                    entry["skip_reason"]
                    for entry in modifiers
                    if entry.get("skip_reason")
                ),
                "unsupported_stat_key",
            )
        out[effect_id] = {
            "effect_id": effect_id,
            "state": state,
            "reason": reason,
            "reason_detail": _SUPPORT_REASON_DETAILS.get(reason, ""),
            "support_level": support_level,
            "baseline_relation": baseline_relation,
            "control_kind": control_kind,
            "auto": control_kind == "none",
            "scope": scope,
            "modifiers": modifiers,
        }
    return out


# ---------------------------------------------------------------------------
# Engine summaries / delta
# ---------------------------------------------------------------------------


def _single_summary(single: dict[str, Any]) -> dict[str, Any]:
    totals = single.get("totals") or {}
    return {
        "single_cast_total": float(totals.get("single_cast_total", 0.0)),
        "body_total": float(totals.get("body_total", 0.0)),
        "ignite_total": float(totals.get("ignite_total", 0.0)),
        "ignite_tail_damage": float(totals.get("ignite_tail_damage", 0.0)),
        "expected_total_stacks_applied": float(
            totals.get("expected_total_stacks_applied", 0.0)
        ),
        "observation_end_s": float(totals.get("observation_end_s", 0.0)),
        "cast_time_s": float((single.get("cast") or {}).get("cast_time_s", 0.0)),
    }


def _loop_summary(loop: dict[str, Any]) -> dict[str, Any]:
    totals = loop.get("totals") or {}
    windows = [
        {
            "window_s": float(window.get("window_s", 0.0)),
            "dps": float(window.get("dps", 0.0)),
            "total_damage": float(window.get("total_damage", 0.0)),
        }
        for window in loop.get("windows") or []
    ]
    return {
        "policy_id": str(loop.get("policy_id") or ""),
        "windows": windows,
        "cast_count": int(totals.get("cast_count", 0) or 0),
        "cycle_s": totals.get("cycle_s"),
    }


def _select_window(loop_summary: dict[str, Any]) -> Optional[dict[str, Any]]:
    windows = loop_summary.get("windows") or []
    for window in windows:
        if abs(float(window.get("window_s", 0.0)) - 120.0) < 1e-9:
            return window
    return windows[-1] if windows else None


def _delta_block(
    baseline: dict[str, Any], scenario: dict[str, Any]
) -> dict[str, Any]:
    base_single = baseline.get("single") or {}
    scen_single = scenario.get("single") or {}
    single_delta, single_pct = _percent_change_delta(
        float(base_single.get("single_cast_total", 0.0)),
        float(scen_single.get("single_cast_total", 0.0)),
    )
    body_delta, body_pct = _percent_change_delta(
        float(base_single.get("body_total", 0.0)),
        float(scen_single.get("body_total", 0.0)),
    )
    ignite_delta, ignite_pct = _percent_change_delta(
        float(base_single.get("ignite_total", 0.0)),
        float(scen_single.get("ignite_total", 0.0)),
    )
    loop_rows: list[dict[str, Any]] = []
    scenario_loops = {
        str(loop.get("policy_id") or ""): loop
        for loop in scenario.get("loops") or []
        if isinstance(loop, dict)
    }
    for base_loop in baseline.get("loops") or []:
        policy_id = str(base_loop.get("policy_id") or "")
        scen_loop = scenario_loops.get(policy_id)
        if scen_loop is None:
            continue
        base_window = _select_window(base_loop)
        scen_window = _select_window(scen_loop)
        if base_window is None or scen_window is None:
            continue
        base_dps = float(base_window.get("dps", 0.0))
        scen_dps = float(scen_window.get("dps", 0.0))
        dps_delta, dps_pct = _percent_change_delta(base_dps, scen_dps)
        loop_rows.append(
            {
                "policy_id": policy_id,
                "window_s": float(base_window.get("window_s", 0.0)),
                "dps": dps_delta,
                "dps_pct": dps_pct,
                "baseline_dps": base_dps,
                "scenario_dps": scen_dps,
            }
        )
    return {
        "single_cast_total": single_delta,
        "single_cast_total_pct": single_pct,
        "body_total": body_delta,
        "body_total_pct": body_pct,
        "ignite_total": ignite_delta,
        "ignite_total_pct": ignite_pct,
        "loops": loop_rows,
    }


def _empty_payload(
    *,
    status: str,
    ruleset: Optional[Ruleset],
    monster_level: Optional[int],
    warnings: list[str],
    assumptions: list[str],
    scenario: dict[str, Any],
    fingerprint: str,
    dps_recomputed: bool,
) -> dict[str, Any]:
    ruleset_block = {"id": None, "sha256": None}
    if ruleset is not None:
        try:
            ruleset_block = {"id": ruleset.ruleset_id, "sha256": ruleset.sha256}
        except Exception:  # malformed Ruleset object: report the degradation only
            ruleset_block = {"id": None, "sha256": None}
    return {
        "schema": SCHEMA,
        "status": status,
        "ruleset": ruleset_block,
        "monster_level": monster_level,
        "baseline": {},
        "scenario": {},
        "delta": {},
        "condition_application": {
            "applied": [],
            "skipped": [],
            "target_conditions": {},
            "proposals_used": [],
            "auto_included": False,
        },
        "warnings": list(warnings),
        "assumptions": list(assumptions),
        "scenario_fingerprint": fingerprint,
        "channel_support": {},
        "glyph_model": {},
        "dps_recomputed": dps_recomputed,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _ruleset_info(ruleset: Optional[Ruleset]) -> dict[str, Any]:
    if ruleset is None:
        return {"id": None, "sha256": None}
    try:
        return {"id": ruleset.ruleset_id, "sha256": ruleset.sha256}
    except Exception:  # malformed Ruleset object
        return {"id": None, "sha256": None}


# ---------------------------------------------------------------------------
# Unified target / baseline profile / application-state helpers (R1/R2/R5/R8)
# ---------------------------------------------------------------------------


def _default_target_from_ruleset(ruleset: Ruleset) -> dict[str, Any]:
    target_side = ruleset.target_side
    monster_dr = (target_side.get("monster_dr") or {}) if isinstance(target_side, dict) else {}
    resistances = (
        (target_side.get("base_resistances") or {}) if isinstance(target_side, dict) else {}
    )
    return {
        "monster_level": int(monster_dr.get("default_level", 0) or 0),
        "fire_resistance": float(resistances.get("fire", 0.0) or 0.0),
        "boss": bool(target_side.get("boss_tag", True)) if isinstance(target_side, dict) else True,
        "rare": bool(target_side.get("rare_tag", True)) if isinstance(target_side, dict) else True,
    }


def _sanitize_target(
    target: dict[str, Any],
    fallback: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    """Clamp/repair one target block with warnings (defensive path)."""
    from .scenario import TARGET_FIRE_RESISTANCE_RANGE, TARGET_MONSTER_LEVEL_RANGE

    out = dict(fallback)
    level = target.get("monster_level")
    if isinstance(level, int) and not isinstance(level, bool):
        low, high = TARGET_MONSTER_LEVEL_RANGE
        if low <= level <= high:
            out["monster_level"] = int(level)
        else:
            warnings.append(
                f"target.monster_level={level} 越界（{low}..{high}），回退到 {fallback['monster_level']}。"
            )
    fire = target.get("fire_resistance")
    if isinstance(fire, (int, float)) and not isinstance(fire, bool):
        low, high = TARGET_FIRE_RESISTANCE_RANGE
        if low <= float(fire) <= high:
            out["fire_resistance"] = float(fire)
        else:
            warnings.append(
                f"target.fire_resistance={fire} 越界（{low}..{high}），回退到 {fallback['fire_resistance']}。"
            )
    for flag in ("boss", "rare"):
        if isinstance(target.get(flag), bool):
            out[flag] = target[flag]
    return out


def _resolve_target_block(
    *,
    ruleset: Ruleset,
    normalized_scenario: dict[str, Any],
    raw_scenario: dict[str, Any],
    monster_level: Optional[int],
    catalog: Any,
    warnings: list[str],
) -> dict[str, Any]:
    """Resolve the unified scenario target (R1/R8).

    Precedence: ruleset target-side defaults -> explicit ``monster_level``
    argument -> catalog/DPS-context target defaults -> scenario target block
    -> legacy ``states.target.boss_or_rare`` bridge (only when the scenario has
    no target block).  The scenario target block wins so a page edit of
    level/fire resistance/boss/rare is honoured (R1).
    """
    defaults = _default_target_from_ruleset(ruleset)
    candidate = dict(defaults)
    if monster_level is not None:
        if isinstance(monster_level, bool) or not isinstance(monster_level, int):
            warnings.append(f"monster_level={monster_level!r} 不是整数；忽略。")
        else:
            candidate["monster_level"] = int(monster_level)
    catalog_target = catalog.get("target_context") if isinstance(catalog, dict) else None
    if isinstance(catalog_target, dict):
        candidate.update(
            {
                key: value
                for key, value in catalog_target.items()
                if key in candidate
                and (
                    (key in ("boss", "rare") and isinstance(value, bool))
                    or (
                        key == "monster_level"
                        and isinstance(value, int)
                        and not isinstance(value, bool)
                    )
                    or (
                        key == "fire_resistance"
                        and isinstance(value, (int, float))
                        and not isinstance(value, bool)
                    )
                )
            }
        )
    block: Optional[dict[str, Any]] = None
    if isinstance(normalized_scenario.get("target"), dict):
        block = dict(normalized_scenario["target"])
    elif isinstance(raw_scenario.get("target"), dict):
        block = dict(raw_scenario["target"])
    if block is not None:
        candidate.update(
            {
                key: value
                for key, value in block.items()
                if key in candidate
                and (
                    (key in ("boss", "rare") and isinstance(value, bool))
                    or (
                        key == "monster_level"
                        and isinstance(value, int)
                        and not isinstance(value, bool)
                    )
                    or (
                        key == "fire_resistance"
                        and isinstance(value, (int, float))
                        and not isinstance(value, bool)
                    )
                )
            }
        )
    else:
        states = normalized_scenario.get("states")
        if not isinstance(states, dict):
            states = raw_scenario.get("states") if isinstance(raw_scenario.get("states"), dict) else {}
        legacy = states.get("target.boss_or_rare")
        if isinstance(legacy, bool):
            candidate["boss"] = legacy
            candidate["rare"] = legacy
    return _sanitize_target(candidate, defaults, warnings)


def _scenario_target_payload(
    target: dict[str, Any], target_conditions: dict[str, Any]
) -> dict[str, Any]:
    """The effective target the recompute consumed (page/scenario drawer)."""
    return {
        "monster_level": int(target.get("monster_level", 0)),
        "fire_resistance": float(target.get("fire_resistance", 0.0)),
        "boss": bool(target.get("boss", True)),
        "rare": bool(target.get("rare", True)),
        "boss_or_rare": bool(target.get("boss", True) or target.get("rare", True)),
        "within_center_1m": bool(target_conditions.get("target_within_center_1m", True)),
        "both_stars_cover": bool(target_conditions.get("both_stars_cover", True)),
    }


def _resolve_include_auto(
    scenario: dict[str, Any],
    registry: Any,
    include_auto: Optional[bool],
    warnings: list[str],
) -> tuple[bool, str]:
    """Baseline-profile policy (R2).

    * explicit ``include_auto`` argument -> wins;
    * ``baseline_profile == "legacy_single_skill"`` -> auto proposals excluded;
    * ``build_base`` (default) -> auto proposals included.
    """
    if include_auto is not None:
        return bool(include_auto), "explicit"
    profile = str(scenario.get("baseline_profile") or "build_base")
    if profile == "legacy_single_skill":
        return False, "baseline_profile:legacy_single_skill"
    if profile != "build_base":
        valid = set()
        try:
            valid = {str(item.get("id")) for item in registry.baseline_profiles}
        except Exception:  # pragma: no cover - defensive
            valid = set()
        if valid and profile not in valid:
            warnings.append(f"未知 baseline_profile={profile!r}；按 build_base 处理。")
        profile = "build_base"
    return True, f"baseline_profile:{profile}"


def _assumptions_applied_rows(
    specs: list[dict[str, Any]], glyph_model: dict[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in specs:
        scope = (
            "both"
            if spec["scopes"] >= {"single_cast", "loops"}
            else sorted(spec["scopes"])[0]
        )
        for assumption in spec.get("assumptions") or []:
            if not isinstance(assumption, dict):
                continue
            rows.append(
                {
                    "assumption_id": str(assumption.get("assumption_id") or ""),
                    "effect_id": str(assumption.get("effect_id") or spec["effect_id"]),
                    "modifier_id": str(assumption.get("modifier_id") or spec["modifier_id"]),
                    "branch": str(assumption.get("branch") or "ignite"),
                    "scope": scope,
                    "text": str(assumption.get("text") or ""),
                    "evidence_keys": list(assumption.get("evidence_keys") or []),
                    "signals": list(assumption.get("signals") or []),
                }
            )
    if isinstance(glyph_model, dict) and glyph_model.get("enabled"):
        glyph_rows = (
            (
                "assume:glyph-of-dominion-shred-coverage",
                GLYPH_ASSUMPTION_STACKS,
                "ignite.application",
            ),
            (
                "assume:glyph-of-dominion-boss-effectiveness",
                GLYPH_ASSUMPTION_BOSS,
                "target.fire_resistance",
            ),
            (
                "assume:glyph-of-dominion-manual-state",
                GLYPH_ASSUMPTION_MANUAL_STATE,
                "player.standing_on_glyph_of_dominion",
            ),
        )
        for assumption_id, text, branch in glyph_rows:
            rows.append(
                {
                    "assumption_id": assumption_id,
                    "effect_id": APEX_EFFECT_ID,
                    "modifier_id": APEX_MODIFIER_ID,
                    "branch": branch,
                    "scope": "both",
                    "text": text,
                    "evidence_keys": ["glyph_model"],
                    "signals": [],
                }
            )
    rows.sort(key=lambda row: (row["effect_id"], row["modifier_id"], row["assumption_id"]))
    return rows


_APPLICATION_STATE_BY_SCOPE = {
    "both": "applied_both",
    "single_cast": "applied_single",
    "loops": "applied_loop",
}


def _application_states(
    *,
    registry: Any,
    applied: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    assumptions_applied: list[dict[str, Any]],
    support: dict[str, Any],
) -> dict[str, Any]:
    """Per-effect application status for the report UI (R5).

    ``state`` is one of ``applied_both`` / ``applied_single`` / ``applied_loop``
    / ``skipped`` / ``not_selected``.  Entries also carry the fields the report
    page needs to render one row per source: independent ``single``/``loop``
    booleans, skip ``reason``/``reason_detail``, registry ``support_level``,
    ``assumptions`` ids (+``assumption_note``), ``modifier_ids``, ``branches``
    and the raw ``applied_rows`` that fed the numbers.
    """
    effect_ids: set[str] = set()
    try:
        effect_ids.update(
            str(effect.get("effect_id"))
            for effect in registry.effects
            if isinstance(effect, dict) and effect.get("effect_id")
        )
    except Exception:  # pragma: no cover - defensive
        effect_ids = set()
    effect_ids.update(str(entry.get("effect_id") or "") for entry in applied)
    effect_ids.update(str(entry.get("effect_id") or "") for entry in skipped)
    effect_ids.discard("")
    states: dict[str, Any] = {}
    for effect_id in sorted(effect_ids):
        applied_rows = [
            entry for entry in applied if str(entry.get("effect_id")) == effect_id
        ]
        skipped_rows = [
            entry for entry in skipped if str(entry.get("effect_id")) == effect_id
        ]
        single = any(
            str(entry.get("scope")) in ("both", "single_cast") for entry in applied_rows
        )
        loop = any(str(entry.get("scope")) in ("both", "loops") for entry in applied_rows)
        if single and loop:
            state = "applied_both"
        elif single:
            state = "applied_single"
        elif loop:
            state = "applied_loop"
        elif skipped_rows:
            state = "skipped"
        elif str(support.get(effect_id, {}).get("state") or "") == "skipped":
            state = "skipped"
        else:
            state = "not_selected"
        reason = ""
        reason_detail = ""
        if applied_rows:
            loop_skips = [
                row for row in skipped_rows if str(row.get("scope")) == "loops"
            ]
            if loop_skips:
                reason = str(loop_skips[0].get("reason") or "")
                reason_detail = str(loop_skips[0].get("detail") or "")
        elif skipped_rows:
            reason = str(skipped_rows[0].get("reason") or "")
            reason_detail = str(skipped_rows[0].get("detail") or "")
        else:
            support_entry = support.get(effect_id) or {}
            if str(support_entry.get("state")) == "baseline":
                reason = "already_in_baseline"
                reason_detail = str(support_entry.get("reason_detail") or "")
            elif state == "skipped":
                reason = str(support_entry.get("reason") or "")
                reason_detail = str(support_entry.get("reason_detail") or "")
        assumption_ids = [
            str(row.get("assumption_id"))
            for row in assumptions_applied
            if str(row.get("effect_id")) == effect_id
        ]
        assumption_texts = [
            str(row.get("text"))
            for row in assumptions_applied
            if str(row.get("effect_id")) == effect_id
        ]
        modifier_ids = sorted(
            {
                str(entry.get("modifier_id"))
                for entry in applied_rows
                if entry.get("modifier_id")
            }
            | {
                str(entry.get("modifier_id"))
                for entry in skipped_rows
                if entry.get("modifier_id")
            }
        )
        branches: list[str] = []
        for entry in applied_rows:
            for branch in entry.get("branches") or []:
                branch_text = str(branch)
                if branch_text not in branches:
                    branches.append(branch_text)
        support_entry = support.get(effect_id) or {}
        states[effect_id] = {
            "effect_id": effect_id,
            "state": state,
            "single": bool(single),
            "loop": bool(loop),
            "reason": reason,
            "reason_detail": reason_detail,
            "support_level": str(support_entry.get("support_level") or ""),
            "assumptions": assumption_ids,
            "assumption_note": "；".join(part for part in assumption_texts if part),
            "modifier_ids": modifier_ids,
            "branches": branches,
            "applied_rows": [dict(row) for row in applied_rows],
        }
    return states


# ---------------------------------------------------------------------------
# Scenario outputs (single engine run per environment, R7 performance)
# ---------------------------------------------------------------------------


def _compute_outputs_error(
    prepared: dict[str, Any], status: str, warnings: list[str]
) -> dict[str, Any]:
    return {
        "schema": "bh-conditional-scenario-outputs-v1",
        "status": status,
        "baseline": {},
        "scenario": {},
        "delta": {},
        "raw": {},
        "warnings": warnings,
    }


def compute_scenario_outputs(
    prepared: dict[str, Any], include_baseline: bool = True
) -> dict[str, Any]:
    """Run the prepared baseline/scenario DPS simulations exactly once each.

    ``prepared`` is the output of :func:`prepare_scenario`.  Baseline single +
    loops and scenario single + loops are each computed once (the review found
    the report assembly was running ``run_loop`` up to 8 times per page).  The
    returned dict holds both the flat summaries the report needs and the raw
    engine results for timeline/consistency assembly::

        outputs = compute_scenario_outputs(prepared)
        raw_single = outputs["raw"]["scenario_single"]
        raw_loops = outputs["raw"]["scenario_loops"]
        baseline_single = outputs["raw"]["baseline_single"]
        delta = outputs["delta"]

    ``include_baseline=False`` skips the frozen baseline runs (the service may
    cache them); ``baseline``/``delta`` are then empty.
    """
    if not isinstance(prepared, dict) or prepared.get("status") == "error":
        return _compute_outputs_error(
            prepared,
            "error",
            list((prepared or {}).get("warnings") or []),
        )
    warnings = list(prepared.get("warnings") or [])
    ruleset = prepared.get("ruleset")
    baseline_ruleset = prepared.get("baseline_ruleset")
    if not isinstance(ruleset, Ruleset):
        return _compute_outputs_error(prepared, "error", warnings + ["prepared ruleset 不可用"])
    outputs: dict[str, Any] = {
        "schema": "bh-conditional-scenario-outputs-v1",
        "status": str(prepared.get("status") or "ok"),
        "include_baseline": bool(include_baseline),
        "baseline": {},
        "scenario": {},
        "delta": {},
        "raw": {},
        "warnings": warnings,
    }
    try:
        if include_baseline:
            if not isinstance(baseline_ruleset, Ruleset):
                raise ValueError("prepared baseline_ruleset 不可用")
            baseline_single = run_single_cast(
                prepared["baseline_snapshot"],
                baseline_ruleset,
                prepared["baseline_scenario"],
            )
            baseline_loops = [
                run_loop(
                    prepared["baseline_snapshot"],
                    baseline_ruleset,
                    prepared["baseline_scenario"],
                    policy,
                )
                for policy in baseline_ruleset.action_policies
            ]
        else:
            baseline_single = None
            baseline_loops = []
        scenario_single = run_single_cast(
            prepared["snapshot_single_cast"],
            ruleset,
            prepared["engine_scenario"],
        )
        scenario_loops = [
            run_loop(
                prepared["snapshot_loops"],
                ruleset,
                prepared["engine_scenario"],
                policy,
            )
            for policy in ruleset.action_policies
        ]
    except Exception as exc:
        warnings.append(f"场景 DPS 计算失败：{exc}")
        outputs["status"] = "error"
        outputs["warnings"] = warnings
        return outputs

    scenario_block = {
        "single": _single_summary(scenario_single),
        "loops": [_loop_summary(loop) for loop in scenario_loops],
    }
    outputs["scenario"] = scenario_block
    outputs["raw"] = {
        "scenario_single": scenario_single,
        "scenario_loops": scenario_loops,
    }
    if baseline_single is not None:
        baseline_block = {
            "single": _single_summary(baseline_single),
            "loops": [_loop_summary(loop) for loop in baseline_loops],
        }
        outputs["baseline"] = baseline_block
        outputs["delta"] = _delta_block(baseline_block, scenario_block)
        outputs["raw"]["baseline_single"] = baseline_single
        outputs["raw"]["baseline_loops"] = baseline_loops
    outputs["scenario_single"] = scenario_block["single"]
    outputs["scenario_loops"] = scenario_block["loops"]
    if outputs["baseline"]:
        outputs["baseline_single"] = outputs["baseline"]["single"]
        outputs["baseline_loops"] = outputs["baseline"]["loops"]
    # Ready-to-forward bundle for ``report.build_result(precomputed_outputs=...)``:
    # scenario dict + the already-computed single/loop engine results.
    outputs["report"] = {
        "scenario": copy.deepcopy(prepared.get("engine_scenario") or {}),
        "single": outputs["raw"]["scenario_single"],
        "loops": outputs["raw"]["scenario_loops"],
    }
    return outputs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _prepared_error(
    status: str,
    warnings: list[str],
    assumptions: list[str],
    *,
    validation: Optional[dict[str, Any]] = None,
    target: Optional[dict[str, Any]] = None,
    support: Optional[dict[str, Any]] = None,
    fingerprint: str = "",
) -> dict[str, Any]:
    return {
        "schema": PREPARED_SCHEMA,
        "status": status,
        "snapshot": None,
        "snapshot_loops": None,
        "snapshot_single_cast": None,
        "baseline_snapshot": None,
        "ruleset": None,
        "baseline_ruleset": None,
        "engine_scenario": {},
        "baseline_scenario": {},
        "monster_level": None,
        "monster_damage_reduction": None,
        "monster_dr_factor": None,
        "ruleset_info": {"id": None, "sha256": None},
        "scenario": {},
        "scenario_fingerprint": fingerprint,
        "evaluation": {},
        "target": dict(target or {}),
        "scenario_target": {},
        "target_conditions": {},
        "applied": [],
        "skipped": [],
        "warnings": list(warnings),
        "assumptions": list(assumptions),
        "condition_application": {
            "applied": [],
            "skipped": [],
            "target_conditions": {},
            "proposals_used": [],
            "auto_included": False,
        },
        "channel_support": dict(support or {}),
        "glyph_model": _glyph_model_disabled("unavailable"),
        "application_states": {},
        "assumptions_applied": [],
        "applied_effects": [],
        "baseline_profile": None,
        "include_auto": None,
        "include_auto_source": "",
        "validation": dict(validation or {}),
        "diagnostic": False,
    }


def prepare_scenario(
    snapshot: model.Snapshot,
    ruleset: Ruleset,
    *,
    scenario: dict[str, Any],
    registry: Registry,
    catalog: Optional[dict[str, Any]] = None,
    evaluation: Optional[dict[str, Any]] = None,
    monster_level: Optional[int] = None,
    include_auto: Optional[bool] = None,
    allow_stale: bool = False,
) -> dict[str, Any]:
    """Build the deterministic engine environment for one conditional scenario.

    Key 2026-11-07 review additions:

    * **R1 unified target** — the scenario ``target`` block
      (``monster_level`` / ``fire_resistance`` / ``boss`` / ``rare``) is
      resolved once and applied to *both* the baseline and scenario engine
      environments; ``target``/``scenario_target`` expose what was consumed.
    * **R2 baseline profile** — ``baseline_profile=legacy_single_skill``
      excludes ``auto_active`` proposals (Eternal Flames passive) while
      ``build_base`` (default) includes them; an explicit ``include_auto``
      argument overrides the profile policy.
    * **R7 strict validation** — scenario validation errors (e.g. stale
      binding) fail closed with ``status="error"`` and no contributions
      applied unless ``allow_stale=True`` is passed explicitly (diagnostic).
    * **R5 application states** / **R4 assumptions** — ``application_states``,
      ``assumptions_applied`` and ``applied_effects`` are produced for the UI.

    The returned dict contains live Python/model objects (not JSON); for
    persistence use ``prepared["snapshot"].to_dict()``,
    ``prepared["ruleset"].data`` and the already-plain blocks.  It never mutates
    the inputs and is deterministic.
    """
    warnings: list[str] = []
    assumptions: list[str] = [
        "基线使用与 report.build_result 相同的 ruleset 场景构造（含目标怪物 DR），不含任何条件加成。",
        "条件贡献按注册表 modifier 的 stat_key/operation 映射为 model.Contribution；"
        "more 提案使用独立 group（conditional:<modifier_id>），不与既有 more 合并。",
        "固定条件估算：选中的持续状态在观察期内保持，不模拟获得/维持成本与逐施法动作时序。",
    ]
    raw_scenario = copy.deepcopy(scenario) if isinstance(scenario, dict) else {}
    if not isinstance(ruleset, Ruleset):
        warnings.append("ruleset 不是已加载的 Ruleset 对象；无法构建条件场景。")
        return _prepared_error("error", warnings, assumptions)

    registry_ok = isinstance(registry, Registry)
    normalized_scenario: dict[str, Any] = raw_scenario
    if registry_ok:
        try:
            normalized_scenario = normalize_scenario(
                raw_scenario,
                registry,
                {"binding": _binding_from_catalog(catalog)},
            )
        except Exception as exc:  # defensive: keep the raw scenario
            warnings.append(f"场景归一化失败（{exc}）；按原始状态读取目标条件。")
            normalized_scenario = raw_scenario
    else:
        warnings.append(
            "registry 缺失/不可用：条件求值被跳过，仅重算基线口径（status=skipped）。"
        )
    if not isinstance(normalized_scenario, dict):
        normalized_scenario = raw_scenario

    # Unified target block (R1/R8); also used for the fail-closed error payload.
    try:
        target = _resolve_target_block(
            ruleset=ruleset,
            normalized_scenario=normalized_scenario,
            raw_scenario=raw_scenario,
            monster_level=monster_level,
            catalog=catalog,
            warnings=warnings,
        )
    except Exception as exc:
        warnings.append(f"目标参数不可用：{exc}")
        return _prepared_error(
            "error",
            warnings,
            assumptions,
            fingerprint=scenario_fingerprint(raw_scenario) if raw_scenario else "",
        )
    try:
        selected_level, mr_info = _resolve_monster_level(
            ruleset, int(target["monster_level"]), warnings
        )
    except Exception as exc:
        warnings.append(f"规则包目标侧（怪物等级/DR）不可用：{exc}")
        return _prepared_error(
            "error",
            warnings,
            assumptions,
            target=target,
            fingerprint=scenario_fingerprint(raw_scenario) if raw_scenario else "",
        )
    target["monster_level"] = int(selected_level)

    # -- evaluation -------------------------------------------------------
    evaluation_payload: dict[str, Any] = {}
    conditional_status = "ok"
    validation: dict[str, Any] = {}
    if registry_ok:
        try:
            if isinstance(evaluation, dict):
                evaluation_payload = evaluation
            else:
                evaluation_payload = evaluate_scenario(
                    normalized_scenario,
                    registry,
                    catalog if isinstance(catalog, dict) else {},
                )
        except Exception as exc:
            warnings.append(f"条件求值失败（{exc}）：拟议贡献不可用，仅重算基线口径。")
            evaluation_payload = {}
            conditional_status = "skipped"
        validation = evaluation_payload.get("validation") if isinstance(
            evaluation_payload, dict
        ) else None
        if not isinstance(validation, dict):
            try:
                validation = validate_scenario(
                    raw_scenario, registry, catalog if isinstance(catalog, dict) else {}
                )
            except Exception:  # pragma: no cover - defensive
                validation = {}
        # R7: even when a caller supplies a cached evaluation, re-validate the
        # raw scenario itself.  A cached evaluation may belong to the baseline
        # scenario, so stale bindings must fail closed at this entry point too.
        try:
            direct_validation = validate_scenario(
                raw_scenario, registry, catalog if isinstance(catalog, dict) else {}
            )
        except Exception:  # pragma: no cover - defensive
            direct_validation = {}
        if isinstance(direct_validation, dict):
            merged_errors = [
                error
                for error in (validation.get("errors") or [])
                if isinstance(error, dict)
            ]
            seen = {
                (str(error.get("code") or ""), str(error.get("message") or ""))
                for error in merged_errors
            }
            for error in direct_validation.get("errors") or []:
                if not isinstance(error, dict):
                    continue
                key = (str(error.get("code") or ""), str(error.get("message") or ""))
                if key not in seen:
                    merged_errors.append(error)
                    seen.add(key)
            merged_warnings = [
                warning
                for warning in (validation.get("warnings") or [])
                if isinstance(warning, dict)
            ]
            seen_warnings = {
                (str(warning.get("code") or ""), str(warning.get("message") or ""))
                for warning in merged_warnings
            }
            for warning in direct_validation.get("warnings") or []:
                if not isinstance(warning, dict):
                    continue
                key = (str(warning.get("code") or ""), str(warning.get("message") or ""))
                if key not in seen_warnings:
                    merged_warnings.append(warning)
                    seen_warnings.add(key)
            validation = {"errors": merged_errors, "warnings": merged_warnings}
    else:
        conditional_status = "skipped"
        validation = {"errors": [], "warnings": []}

    validation_errors = [
        error for error in (validation.get("errors") or []) if isinstance(error, dict)
    ]
    for error in validation_errors:
        warnings.append(
            "场景校验错误："
            + str(error.get("code") or "error")
            + " - "
            + str(error.get("message") or "")
        )
    if validation_errors and not allow_stale:
        warnings.append(
            "场景校验失败（fail-closed）：存在影响正确性的校验错误，条件贡献未应用；"
            "如需诊断性宽松重算请显式传入 allow_stale=True。"
        )
        return _prepared_error(
            "error",
            warnings,
            assumptions,
            validation=validation,
            target=target,
            support=channel_support(registry) if registry_ok else {},
            fingerprint=scenario_fingerprint(normalized_scenario)
            if isinstance(normalized_scenario, dict)
            else "",
        )
    diagnostic = bool(validation_errors and allow_stale)
    if diagnostic:
        assumptions.append(
            "诊断性重算（allow_stale=True）：场景校验存在错误但仍继续，结果不得作为正式口径。"
        )

    # -- baseline profile policy (R2) -------------------------------------
    include_auto_resolved, include_auto_source = _resolve_include_auto(
        normalized_scenario,
        registry if registry_ok else None,
        include_auto,
        warnings,
    )

    # -- target conditions + environments --------------------------------
    baseline_target_conditions = {
        "target_is_boss_or_rare": bool(target["boss"] or target["rare"]),
    }
    target_conditions = _target_condition_overrides(
        normalized_scenario, target, warnings, assumptions
    )
    baseline_scenario = _engine_scenario(
        ruleset,
        mr_info,
        target_conditions=baseline_target_conditions,
        target_params=target,
    )
    engine_scenario = _engine_scenario(
        ruleset,
        mr_info,
        target_conditions=target_conditions,
        target_params=target,
    )
    baseline_snapshot = copy.deepcopy(snapshot)
    baseline_ruleset = _scenario_ruleset(
        ruleset,
        baseline_target_conditions,
        assumptions,
        fire_resistance=target["fire_resistance"],
    )

    glyph_mode, glyph_model = _resolve_glyph_mode(
        snapshot=snapshot,
        ruleset=ruleset,
        registry=registry,
        catalog=catalog,
        normalized_scenario=normalized_scenario,
        target_conditions=target_conditions,
        target_params=target,
        engine_scenario=engine_scenario,
        assumptions=assumptions,
        warnings=warnings,
    )
    scenario_ruleset = _scenario_ruleset(
        ruleset,
        target_conditions,
        assumptions,
        disable_heat_wave=glyph_mode,
        fire_resistance=target["fire_resistance"],
    )

    # -- proposals / contributions ---------------------------------------
    proposals = (
        evaluation_payload.get("proposed_contributions")
        if isinstance(evaluation_payload, dict)
        else None
    )
    proposals = proposals if isinstance(proposals, list) else []
    skip_entries: list[dict[str, Any]] = []
    specs: list[dict[str, Any]] = []
    if registry_ok:
        try:
            mapping_proposals = [
                proposal
                for proposal in proposals
                if not (
                    glyph_mode
                    and isinstance(proposal, dict)
                    and str(proposal.get("effect_id")) == APEX_EFFECT_ID
                )
            ]
            specs = _build_proposal_specs(
                mapping_proposals,
                snapshot=snapshot,
                registry=registry,
                normalized_scenario=normalized_scenario,
                include_auto=include_auto_resolved,
                warnings=warnings,
                assumptions=assumptions,
                skipped=skip_entries,
            )
        except Exception as exc:  # a malformed proposal must not kill the run
            warnings.append(f"提案映射失败（{exc}）：条件贡献未应用。")
            specs = []
            conditional_status = "skipped"

    applied = _applied_entries(specs)
    if glyph_mode:
        glyph_proposal_id = ""
        for proposal in proposals:
            if (
                isinstance(proposal, dict)
                and str(proposal.get("effect_id")) == APEX_EFFECT_ID
            ):
                glyph_proposal_id = str(proposal.get("proposal_id") or "")
                break
        applied.append(
            _glyph_applied_entry(
                glyph_model, glyph_proposal_id or f"glyph:{APEX_EFFECT_ID}"
            )
        )
    applied.sort(key=lambda entry: (entry["effect_id"], entry["modifier_id"]))
    skipped = skip_entries + _evaluation_skipped_entries(
        evaluation_payload,
        already_skipped=skip_entries,
        applied=applied,
    )
    skipped.sort(
        key=lambda entry: (
            str(entry.get("effect_id") or ""),
            str(entry.get("scope") or ""),
            str(entry.get("reason") or ""),
            str(entry.get("modifier_id") or ""),
        )
    )
    proposals_used = sorted(
        {entry["proposal_id"] for entry in applied if entry["proposal_id"]}
    )
    applied_effects = sorted({str(entry["effect_id"]) for entry in applied})
    assumptions_applied = _assumptions_applied_rows(specs, glyph_model)
    support = channel_support(registry) if registry_ok else {}
    application_states = _application_states(
        registry=registry,
        applied=applied,
        skipped=skipped,
        assumptions_applied=assumptions_applied,
        support=support,
    )

    snapshot_loops = _snapshot_with_specs(snapshot, specs, "loops", registry)
    snapshot_single_cast = _snapshot_with_specs(
        snapshot, specs, "single_cast", registry
    )

    fingerprint = ""
    try:
        fingerprint = scenario_fingerprint(normalized_scenario)
    except Exception:  # pragma: no cover - defensive
        fingerprint = ""

    condition_application = {
        "applied": applied,
        "skipped": skipped,
        "target_conditions": dict(target_conditions),
        "proposals_used": proposals_used,
        "auto_included": bool(include_auto_resolved),
    }
    scenario_target = _scenario_target_payload(target, target_conditions)
    try:
        scenario_target["effective_fire_resistance"] = float(
            engine_scenario["target"]["resistances"]["fire"]
        )
    except Exception:  # pragma: no cover - defensive
        scenario_target["effective_fire_resistance"] = float(
            target.get("fire_resistance", 0.0)
        )
    scenario_target["fire_resistance_shred"] = (
        float(glyph_model.get("total_shred", 0.0))
        if isinstance(glyph_model, dict) and glyph_model.get("enabled")
        else 0.0
    )
    return {
        "schema": PREPARED_SCHEMA,
        "status": conditional_status,
        "snapshot": snapshot_loops,
        "snapshot_loops": snapshot_loops,
        "snapshot_single_cast": snapshot_single_cast,
        "baseline_snapshot": baseline_snapshot,
        "ruleset": scenario_ruleset,
        "baseline_ruleset": baseline_ruleset,
        "engine_scenario": engine_scenario,
        "baseline_scenario": baseline_scenario,
        "monster_level": int(selected_level),
        "monster_damage_reduction": float(mr_info["monster_damage_reduction"]),
        "monster_dr_factor": float(mr_info["monster_dr_factor"]),
        "ruleset_info": _ruleset_info(ruleset),
        "scenario": normalized_scenario,
        "scenario_fingerprint": fingerprint,
        "evaluation": evaluation_payload,
        "target": dict(target),
        "scenario_target": scenario_target,
        "target_conditions": dict(target_conditions),
        "applied": applied,
        "skipped": skipped,
        "warnings": warnings,
        "assumptions": assumptions,
        "condition_application": condition_application,
        "channel_support": support,
        "glyph_model": glyph_model,
        "application_states": application_states,
        "assumptions_applied": assumptions_applied,
        "applied_effects": applied_effects,
        "baseline_profile": str(
            normalized_scenario.get("baseline_profile") or "build_base"
        ),
        "include_auto": bool(include_auto_resolved),
        "include_auto_source": include_auto_source,
        "validation": validation,
        "diagnostic": diagnostic,
    }


def recompute_scenario(
    snapshot: model.Snapshot,
    ruleset: Ruleset,
    *,
    scenario: dict[str, Any],
    registry: Registry,
    catalog: Optional[dict[str, Any]] = None,
    evaluation: Optional[dict[str, Any]] = None,
    monster_level: Optional[int] = None,
    include_auto: Optional[bool] = None,
    allow_stale: bool = False,
    precomputed_outputs: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Recompute single-cast/loop DPS with the selected conditional Buffs applied.

    This is ``prepare_scenario`` + :func:`compute_scenario_outputs`: the
    baseline and scenario single/loop simulations run exactly once each, then
    the prepared application metadata is embedded into the result.

    ``precomputed_outputs`` may carry an already executed
    :func:`compute_scenario_outputs` result (e.g. the one-click service runs it
    once for the report pages); it is reused instead of running the engine a
    second time.

    Review fixes reflected here: unified target block (R1), baseline-profile
    policy (R2), fail-closed validation with explicit ``allow_stale`` diagnostic
    bypass (R7), assumption metadata on applied modifiers (R4),
    ``application_states``/``applied_effects`` for the UI (R5), independent
    boss/rare flags for the glyph shred model (R8).
    """
    prepared = prepare_scenario(
        snapshot,
        ruleset,
        scenario=scenario,
        registry=registry,
        catalog=catalog,
        evaluation=evaluation,
        monster_level=monster_level,
        include_auto=include_auto,
        allow_stale=allow_stale,
    )
    warnings = list(prepared.get("warnings") or [])
    assumptions = list(prepared.get("assumptions") or [])
    selected_level = prepared.get("monster_level")
    if isinstance(precomputed_outputs, dict) and precomputed_outputs.get(
        "schema"
    ) == "bh-conditional-scenario-outputs-v1":
        outputs = precomputed_outputs
    else:
        outputs = compute_scenario_outputs(prepared)
    if prepared.get("status") == "error" or outputs.get("status") == "error":
        payload = _empty_payload(
            status="error",
            ruleset=ruleset if isinstance(ruleset, Ruleset) else None,
            monster_level=selected_level,
            warnings=warnings + list(outputs.get("warnings") or []),
            assumptions=assumptions,
            scenario={},
            fingerprint=prepared.get("scenario_fingerprint") or "",
            dps_recomputed=False,
        )
        payload["target"] = dict(prepared.get("target") or {})
        payload["scenario_target"] = dict(prepared.get("scenario_target") or {})
        payload["validation"] = dict(prepared.get("validation") or {})
        payload["diagnostic"] = bool(prepared.get("diagnostic"))
        payload["channel_support"] = dict(prepared.get("channel_support") or {})
        return payload

    status = str(prepared.get("status") or "ok")
    if status not in ("ok", "skipped"):
        status = "ok"
    payload = _empty_payload(
        status=status,
        ruleset=ruleset if isinstance(ruleset, Ruleset) else None,
        monster_level=selected_level,
        warnings=warnings,
        assumptions=assumptions,
        scenario={},
        fingerprint=prepared.get("scenario_fingerprint") or "",
        dps_recomputed=True,
    )
    payload["baseline"] = outputs.get("baseline") or {}
    payload["scenario"] = outputs.get("scenario") or {}
    payload["delta"] = outputs.get("delta") or {}
    payload["monster_damage_reduction"] = float(
        prepared.get("monster_damage_reduction") or 0.0
    )
    payload["monster_dr_factor"] = float(prepared.get("monster_dr_factor") or 1.0)
    payload["channel_support"] = prepared.get("channel_support") or {}
    payload["glyph_model"] = prepared.get("glyph_model") or {}
    payload["condition_application"] = prepared.get("condition_application") or {
        "applied": [],
        "skipped": [],
        "target_conditions": {},
        "proposals_used": [],
        "auto_included": False,
    }
    payload["target"] = dict(prepared.get("target") or {})
    payload["scenario_target"] = dict(prepared.get("scenario_target") or {})
    payload["target_conditions"] = dict(prepared.get("target_conditions") or {})
    payload["application_states"] = prepared.get("application_states") or {}
    payload["assumptions_applied"] = prepared.get("assumptions_applied") or []
    payload["applied_effects"] = prepared.get("applied_effects") or []
    payload["baseline_profile"] = prepared.get("baseline_profile")
    payload["include_auto"] = prepared.get("include_auto")
    payload["include_auto_source"] = prepared.get("include_auto_source")
    payload["validation"] = dict(prepared.get("validation") or {})
    payload["diagnostic"] = bool(prepared.get("diagnostic"))
    return payload


__all__ = [
    "APEX_EFFECT_ID",
    "APEX_MODIFIER_ID",
    "GLYPH_BOSS_EFFECTIVENESS",
    "GLYPH_STATE_ID",
    "PREPARED_SCHEMA",
    "SCHEMA",
    "SOURCE_KIND_CONDITIONAL",
    "STAT_ALIASES",
    "SUPPORTED_OPERATIONS",
    "channel_support",
    "compute_scenario_outputs",
    "prepare_scenario",
    "recompute_scenario",
]
