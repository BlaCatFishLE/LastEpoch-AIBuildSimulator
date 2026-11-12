"""Conditional scenario: defaults, preset application, validation, IO, fingerprint.

Contract reference: ``docs/contracts/conditional-interface.md`` section 7.

A scenario is a plain JSON document.  In memory the package always works with
the *resolved* form: every user shared state, every controllable effect, every
cast-state flag and the loop defaults are present explicitly.  This keeps
evaluation/fingerprints deterministic and lets a saved scenario be re-loaded
without depending on registry defaults that may change later.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from . import constants as C
from .registry import Registry

BINDING_KEYS = (
    "build_id",
    "revision_id",
    "build_content_sha256",
    "calc_input_sha256",
    "dataset_id",
    "registry_id",
    "registry_sha256",
    "ruleset_id",
    "ruleset_sha256",
)

LOOP_KEYS = ("crescendoing_rage_enabled", "initial_eligible_cast_count", "action_stream")
LOOP_DEFAULTS = {
    "crescendoing_rage_enabled": False,
    "initial_eligible_cast_count": 0,
    "action_stream": "black_hole_only",
}
MAX_INITIAL_CAST_COUNT = 2
ACTION_STREAMS = ("black_hole_only",)

#: unified conditional-scenario target block (R1, 2026-11-07 review).
#: ``monster_level`` feeds the target-side enemy DR; ``fire_resistance`` is the
#: target fire resistance as a fraction (negative = already shred/penetrated
#: below zero, e.g. -0.2); ``boss``/``rare`` are independent target flags
#: (Supermassive uses ``boss OR rare``; glyph shred uses ``boss`` only).
TARGET_KEYS = ("monster_level", "fire_resistance", "boss", "rare")
TARGET_DEFAULTS = {
    "monster_level": 0,
    "fire_resistance": 0.0,
    "boss": True,
    "rare": True,
}
TARGET_MONSTER_LEVEL_RANGE = (0, 100)
#: Reshred can push effective resistance negative; the scenario stores the
#: *raw* target resistance, so allow a generous sanity range (-200% .. 200%).
TARGET_FIRE_RESISTANCE_RANGE = (-2.0, 2.0)

#: fallback cast-state keys used when a registry declares none (kept in sync
#: with the scenario example in the contract)
FALLBACK_CAST_KEYS = ("crescendoing_rage_applies", "preparation_ready")

SCENARIO_KEYS = (
    "schema",
    "mode",
    "baseline_profile",
    "preset_id",
    "binding",
    "target",
    "states",
    "effects",
    "single_cast",
    "loop",
)


def _target_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _target_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def target_from_context(context: Any) -> Optional[dict[str, Any]]:
    """Extract a normalized scenario ``target`` block from a build/DPS context.

    Accepts ``{"target": {...}}`` with either the unified conditional-scenario
    fields (``monster_level`` / ``fire_resistance`` / ``boss`` / ``rare``) or
    the DPS-result target shape (``monster_level`` / ``resistances.fire`` /
    ``boss_tag`` / ``rare_tag``).  Returns ``None`` when the context carries no
    target information (old callers keep the legacy scenario without a target).
    """
    if not isinstance(context, dict):
        return None
    candidate = context.get("target")
    if not isinstance(candidate, dict):
        candidate = context.get("dps_target") if isinstance(context.get("dps_target"), dict) else None
    if not isinstance(candidate, dict):
        return None
    target: dict[str, Any] = {}
    level = _target_int(candidate.get("monster_level"))
    if level is None:
        level = _target_int(context.get("monster_level"))
    if level is not None:
        target["monster_level"] = level
    fire = _target_number(candidate.get("fire_resistance"))
    if fire is None and isinstance(candidate.get("resistances"), dict):
        fire = _target_number(candidate["resistances"].get("fire"))
    if fire is not None:
        target["fire_resistance"] = fire
    boss = candidate.get("boss")
    if not isinstance(boss, bool):
        boss = candidate.get("boss_tag")
    rare = candidate.get("rare")
    if not isinstance(rare, bool):
        rare = candidate.get("rare_tag")
    conditions = context.get("target_conditions")
    if isinstance(conditions, dict) and isinstance(conditions.get("target_is_boss_or_rare"), bool):
        # legacy single combined flag: boss OR rare was true; both unknown.
        if not isinstance(boss, bool) and not isinstance(rare, bool):
            boss = bool(conditions["target_is_boss_or_rare"])
            rare = bool(conditions["target_is_boss_or_rare"])
    if isinstance(boss, bool):
        target["boss"] = boss
    if isinstance(rare, bool):
        target["rare"] = rare
    return target or None


def resolve_target(
    scenario: Any, context: Any = None
) -> Optional[dict[str, Any]]:
    """Resolve the scenario target: context defaults overridden by scenario.

    Old scenarios (no ``target`` block) inherit the context target; when the
    context has none either, ``None`` is returned so the scenario keeps its
    legacy shape.  The legacy ``states.target.boss_or_rare`` flag is bridged
    only when the scenario itself carries no target block.
    """
    provided = scenario.get("target") if isinstance(scenario, dict) else None
    provided = provided if isinstance(provided, dict) else None
    base = target_from_context(context)
    if base is None and provided is None:
        return None
    target: dict[str, Any] = dict(base or {})
    if provided is not None:
        for key in TARGET_KEYS:
            if key not in provided:
                continue
            value = provided[key]
            if key == "monster_level":
                number = _target_int(value)
            elif key == "fire_resistance":
                number = _target_number(value)
            else:
                number = value if isinstance(value, bool) else None
            if number is not None:
                target[key] = number
    if provided is None and isinstance(scenario, dict):
        states = scenario.get("states")
        legacy = states.get("target.boss_or_rare") if isinstance(states, dict) else None
        if isinstance(legacy, bool):
            target["boss"] = legacy
            target["rare"] = legacy
    return target or None


class ScenarioError(ValueError):
    """Raised for unreadable scenario files / unknown presets."""

    def __init__(self, issues: list[dict[str, Any]] | None = None, message: str | None = None):
        self.issues = list(issues or [])
        if message is None:
            if self.issues:
                message = str(self.issues[0].get("message") or "invalid scenario")
            else:  # pragma: no cover - defensive
                message = "invalid scenario"
        super().__init__(message)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


# ---------------------------------------------------------------------------
# Binding helpers
# ---------------------------------------------------------------------------


def _empty_binding() -> dict[str, Any]:
    return {key: None for key in BINDING_KEYS}


def binding_from_context(context: Any) -> dict[str, Any]:
    """Build the scenario ``binding`` block from a build-context/fingerprints dict.

    Accepts either ``{"binding": {...}}`` or a dict resembling
    ``result["conditional"]`` (``input``/``registry``/``ruleset`` keys).
    Unknown values stay ``None`` so validation can skip the comparison.
    """
    binding = _empty_binding()
    if not isinstance(context, dict):
        return binding
    candidate = context.get("binding")
    if isinstance(candidate, dict):
        for key in BINDING_KEYS:
            if candidate.get(key) is not None:
                binding[key] = candidate.get(key)
        return binding

    input_block = context.get("input") if isinstance(context.get("input"), dict) else {}
    build = input_block.get("build") if isinstance(input_block.get("build"), dict) else {}
    calc_input = input_block.get("calc_input") if isinstance(input_block.get("calc_input"), dict) else {}
    dataset = input_block.get("dataset") if isinstance(input_block.get("dataset"), dict) else {}
    registry = context.get("registry") if isinstance(context.get("registry"), dict) else {}
    ruleset = input_block.get("ruleset") if isinstance(input_block.get("ruleset"), dict) else {}

    mapping = {
        "build_id": build.get("build_id"),
        "revision_id": build.get("revision_id"),
        "build_content_sha256": build.get("content_sha256"),
        "calc_input_sha256": calc_input.get("sha256"),
        "dataset_id": dataset.get("dataset_id"),
        "registry_id": registry.get("id"),
        "registry_sha256": registry.get("sha256"),
        "ruleset_id": ruleset.get("id"),
        "ruleset_sha256": ruleset.get("sha256"),
    }
    for key, value in mapping.items():
        if value is not None:
            binding[key] = value
    for key in BINDING_KEYS:
        if context.get(key) is not None:
            binding[key] = context.get(key)
    return binding


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def _cast_state_keys(registry: Registry) -> list[str]:
    keys = list(registry.cast_state_keys)
    if not keys:
        keys = list(FALLBACK_CAST_KEYS)
    return keys


def state_defaults(registry: Registry) -> dict[str, Any]:
    """Default value of every ``source == "user"`` shared state."""
    defaults: dict[str, Any] = {}
    for state in registry.user_states():
        state_id = state.get("id")
        if not state_id:
            continue
        defaults[str(state_id)] = state.get("default")
    return defaults


def _default_state_value(state: dict[str, Any]) -> Any:
    if state.get("kind") == "int":
        try:
            return int(state.get("default") or 0)
        except (TypeError, ValueError):
            return 0
    return bool(state.get("default"))


def _effect_control_default(effect: dict[str, Any], registry: Registry) -> Optional[dict[str, Any]]:
    control = effect.get("control") or {}
    kind = control.get("kind")
    if kind == "toggle":
        return {"enabled": bool(control.get("default", False))}
    if kind == "stacks":
        binding = registry.control_binding(effect)
        return {"stacks": int(binding.get("default") or 0)}
    return None


def _base_fragment(registry: Registry) -> dict[str, Any]:
    states: dict[str, Any] = {}
    for state in registry.user_states():
        state_id = str(state.get("id") or "")
        if state_id:
            states[state_id] = _default_state_value(state)
    effects: dict[str, Any] = {}
    for effect in registry.effects:
        effect_id = str(effect.get("effect_id") or "")
        config = _effect_control_default(effect, registry)
        if effect_id and config is not None:
            effects[effect_id] = config
    single_cast = {key: False for key in _cast_state_keys(registry)}
    return {
        "states": states,
        "effects": effects,
        "single_cast": single_cast,
        "loop": dict(LOOP_DEFAULTS),
    }


def _merge_fragment(base: dict[str, Any], fragment: Any) -> None:
    if not isinstance(fragment, dict):
        return
    for section in ("states", "effects", "single_cast", "loop"):
        values = fragment.get(section)
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            base[section][key] = value


def _registry_identity(registry: Registry) -> tuple[str, str]:
    mode = registry.mode()
    profile = registry.baseline_profile()
    return str(mode.get("id") or C.DEFAULT_MODE_ID), str(profile.get("id") or C.DEFAULT_BASELINE_PROFILE_ID)


def _sanitize_fragment(registry: Registry, base: dict[str, Any]) -> None:
    """Keep only registry-valid sections when composing from a preset."""
    for state_id in list(base["states"]):
        state = registry.state(state_id)
        if state is None or state.get("source") != "user":
            base["states"].pop(state_id, None)
    for effect_id in list(base["effects"]):
        effect = registry.effect(effect_id)
        if effect is None:
            base["effects"].pop(effect_id, None)
            continue
        kind = registry.control_binding(effect).get("kind")
        config = base["effects"][effect_id]
        if not isinstance(config, dict):
            config = {}
        if kind == "toggle":
            base["effects"][effect_id] = {"enabled": bool(config.get("enabled", False))}
        elif kind == "stacks":
            value = config.get("stacks")
            if isinstance(value, bool) or not isinstance(value, int):
                value = int(registry.control_binding(effect).get("default") or 0)
            base["effects"][effect_id] = {"stacks": int(value)}
        else:
            base["effects"].pop(effect_id, None)
    allowed_cast = set(_cast_state_keys(registry))
    cleaned_cast: dict[str, bool] = {key: False for key in allowed_cast}
    for key, value in base["single_cast"].items():
        if key in allowed_cast:
            cleaned_cast[key] = bool(value)
    base["single_cast"] = cleaned_cast
    cleaned_loop = dict(LOOP_DEFAULTS)
    loop = base.get("loop") or {}
    if isinstance(loop.get("crescendoing_rage_enabled"), bool):
        cleaned_loop["crescendoing_rage_enabled"] = loop["crescendoing_rage_enabled"]
    count = loop.get("initial_eligible_cast_count")
    if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= MAX_INITIAL_CAST_COUNT:
        cleaned_loop["initial_eligible_cast_count"] = count
    stream = loop.get("action_stream")
    if stream in ACTION_STREAMS:
        cleaned_loop["action_stream"] = stream
    base["loop"] = cleaned_loop


def _compose_scenario(
    registry: Registry,
    *,
    binding: dict[str, Any],
    mode: str,
    baseline_profile: str,
    preset_id: str,
    fragment: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = _base_fragment(registry)
    _merge_fragment(base, fragment)
    _sanitize_fragment(registry, base)
    scenario = {
        "schema": C.SCHEMA_SCENARIO,
        "mode": mode,
        "baseline_profile": baseline_profile,
        "preset_id": preset_id,
        "binding": dict(binding),
        "states": base["states"],
        "effects": base["effects"],
        "single_cast": base["single_cast"],
        "loop": base["loop"],
    }
    if target:
        scenario["target"] = dict(target)
    return {key: scenario[key] for key in SCENARIO_KEYS if key in scenario}


def default_scenario(registry: Registry, context: Any = None) -> dict[str, Any]:
    """Return the build-base scenario (registry default preset applied).

    ``context`` may carry ``binding`` and a ``target`` block (the DPS scenario's
    target parameters, see :func:`target_from_context`); the target is copied
    into the composed scenario so old callers without a target stay unchanged.
    """
    binding = binding_from_context(context)
    target = target_from_context(context)
    mode, baseline_profile = _registry_identity(registry)
    preset = registry.default_preset()
    preset_id = "custom"
    fragment: dict[str, Any] | None = None
    if preset is not None:
        preset_id = str(preset.get("preset_id") or "custom")
        fragment = preset
    return _compose_scenario(
        registry,
        binding=binding,
        mode=mode,
        baseline_profile=baseline_profile,
        preset_id=preset_id,
        fragment=fragment,
        target=target,
    )


def apply_preset(
    registry: Registry,
    preset_id: str,
    context: Any = None,
) -> dict[str, Any]:
    """Build a scenario from one registry preset; raises for unknown ids."""
    preset = registry.preset(preset_id)
    if preset is None:
        raise ScenarioError(
            [
                {
                    "code": "unknown_preset",
                    "message": f"unknown conditional preset {preset_id!r}",
                }
            ]
        )
    binding = binding_from_context(context)
    target = target_from_context(context)
    mode, baseline_profile = _registry_identity(registry)
    return _compose_scenario(
        registry,
        binding=binding,
        mode=mode,
        baseline_profile=baseline_profile,
        preset_id=str(preset.get("preset_id") or preset_id),
        fragment=preset,
        target=target,
    )


def normalize_scenario(
    scenario: Optional[dict[str, Any]],
    registry: Registry,
    context: Any = None,
) -> dict[str, Any]:
    """Resolve a (possibly sparse) scenario into the canonical full form.

    Unknown ids are dropped here; ``validate_scenario`` reports them as errors.
    """
    binding = binding_from_context(context)
    mode, baseline_profile = _registry_identity(registry)
    target = resolve_target(scenario if isinstance(scenario, dict) else None, context)
    if not isinstance(scenario, dict):
        return _compose_scenario(
            registry,
            binding=binding,
            mode=mode,
            baseline_profile=baseline_profile,
            preset_id="custom",
            target=target,
        )
    resolved = _compose_scenario(
        registry,
        binding=binding,
        mode=str(scenario.get("mode") or mode),
        baseline_profile=str(scenario.get("baseline_profile") or baseline_profile),
        preset_id=str(scenario.get("preset_id") or "custom"),
        target=target,
    )
    scenario_binding = scenario.get("binding")
    if isinstance(scenario_binding, dict):
        for key in BINDING_KEYS:
            if key in scenario_binding:
                resolved["binding"][key] = scenario_binding.get(key)
    states = scenario.get("states")
    if isinstance(states, dict):
        for state_id, value in states.items():
            state = registry.state(str(state_id))
            if state is None or state.get("source") != "user":
                continue
            resolved["states"][str(state_id)] = value
    effects = scenario.get("effects")
    if isinstance(effects, dict):
        for effect_id, config in effects.items():
            effect = registry.effect(str(effect_id))
            if effect is None or not isinstance(config, dict):
                continue
            binding_info = registry.control_binding(effect)
            kind = binding_info.get("kind")
            if kind == "toggle":
                resolved["effects"][str(effect_id)]["enabled"] = bool(config.get("enabled", False))
            elif kind == "stacks":
                value = config.get("stacks")
                if isinstance(value, bool) or not isinstance(value, int):
                    value = int(binding_info.get("default") or 0)
                resolved["effects"][str(effect_id)]["stacks"] = int(value)
    single_cast = scenario.get("single_cast")
    allowed_cast = set(_cast_state_keys(registry))
    if isinstance(single_cast, dict):
        for key, value in single_cast.items():
            if str(key) in allowed_cast:
                resolved["single_cast"][str(key)] = bool(value)
    loop = scenario.get("loop")
    if isinstance(loop, dict):
        for key in LOOP_KEYS:
            if key not in loop:
                continue
            value = loop.get(key)
            if key == "initial_eligible_cast_count":
                if isinstance(value, bool) or not isinstance(value, int):
                    continue
            elif key == "crescendoing_rage_enabled":
                if not isinstance(value, bool):
                    continue
            elif key == "action_stream":
                if not isinstance(value, str):
                    continue
            resolved["loop"][key] = value
    # Bind user-selected controls to their declared shared state.  The control
    # *is* the state widget: a toggle/cast-state/stacks control always owns its
    # bound state, so the resolved scenario keeps the two views consistent (the
    # page renders both the shared-state section and the effect card from the
    # same value).  Conditions on other shared states stay independent, which
    # is what produces "selected but condition unmet" configurations.
    for effect in registry.effects:
        if not isinstance(effect, dict):
            continue
        control = effect.get("control") or {}
        binding = registry.control_binding(effect)
        kind = binding.get("kind")
        state_id = _text(control.get("state_id"))
        if kind in ("toggle", "cast_state") and state_id and registry.state(state_id) is not None:
            if registry.state(state_id).get("source") != "user":
                continue
            if kind == "toggle":
                config = (scenario.get("effects") or {}).get(str(effect.get("effect_id") or ""))
                enabled = bool(config.get("enabled")) if isinstance(config, dict) else False
                resolved["states"][state_id] = enabled
            else:
                key = str(binding.get("cast_state_id") or "")
                resolved["states"][state_id] = bool(resolved["single_cast"].get(key))
        elif kind == "stacks":
            stack_state_id = _text(control.get("stack_state_id"))
            if not stack_state_id or registry.state(stack_state_id) is None:
                continue
            if registry.state(stack_state_id).get("source") != "user":
                continue
            effect_id = str(effect.get("effect_id") or "")
            config = (scenario.get("effects") or {}).get(effect_id)
            stacks = 0
            if isinstance(config, dict):
                raw_stacks = config.get("stacks")
                if isinstance(raw_stacks, int) and not isinstance(raw_stacks, bool):
                    stacks = raw_stacks
            resolved["states"][stack_state_id] = stacks
    # Unified target block (R1/R8): keep the legacy combined
    # ``target.boss_or_rare`` state in sync with the independent boss/rare
    # flags so Supermassive evaluation keeps using "boss OR rare" while the
    # glyph shred model can use the boss flag alone.
    if isinstance(resolved.get("target"), dict):
        target_block = resolved["target"]
        legacy_combined = resolved["states"].get("target.boss_or_rare", True)
        if not isinstance(target_block.get("boss"), bool):
            target_block["boss"] = (
                legacy_combined if isinstance(legacy_combined, bool) else True
            )
        if not isinstance(target_block.get("rare"), bool):
            target_block["rare"] = (
                legacy_combined if isinstance(legacy_combined, bool) else True
            )
        if "target.boss_or_rare" in resolved["states"]:
            resolved["states"]["target.boss_or_rare"] = bool(
                target_block["boss"] or target_block["rare"]
            )
    return resolved


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def scenario_fingerprint(scenario: dict[str, Any]) -> str:
    """sha256 over canonical JSON of the scenario *excluding* top-level preset_id."""
    payload = {key: value for key, value in (scenario or {}).items() if key != "preset_id"}
    return C.json_fingerprint(payload)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _scenario_error(
    errors: list[dict[str, Any]],
    code: str,
    message: str,
    **extra: Any,
) -> None:
    entry: dict[str, Any] = {"code": code, "message": message}
    for key, value in extra.items():
        if value is not None:
            entry[key] = value
    errors.append(entry)


def _scenario_warning(
    warnings: list[dict[str, Any]],
    code: str,
    message: str,
    **extra: Any,
) -> None:
    entry: dict[str, Any] = {"code": code, "message": message}
    for key, value in extra.items():
        if value is not None:
            entry[key] = value
    warnings.append(entry)


def _check_bool_value(value: Any) -> bool:
    return isinstance(value, bool)


def validate_scenario(
    scenario: Any,
    registry: Registry,
    catalog: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Validate a scenario against the registry and the current build catalog.

    Returns ``{"errors": [...], "warnings": [...]}``; each entry is
    ``{"code", "message", optional "effect_id"/"state_id"}`` per contract §7.
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if not isinstance(scenario, dict):
        _scenario_error(errors, "invalid_type", "scenario must be a JSON object")
        return {"errors": errors, "warnings": warnings}
    if scenario.get("schema") != C.SCHEMA_SCENARIO:
        _scenario_error(
            errors,
            "schema_mismatch",
            f"scenario.schema={scenario.get('schema')!r} must equal {C.SCHEMA_SCENARIO!r}",
        )
    allowed_modes = {str(mode.get("id")) for mode in registry.modes}
    mode = scenario.get("mode")
    if mode is None:
        _scenario_error(errors, "missing_field", "scenario.mode is required")
    elif str(mode) not in allowed_modes:
        _scenario_error(errors, "unknown_mode", f"scenario.mode={mode!r} is not a registered mode")
    allowed_profiles = {str(profile.get("id")) for profile in registry.baseline_profiles}
    baseline_profile = scenario.get("baseline_profile")
    if baseline_profile is None:
        _scenario_error(errors, "missing_field", "scenario.baseline_profile is required")
    elif str(baseline_profile) not in allowed_profiles:
        _scenario_error(
            errors,
            "unknown_baseline_profile",
            f"scenario.baseline_profile={baseline_profile!r} is not a registered baseline profile",
        )

    preset_id = scenario.get("preset_id")
    if preset_id is not None:
        if not isinstance(preset_id, str):
            _scenario_error(errors, "invalid_type", "scenario.preset_id must be a string")
        elif preset_id != "custom" and registry.preset(preset_id) is None:
            _scenario_error(errors, "unknown_preset", f"scenario.preset_id={preset_id!r} is not a registry preset")

    for key in scenario:
        if key not in SCENARIO_KEYS:
            _scenario_warning(warnings, "unknown_field", f"scenario has unknown top-level field {key!r}")

    # -- unified target block (R1/R8) -------------------------------------
    target = scenario.get("target")
    if target is not None:
        if not isinstance(target, dict):
            _scenario_error(errors, "invalid_type", "scenario.target must be a JSON object")
            target = {}
        else:
            for key in target:
                if key not in TARGET_KEYS:
                    _scenario_warning(
                        warnings,
                        "unknown_target_field",
                        f"scenario.target.{key} is not a known target field",
                    )
            low_level, high_level = TARGET_MONSTER_LEVEL_RANGE
            if "monster_level" in target:
                level = target.get("monster_level")
                if isinstance(level, bool) or not isinstance(level, int):
                    _scenario_error(
                        errors,
                        "invalid_type",
                        "scenario.target.monster_level must be an integer",
                    )
                elif not low_level <= level <= high_level:
                    _scenario_error(
                        errors,
                        "value_out_of_bounds",
                        f"scenario.target.monster_level={level} must be between {low_level} and {high_level}",
                    )
            low_fire, high_fire = TARGET_FIRE_RESISTANCE_RANGE
            if "fire_resistance" in target:
                fire = target.get("fire_resistance")
                if isinstance(fire, bool) or not isinstance(fire, (int, float)):
                    _scenario_error(
                        errors,
                        "invalid_type",
                        "scenario.target.fire_resistance must be a number",
                    )
                elif not low_fire <= float(fire) <= high_fire:
                    _scenario_error(
                        errors,
                        "value_out_of_bounds",
                        f"scenario.target.fire_resistance={fire} must be between {low_fire} and {high_fire}",
                    )
            for flag in ("boss", "rare"):
                if flag in target and not isinstance(target.get(flag), bool):
                    _scenario_error(
                        errors,
                        "invalid_type",
                        f"scenario.target.{flag} must be a boolean",
                    )
            states = scenario.get("states")
            legacy_combined = states.get("target.boss_or_rare") if isinstance(states, dict) else None
            boss = target.get("boss")
            rare = target.get("rare")
            if isinstance(legacy_combined, bool) and (
                isinstance(boss, bool) or isinstance(rare, bool)
            ):
                combined = bool((boss if isinstance(boss, bool) else legacy_combined)
                                or (rare if isinstance(rare, bool) else legacy_combined))
                if combined != legacy_combined:
                    _scenario_warning(
                        warnings,
                        "target_state_mismatch",
                        "scenario.target.boss/rare 与 states.target.boss_or_rare 不一致；"
                        "重算以 target 的独立标志为准（Supermassive 使用 boss OR rare）。",
                    )

    # -- binding -----------------------------------------------------------
    expected_binding = {}
    if isinstance(catalog, dict):
        expected_binding = dict(catalog.get("binding") or {})
    binding = scenario.get("binding")
    if not isinstance(binding, dict):
        _scenario_error(errors, "missing_binding", "scenario.binding is required (build/registry/ruleset fingerprints)")
        binding = {}
    stale_fields: list[str] = []
    for key in BINDING_KEYS:
        expected = expected_binding.get(key)
        actual = binding.get(key)
        if expected is not None and actual != expected:
            stale_fields.append(key)
    for key in binding:
        if key not in BINDING_KEYS:
            _scenario_warning(warnings, "unknown_binding_field", f"scenario.binding.{key} is not a known binding field")
    if stale_fields:
        _scenario_error(
            errors,
            "stale_preset",
            "scenario binding does not match the current inputs "
            f"({', '.join(sorted(stale_fields))}); re-review the preset instead of applying it silently",
        )

    # -- states ------------------------------------------------------------
    states = scenario.get("states")
    if not isinstance(states, dict):
        _scenario_error(errors, "invalid_type", "scenario.states must be a JSON object")
        states = {}
    for state_id, value in states.items():
        key = str(state_id)
        state = registry.state(key)
        if state is None:
            _scenario_error(errors, "unknown_state", f"scenario.states.{key} is not a registered shared state", state_id=key)
            continue
        if state.get("source") == "auto":
            _scenario_error(
                errors,
                "auto_state_in_scenario",
                f"scenario.states.{key} is an auto-derived state and must not be written into the scenario",
                state_id=key,
            )
            continue
        kind = state.get("kind")
        if kind == "bool" and not _check_bool_value(value):
            _scenario_error(
                errors, "type_error", f"scenario.states.{key} must be a boolean", state_id=key
            )
        elif kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                _scenario_error(
                    errors, "type_error", f"scenario.states.{key} must be an integer", state_id=key
                )
            else:
                bounds = state.get("bounds") or {}
                low = bounds.get("min")
                high = bounds.get("max")
                if isinstance(low, int) and value < low:
                    _scenario_error(
                        errors,
                        "value_out_of_bounds",
                        f"scenario.states.{key}={value} is below the minimum {low}",
                        state_id=key,
                    )
                if isinstance(high, int) and value > high:
                    _scenario_error(
                        errors,
                        "value_out_of_bounds",
                        f"scenario.states.{key}={value} is above the maximum {high}",
                        state_id=key,
                    )

    # -- effects -----------------------------------------------------------
    effects = scenario.get("effects")
    if not isinstance(effects, dict):
        _scenario_error(errors, "invalid_type", "scenario.effects must be a JSON object")
        effects = {}
    qualification_by_effect: dict[str, dict[str, Any]] = {}
    if isinstance(catalog, dict):
        for entry in catalog.get("effects") or []:
            if isinstance(entry, dict) and entry.get("effect_id"):
                qualification_by_effect[str(entry["effect_id"])] = dict(entry.get("qualification") or {})
    for effect_id, config in effects.items():
        key = str(effect_id)
        effect = registry.effect(key)
        if effect is None:
            _scenario_error(errors, "unknown_effect", f"scenario.effects.{key} is not a registered effect", effect_id=key)
            continue
        qualification = qualification_by_effect.get(key)
        if not isinstance(config, dict):
            _scenario_error(errors, "invalid_type", f"scenario.effects.{key} must be a JSON object", effect_id=key)
            continue
        intent_selected = bool(config.get("enabled")) or (
            isinstance(config.get("stacks"), int)
            and not isinstance(config.get("stacks"), bool)
            and config.get("stacks") > 0
        )
        if qualification is not None and not qualification.get("available") and intent_selected:
            _scenario_error(
                errors,
                "not_available",
                f"scenario.effects.{key} 的来源在当前构筑中不可用"
                + (f"（{qualification.get('reason')}）" if qualification.get("reason") else ""),
                effect_id=key,
            )
        binding_info = registry.control_binding(effect)
        control_kind = binding_info.get("kind")
        if control_kind == "none":
            _scenario_error(
                errors,
                "enabled_without_control",
                f"scenario.effects.{key} has no user control (control.kind=none) and must not be enabled",
                effect_id=key,
            )
            continue
        for field in config:
            if field not in ("enabled", "stacks"):
                _scenario_warning(
                    warnings, "unknown_field", f"scenario.effects.{key}.{field} is not a known control field", effect_id=key
                )
        if "enabled" in config and not _check_bool_value(config.get("enabled")):
            _scenario_error(errors, "type_error", f"scenario.effects.{key}.enabled must be a boolean", effect_id=key)
        if "stacks" in config:
            stacks = config.get("stacks")
            if isinstance(stacks, bool) or not isinstance(stacks, int):
                _scenario_error(errors, "type_error", f"scenario.effects.{key}.stacks must be an integer", effect_id=key)
        if control_kind == "toggle" and "stacks" in config:
            _scenario_error(
                errors,
                "invalid_control_field",
                f"scenario.effects.{key} is a toggle effect; use 'enabled', not 'stacks'",
                effect_id=key,
            )
        if control_kind == "stacks":
            if "enabled" in config:
                _scenario_error(
                    errors,
                    "invalid_control_field",
                    f"scenario.effects.{key} is a stacks effect; use 'stacks', not 'enabled'",
                    effect_id=key,
                )
            stacks = config.get("stacks")
            if isinstance(stacks, int) and not isinstance(stacks, bool):
                bounds = binding_info.get("bounds") or {}
                low = bounds.get("min", 0)
                high = bounds.get("max", 0)
                if stacks < low or stacks > high:
                    _scenario_error(
                        errors,
                        "stacks_out_of_bounds",
                        f"scenario.effects.{key}.stacks={stacks} is outside [{low}, {high}]",
                        effect_id=key,
                    )
        if control_kind == "cast_state":
            _scenario_error(
                errors,
                "invalid_control_field",
                f"scenario.effects.{key} is a cast-state effect; use scenario.single_cast instead",
                effect_id=key,
            )
        support_level = str((effect.get("support") or {}).get("level") or "")
        activation_kind = str((effect.get("activation") or {}).get("kind") or "")
        enabled = bool(config.get("enabled") or config.get("stacks") or False)
        if enabled and (
            activation_kind == "action" or support_level == "needs_action_or_time_simulation"
        ):
            _scenario_warning(
                warnings,
                "unsupported_selected",
                f"scenario selects {key!r} but its mechanism needs an action/time simulation; "
                "the selection is recorded but not treated as engine-supported",
                effect_id=key,
            )

    # -- single_cast / loop -------------------------------------------------
    single_cast = scenario.get("single_cast")
    if not isinstance(single_cast, dict):
        _scenario_error(errors, "invalid_type", "scenario.single_cast must be a JSON object")
        single_cast = {}
    allowed_cast = set(_cast_state_keys(registry))
    for key, value in single_cast.items():
        if str(key) not in allowed_cast:
            _scenario_error(errors, "unknown_single_cast_key", f"scenario.single_cast.{key} is not a declared cast-state key")
        elif not _check_bool_value(value):
            _scenario_error(errors, "type_error", f"scenario.single_cast.{key} must be a boolean")
    loop = scenario.get("loop")
    if not isinstance(loop, dict):
        _scenario_error(errors, "invalid_type", "scenario.loop must be a JSON object")
        loop = {}
    for key, value in loop.items():
        if key not in LOOP_KEYS:
            _scenario_error(errors, "unknown_loop_key", f"scenario.loop.{key} is not a known loop field")
            continue
        if key == "crescendoing_rage_enabled" and not _check_bool_value(value):
            _scenario_error(errors, "type_error", f"scenario.loop.{key} must be a boolean")
        elif key == "initial_eligible_cast_count":
            if isinstance(value, bool) or not isinstance(value, int):
                _scenario_error(errors, "type_error", f"scenario.loop.{key} must be an integer")
            elif value < 0 or value > MAX_INITIAL_CAST_COUNT:
                _scenario_error(
                    errors,
                    "value_out_of_bounds",
                    f"scenario.loop.{key}={value} must be between 0 and {MAX_INITIAL_CAST_COUNT}",
                )
        elif key == "action_stream":
            if not isinstance(value, str):
                _scenario_error(errors, "type_error", f"scenario.loop.{key} must be a string")
            elif value not in ACTION_STREAMS:
                _scenario_error(
                    errors,
                    "invalid_loop_value",
                    f"scenario.loop.{key}={value!r} is not one of {list(ACTION_STREAMS)}",
                )
    return {"errors": errors, "warnings": warnings}


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------


def load_scenario(path: str | Path) -> dict[str, Any]:
    """Read a scenario JSON file (no validation; call validate_scenario next)."""
    resolved = Path(path)
    if not resolved.is_file():
        raise ScenarioError(
            [{"code": "scenario_missing", "message": f"scenario file not found: {resolved.name}"}]
        )
    try:
        data = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScenarioError(
            [{"code": "scenario_invalid_json", "message": f"scenario is not valid JSON: {exc}"}]
        ) from exc
    if not isinstance(data, dict):
        raise ScenarioError(
            [{"code": "scenario_invalid_type", "message": "scenario file must contain a JSON object"}]
        )
    return data


def save_scenario(scenario: dict[str, Any], path: str | Path) -> str:
    """Write a scenario as UTF-8, sorted-key JSON; returns the path string."""
    resolved = Path(path)
    if resolved.parent and not resolved.parent.exists():
        resolved.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(C.json_safe(scenario), ensure_ascii=False, indent=2, sort_keys=True)
    resolved.write_text(text + "\n", encoding="utf-8")
    return str(resolved)


def scenario_json_text(scenario: dict[str, Any]) -> str:
    """Stable JSON text used by export buttons/tests."""
    return json.dumps(C.json_safe(scenario), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


__all__ = [
    "BINDING_KEYS",
    "LOOP_DEFAULTS",
    "LOOP_KEYS",
    "SCENARIO_KEYS",
    "ScenarioError",
    "TARGET_DEFAULTS",
    "TARGET_FIRE_RESISTANCE_RANGE",
    "TARGET_KEYS",
    "TARGET_MONSTER_LEVEL_RANGE",
    "apply_preset",
    "binding_from_context",
    "default_scenario",
    "load_scenario",
    "normalize_scenario",
    "resolve_target",
    "save_scenario",
    "scenario_fingerprint",
    "scenario_json_text",
    "state_defaults",
    "target_from_context",
    "validate_scenario",
]
