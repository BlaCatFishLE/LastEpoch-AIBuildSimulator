"""Strict-ish loader and validator for the conditional-effects registry.

The registry (``tools/phase3/rules/conditional-effects-bh-stage1-v1.json``) is
authored by the registry-research workstream; this module is the single place
that reads it.  ``load_registry`` returns a :class:`Registry` with:

* the raw (validated) document and its file sha256,
* indexes for effects / shared states / presets / computation modules,
* the ``cast_state`` scenario-key binding collected from effect controls,
* ``resolve_actuals`` which turns declarative ``values`` into concrete numbers
  given the build's actual node points / stacks.

Contract reference: ``docs/contracts/conditional-interface.md`` section 5.
Validation is intentionally explicit about every enum/ref rule in section 5.1
while tolerating *additional* fields (the JSONC contract is not a closed
schema; research may add evidence/detail fields).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import constants as C

# ``.../le_calc/conditional/registry.py`` -> ``tools/phase3/rules/...``
DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parents[3] / "rules" / "conditional-effects-bh-stage1-v1.json"
)

#: repository root derived from this package (``.../tools/phase3/src/le_calc/conditional``)
PROJECT_ROOT = Path(__file__).resolve().parents[5]


class RegistryError(ValueError):
    """Raised when the registry file is missing / invalid.

    ``issues`` carries structured, readable validation errors of the shape
    ``{"code": str, "message": str, "path": str | None}``.
    """

    def __init__(self, issues: list[dict[str, Any]] | None = None, message: str | None = None):
        self.issues = list(issues or [])
        if message is None:
            if self.issues:
                first = self.issues[0]
                message = f"conditional-effects registry is invalid: {first.get('message')}"
            else:  # pragma: no cover - defensive
                message = "conditional-effects registry is invalid"
        super().__init__(message)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _error(errors: list[dict[str, Any]], code: str, path: str, message: str) -> None:
    errors.append({"code": code, "path": path, "message": message})


def _expect_dict(
    errors: list[dict[str, Any]], value: Any, path: str, *, code: str = "invalid_type"
) -> Optional[dict[str, Any]]:
    if isinstance(value, dict):
        return value
    _error(errors, code, path, f"expected a JSON object at {path}")
    return None


def _expect_list(
    errors: list[dict[str, Any]], value: Any, path: str, *, code: str = "invalid_type"
) -> Optional[list[Any]]:
    if isinstance(value, list):
        return value
    _error(errors, code, path, f"expected a JSON array at {path}")
    return None


def _require_str(
    errors: list[dict[str, Any]],
    payload: dict[str, Any],
    key: str,
    path: str,
    *,
    allow_empty: bool = False,
    allow_null: bool = False,
) -> Optional[str]:
    if key not in payload:
        _error(errors, "missing_field", f"{path}.{key}", f"missing required field {key!r} at {path}")
        return None
    value = payload.get(key)
    if value is None:
        if allow_null:
            return None
        _error(errors, "invalid_type", f"{path}.{key}", f"{path}.{key} must be a string")
        return None
    if not isinstance(value, str):
        _error(errors, "invalid_type", f"{path}.{key}", f"{path}.{key} must be a string")
        return None
    if not allow_empty and not value.strip():
        _error(errors, "empty_value", f"{path}.{key}", f"{path}.{key} must not be empty")
        return None
    return value


def _require_number(
    errors: list[dict[str, Any]],
    payload: dict[str, Any],
    key: str,
    path: str,
    *,
    allow_null: bool = False,
) -> Optional[float]:
    if key not in payload:
        _error(errors, "missing_field", f"{path}.{key}", f"missing required field {key!r} at {path}")
        return None
    value = payload.get(key)
    if value is None:
        if allow_null:
            return None
        _error(errors, "invalid_type", f"{path}.{key}", f"{path}.{key} must be a number")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _error(errors, "invalid_type", f"{path}.{key}", f"{path}.{key} must be a number")
        return None
    return float(value)


def _require_enum(
    errors: list[dict[str, Any]],
    payload: dict[str, Any],
    key: str,
    allowed: tuple[str, ...],
    path: str,
    *,
    allow_null: bool = False,
) -> Optional[str]:
    value = _require_str(errors, payload, key, path, allow_null=allow_null)
    if value is None:
        return None
    if value not in allowed:
        _error(
            errors,
            "invalid_enum",
            f"{path}.{key}",
            f"{path}.{key}={value!r} is not one of {list(allowed)}",
        )
        return None
    return value


def _optional_list(
    errors: list[dict[str, Any]], payload: dict[str, Any], key: str, path: str
) -> list[Any]:
    value = payload.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        _error(errors, "invalid_type", f"{path}.{key}", f"{path}.{key} must be a JSON array")
        return []
    return value


# ---------------------------------------------------------------------------
# validate_registry
# ---------------------------------------------------------------------------


def validate_registry(data: Any) -> list[dict[str, Any]]:
    """Validate one registry document; returns a list of readable issues.

    An empty list means the document is loadable.  This function never raises.
    """
    errors: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        _error(errors, "invalid_type", "$", "registry document must be a JSON object")
        return errors

    # -- top level ---------------------------------------------------------
    for key in (
        "schema",
        "registry_id",
        "game_version",
        "dataset_id",
        "created",
        "context",
        "computation_modules",
        "shared_states",
        "effects",
        "checked_records",
        "checked_scope_expectations",
        "presets",
    ):
        if key not in data:
            _error(errors, "missing_field", key, f"registry is missing required top-level key {key!r}")
    schema = data.get("schema")
    if schema is not None and schema not in C.REGISTRY_SCHEMAS:
        _error(
            errors,
            "schema_mismatch",
            "schema",
            f"registry.schema={schema!r} must equal {C.SCHEMA_REGISTRY!r}",
        )
    for key in ("registry_id", "game_version", "dataset_id", "created"):
        if key in data:
            _require_str(errors, data, key, "")

    # -- context -----------------------------------------------------------
    module_ids: set[str] = set()
    baseline_profile_ids: set[str] = set()
    mode_ids: set[str] = set()
    context = _expect_dict(errors, data.get("context"), "context") if "context" in data else None
    if context is not None:
        for key in ("main_skill_id", "main_skill_name", "ruleset_id", "disclaimer"):
            if key in context:
                _require_str(errors, context, key, "context")
        modes = context.get("modes")
        mode_list = _expect_list(errors, modes, "context.modes") if modes is not None else None
        if modes is None:
            _error(errors, "missing_field", "context.modes", "context.modes is required")
        for index, mode in enumerate(mode_list or []):
            path = f"context.modes[{index}]"
            if not isinstance(mode, dict):
                _error(errors, "invalid_type", path, f"{path} must be an object")
                continue
            mode_id = _require_str(errors, mode, "id", path)
            _require_str(errors, mode, "label", path)
            _require_str(errors, mode, "description", path)
            if mode_id:
                if mode_id in mode_ids:
                    _error(errors, "duplicate_id", path, f"duplicate mode id {mode_id!r}")
                mode_ids.add(mode_id)
        profiles = context.get("baseline_profiles")
        profile_list = (
            _expect_list(errors, profiles, "context.baseline_profiles") if profiles is not None else None
        )
        if profiles is None:
            _error(
                errors,
                "missing_field",
                "context.baseline_profiles",
                "context.baseline_profiles is required",
            )
        for index, profile in enumerate(profile_list or []):
            path = f"context.baseline_profiles[{index}]"
            if not isinstance(profile, dict):
                _error(errors, "invalid_type", path, f"{path} must be an object")
                continue
            profile_id = _require_str(errors, profile, "id", path)
            _require_str(errors, profile, "label", path)
            _require_str(errors, profile, "description", path)
            if profile_id:
                if profile_id in baseline_profile_ids:
                    _error(errors, "duplicate_id", path, f"duplicate baseline profile id {profile_id!r}")
                baseline_profile_ids.add(profile_id)
            if "default" in profile and not isinstance(profile.get("default"), bool):
                _error(errors, "invalid_type", f"{path}.default", f"{path}.default must be a boolean")

    # -- computation modules ----------------------------------------------
    modules = data.get("computation_modules")
    module_list = (
        _expect_list(errors, modules, "computation_modules") if modules is not None else None
    )
    for index, module in enumerate(module_list or []):
        path = f"computation_modules[{index}]"
        if not isinstance(module, dict):
            _error(errors, "invalid_type", path, f"{path} must be an object")
            continue
        module_id = _require_str(errors, module, "id", path)
        if module_id:
            if module_id not in C.COMPUTATION_MODULE_IDS:
                _error(
                    errors,
                    "invalid_enum",
                    f"{path}.id",
                    f"{path}.id={module_id!r} is not one of {list(C.COMPUTATION_MODULE_IDS)}",
                )
            if module_id in module_ids:
                _error(errors, "duplicate_id", path, f"duplicate computation module id {module_id!r}")
            module_ids.add(module_id)
        for key in ("label", "question", "notes"):
            if key in module:
                _require_str(errors, module, key, path, allow_empty=key == "notes")
        for key in ("consumes", "calc_refs", "rule_ids", "evidence"):
            _optional_list(errors, module, key, path)

    # -- shared states -----------------------------------------------------
    state_index: dict[str, dict[str, Any]] = {}
    states = data.get("shared_states")
    state_list = _expect_list(errors, states, "shared_states") if states is not None else None
    for index, state in enumerate(state_list or []):
        path = f"shared_states[{index}]"
        if not isinstance(state, dict):
            _error(errors, "invalid_type", path, f"{path} must be an object")
            continue
        state_id = _require_str(errors, state, "id", path)
        _require_str(errors, state, "label", path)
        kind = _require_enum(errors, state, "kind", C.STATE_KINDS, path)
        source = _require_enum(errors, state, "source", C.STATE_SOURCES, path)
        if "default" not in state:
            _error(errors, "missing_field", f"{path}.default", f"{path}.default is required")
        elif kind == "bool" and not isinstance(state.get("default"), bool):
            _error(errors, "invalid_type", f"{path}.default", f"{path}.default must be a boolean")
        elif kind == "int" and (isinstance(state.get("default"), bool) or not isinstance(state.get("default"), int)):
            _error(errors, "invalid_type", f"{path}.default", f"{path}.default must be an integer")
        bounds = state.get("bounds")
        if kind == "int":
            # engine-derived numeric states (source=="auto") may be unbounded:
            # e.g. auto.max_mana is a read-only snapshot-derived number.
            if not isinstance(bounds, dict) and not (
                source == "auto" and bounds is None
            ):
                _error(errors, "invalid_type", f"{path}.bounds", f"{path}.bounds must be an object for int states")
            elif isinstance(bounds, dict):
                low = bounds.get("min")
                high = bounds.get("max")
                if not isinstance(low, int) or isinstance(low, bool):
                    _error(errors, "invalid_type", f"{path}.bounds.min", f"{path}.bounds.min must be an integer")
                if not isinstance(high, int) or isinstance(high, bool):
                    _error(errors, "invalid_type", f"{path}.bounds.max", f"{path}.bounds.max must be an integer")
                if isinstance(low, int) and isinstance(high, int) and low > high:
                    _error(errors, "invalid_bounds", f"{path}.bounds", "bounds.min must be <= bounds.max")
                default = state.get("default")
                if isinstance(default, int) and isinstance(low, int) and isinstance(high, int):
                    if not (low <= default <= high):
                        _error(
                            errors,
                            "invalid_bounds",
                            f"{path}.default",
                            f"{path}.default={default} is outside bounds [{low}, {high}]",
                        )
        elif kind == "bool":
            if bounds is not None:
                _error(errors, "invalid_bounds", f"{path}.bounds", "bool states must use bounds=null")
        _optional_list(errors, state, "affects", path)
        if "description" in state:
            _require_str(errors, state, "description", path, allow_empty=True)
        if state_id:
            if state_id in state_index:
                _error(errors, "duplicate_id", path, f"duplicate shared state id {state_id!r}")
            else:
                state_index[state_id] = state

    # -- effects -----------------------------------------------------------
    effect_ids: set[str] = set()
    modifier_ids: set[str] = set()
    cast_state_keys: set[str] = set()
    effects = data.get("effects")
    effect_list = _expect_list(errors, effects, "effects") if effects is not None else None
    for index, effect in enumerate(effect_list or []):
        path = f"effects[{index}]"
        if not isinstance(effect, dict):
            _error(errors, "invalid_type", path, f"{path} must be an object")
            continue
        effect_id = _require_str(errors, effect, "effect_id", path)
        if effect_id:
            if effect_id in effect_ids:
                _error(errors, "duplicate_id", path, f"duplicate effect_id {effect_id!r}")
            effect_ids.add(effect_id)
        for key in ("group_id", "name", "name_en"):
            _require_str(errors, effect, key, path)
        _require_enum(errors, effect, "category", C.CATEGORIES, path)

        # source -----------------------------------------------------------
        source = _expect_dict(errors, effect.get("source"), f"{path}.source")
        if source is not None:
            _require_enum(errors, source, "kind", C.SOURCE_KINDS, f"{path}.source")
            _require_str(errors, source, "display", f"{path}.source")
            if "evidence" in source:
                _optional_list(errors, source, "evidence", f"{path}.source")
            for key in ("tree", "tree_entity_id", "node_name", "slot", "item_instance_id", "item_display_name", "locator"):
                if source.get(key) is not None and not isinstance(source.get(key), str):
                    _error(
                        errors,
                        "invalid_type",
                        f"{path}.source.{key}",
                        f"{path}.source.{key} must be a string or null",
                    )
            if source.get("node_id") is not None and (
                isinstance(source.get("node_id"), bool)
                or not isinstance(source.get("node_id"), (int, str))
            ):
                _error(
                    errors,
                    "invalid_type",
                    f"{path}.source.node_id",
                    f"{path}.source.node_id must be an integer, string or null",
                )

        # match ------------------------------------------------------------
        match = _expect_dict(errors, effect.get("match"), f"{path}.match")
        if match is not None:
            match_type = _require_enum(errors, match, "type", C.MATCH_TYPES, f"{path}.match")
            if match_type in ("skill_node", "passive_node"):
                if match.get("node_id") is None and not isinstance(match.get("node_id"), int):
                    _error(
                        errors,
                        "missing_field",
                        f"{path}.match.node_id",
                        f"{path}.match.node_id is required for {match_type} matches",
                    )
                if match.get("tree") is None and match.get("tree_entity_id") is None:
                    _error(
                        errors,
                        "missing_field",
                        f"{path}.match.tree",
                        f"{path}.match.tree (or tree_entity_id) is required for {match_type} matches",
                    )
            if match_type in ("equipment_mod", "mod_id"):
                if match.get("instance_id") is None and match.get("mod_id") is None and match.get("name_contains") is None:
                    _error(
                        errors,
                        "missing_field",
                        f"{path}.match",
                        f"{path}.match needs instance_id, mod_id or name_contains for {match_type} matches",
                    )
            if match.get("min_points") is not None:
                min_points = match.get("min_points")
                if isinstance(min_points, bool) or not isinstance(min_points, int) or min_points < 0:
                    _error(
                        errors,
                        "invalid_type",
                        f"{path}.match.min_points",
                        f"{path}.match.min_points must be a non-negative integer",
                    )

        # values -----------------------------------------------------------
        values = _expect_list(errors, effect.get("values"), f"{path}.values")
        value_keys: set[str] = set()
        for value_index, value in enumerate(values or []):
            value_path = f"{path}.values[{value_index}]"
            if not isinstance(value, dict):
                _error(errors, "invalid_type", value_path, f"{value_path} must be an object")
                continue
            value_key = _require_str(errors, value, "key", value_path)
            if value_key:
                if value_key in value_keys:
                    _error(errors, "duplicate_id", value_path, f"duplicate value key {value_key!r}")
                value_keys.add(value_key)
            _require_str(errors, value, "label", value_path)
            _require_str(errors, value, "unit", value_path)
            _require_str(errors, value, "display", value_path)
            resolvable = False
            for factor_key in ("per_point", "constant", "per_stack"):
                factor = value.get(factor_key)
                if factor is None:
                    continue
                if isinstance(factor, bool) or not isinstance(factor, (int, float)):
                    _error(
                        errors,
                        "invalid_type",
                        f"{value_path}.{factor_key}",
                        f"{value_path}.{factor_key} must be a number or null",
                    )
                else:
                    resolvable = True
            if (
                value.get("per_point") is None
                and value.get("constant") is None
                and value.get("per_stack") is None
            ):
                _error(
                    errors,
                    "no_value",
                    value_path,
                    f"{value_path} needs one of per_point / constant / per_stack to resolve a value",
                )

        # modifiers --------------------------------------------------------
        modifiers = _expect_list(errors, effect.get("modifiers"), f"{path}.modifiers")
        for modifier_index, modifier in enumerate(modifiers or []):
            modifier_path = f"{path}.modifiers[{modifier_index}]"
            if not isinstance(modifier, dict):
                _error(errors, "invalid_type", modifier_path, f"{modifier_path} must be an object")
                continue
            modifier_id = _require_str(errors, modifier, "modifier_id", modifier_path)
            if modifier_id:
                if modifier_id in modifier_ids:
                    _error(errors, "duplicate_id", modifier_path, f"duplicate modifier_id {modifier_id!r}")
                modifier_ids.add(modifier_id)
            for key in ("stat_key", "stat_label", "operation", "unit", "engine_sink"):
                _require_str(errors, modifier, key, modifier_path)
            if "target" not in modifier:
                _error(errors, "missing_field", f"{modifier_path}.target", f"{modifier_path}.target is required (may be null)")
            elif modifier.get("target") is not None and not isinstance(modifier.get("target"), str):
                _error(errors, "invalid_type", f"{modifier_path}.target", f"{modifier_path}.target must be a string or null")
            _optional_list(errors, modifier, "damage_tags", modifier_path)
            _optional_list(errors, modifier, "branches", modifier_path)
            _optional_list(errors, modifier, "dedupe_contribution_ids", modifier_path)
            value_ref = _require_str(errors, modifier, "value_ref", modifier_path)
            if value_ref and value_keys and value_ref not in value_keys:
                _error(
                    errors,
                    "unknown_value_ref",
                    f"{modifier_path}.value_ref",
                    f"{modifier_path}.value_ref={value_ref!r} does not exist in {path}.values",
                )
            branches = modifier.get("branches")
            if isinstance(branches, list):
                for branch in branches:
                    if branch not in module_ids:
                        _error(
                            errors,
                            "unknown_branch",
                            f"{modifier_path}.branches",
                            f"{modifier_path}.branches contains unknown computation module id {branch!r}",
                        )

        # activation -------------------------------------------------------
        activation = _expect_dict(errors, effect.get("activation"), f"{path}.activation")
        if activation is not None:
            _require_enum(errors, activation, "kind", C.ACTIVATION_KINDS, f"{path}.activation")
            if activation.get("duration_s") is not None:
                _require_number(errors, activation, "duration_s", f"{path}.activation", allow_null=True)
            if activation.get("max_stacks") is not None:
                max_stacks = activation.get("max_stacks")
                if isinstance(max_stacks, bool) or not isinstance(max_stacks, int):
                    _error(
                        errors,
                        "invalid_type",
                        f"{path}.activation.max_stacks",
                        f"{path}.activation.max_stacks must be an integer or null",
                    )
            if "refresh" in activation and activation.get("refresh") is not None:
                if activation.get("refresh") not in ("recast_refreshes", "no_refresh", "unknown"):
                    _error(
                        errors,
                        "invalid_enum",
                        f"{path}.activation.refresh",
                        f"{path}.activation.refresh must be recast_refreshes|no_refresh|unknown",
                    )

        # control ----------------------------------------------------------
        control = _expect_dict(errors, effect.get("control"), f"{path}.control")
        control_kind = None
        if control is not None:
            control_kind = _require_enum(errors, control, "kind", C.CONTROL_KINDS, f"{path}.control")
            _require_str(errors, control, "label", f"{path}.control")
            if control_kind == "toggle":
                state_id = _require_str(errors, control, "state_id", f"{path}.control")
                if state_id:
                    ref = state_index.get(state_id)
                    if ref is None:
                        _error(
                            errors,
                            "unknown_state",
                            f"{path}.control.state_id",
                            f"{path}.control.state_id={state_id!r} is not declared in shared_states",
                        )
                    elif ref.get("source") != "user":
                        _error(
                            errors,
                            "control_state_not_user",
                            f"{path}.control.state_id",
                            f"{path}.control.state_id={state_id!r} must reference a user state",
                        )
            elif control_kind == "stacks":
                stack_state_id = _require_str(errors, control, "stack_state_id", f"{path}.control")
                if stack_state_id:
                    ref = state_index.get(stack_state_id)
                    if ref is None:
                        _error(
                            errors,
                            "unknown_state",
                            f"{path}.control.stack_state_id",
                            f"{path}.control.stack_state_id={stack_state_id!r} is not declared in shared_states",
                        )
                    elif ref.get("source") != "user":
                        _error(
                            errors,
                            "control_state_not_user",
                            f"{path}.control.stack_state_id",
                            f"{path}.control.stack_state_id={stack_state_id!r} must reference a user state",
                        )
                    elif ref.get("kind") != "int":
                        _error(
                            errors,
                            "control_state_kind",
                            f"{path}.control.stack_state_id",
                            f"{path}.control.stack_state_id={stack_state_id!r} must be an int state",
                        )
                if control.get("default") is not None:
                    default_stacks = control.get("default")
                    if isinstance(default_stacks, bool) or not isinstance(default_stacks, int):
                        _error(
                            errors,
                            "invalid_type",
                            f"{path}.control.default",
                            f"{path}.control.default must be an integer",
                        )
            elif control_kind == "cast_state":
                state_id = control.get("state_id")
                if state_id is not None and not isinstance(state_id, str):
                    _error(
                        errors,
                        "invalid_type",
                        f"{path}.control.state_id",
                        f"{path}.control.state_id must be a string or null",
                    )
                elif isinstance(state_id, str) and state_id:
                    ref = state_index.get(state_id)
                    if ref is not None and ref.get("source") != "user":
                        _error(
                            errors,
                            "control_state_not_user",
                            f"{path}.control.state_id",
                            f"{path}.control.state_id={state_id!r} must reference a user state",
                        )
                key = _cast_state_key(control, effect_id or "")
                if key:
                    cast_state_keys.add(key)

        # conditions -------------------------------------------------------
        conditions = _expect_dict(errors, effect.get("conditions"), f"{path}.conditions")
        if conditions is not None:
            for group in ("all", "any"):
                entries = conditions.get(group)
                if entries is None:
                    continue
                if not isinstance(entries, list):
                    _error(
                        errors,
                        "invalid_type",
                        f"{path}.conditions.{group}",
                        f"{path}.conditions.{group} must be a JSON array",
                    )
                    continue
                for entry_index, entry in enumerate(entries):
                    entry_path = f"{path}.conditions.{group}[{entry_index}]"
                    if not isinstance(entry, dict):
                        _error(errors, "invalid_type", entry_path, f"{entry_path} must be an object")
                        continue
                    state_id = _require_str(errors, entry, "state", entry_path)
                    if state_id and state_id not in state_index:
                        _error(
                            errors,
                            "unknown_state",
                            f"{entry_path}.state",
                            f"{entry_path}.state={state_id!r} is not declared in shared_states",
                        )
                    if "equals" not in entry:
                        _error(
                            errors,
                            "missing_field",
                            f"{entry_path}.equals",
                            f"{entry_path}.equals is required (declarative condition)",
                        )

        # relevance / support / research -----------------------------------
        relevance = _expect_dict(errors, effect.get("relevance"), f"{path}.relevance")
        if relevance is not None:
            _require_enum(errors, relevance, "status", C.RELEVANCE_STATUSES, f"{path}.relevance")
            _require_str(errors, relevance, "path", f"{path}.relevance")
            _require_enum(errors, relevance, "baseline_relation", C.BASELINE_RELATIONS, f"{path}.relevance")
            _optional_list(errors, relevance, "branches", f"{path}.relevance")
            _optional_list(errors, relevance, "scenario_prerequisites", f"{path}.relevance")
        support = _expect_dict(errors, effect.get("support"), f"{path}.support")
        if support is not None:
            _require_enum(errors, support, "level", C.SUPPORT_LEVELS, f"{path}.support")
            _optional_list(errors, support, "open_questions", f"{path}.support")
            for key in ("engine_gap", "next_probe"):
                if support.get(key) is not None and not isinstance(support.get(key), str):
                    _error(
                        errors,
                        "invalid_type",
                        f"{path}.support.{key}",
                        f"{path}.support.{key} must be a string or null",
                    )
        research = _expect_dict(errors, effect.get("research"), f"{path}.research")
        if research is not None:
            _require_enum(errors, research, "status", C.RESEARCH_STATUSES, f"{path}.research")
            _optional_list(errors, research, "open_questions", f"{path}.research")
            if research.get("confidence_note") is not None and not isinstance(
                research.get("confidence_note"), str
            ):
                _error(
                    errors,
                    "invalid_type",
                    f"{path}.research.confidence_note",
                    f"{path}.research.confidence_note must be a string or null",
                )

        effect_evidence = effect.get("evidence")
        if not isinstance(effect_evidence, list) or not effect_evidence:
            _error(
                errors,
                "missing_evidence",
                f"{path}.evidence",
                f"{path}.evidence must be a non-empty JSON array (contract section 5.1)",
            )

    # -- checked records ---------------------------------------------------
    checked_records = data.get("checked_records")
    records_list = (
        _expect_list(errors, checked_records, "checked_records") if checked_records is not None else None
    )
    record_ids: set[str] = set()
    for index, record in enumerate(records_list or []):
        path = f"checked_records[{index}]"
        if not isinstance(record, dict):
            _error(errors, "invalid_type", path, f"{path} must be an object")
            continue
        record_id = _require_str(errors, record, "record_id", path)
        if record_id:
            if record_id in record_ids:
                _error(errors, "duplicate_id", path, f"duplicate record_id {record_id!r}")
            record_ids.add(record_id)
        _require_enum(errors, record, "category", C.CHECKED_CATEGORIES, path)
        _require_str(errors, record, "source_label", path)
        _require_str(errors, record, "effect_summary", path, allow_empty=True)
        _require_enum(errors, record, "disposition", C.CHECKED_DISPOSITIONS, path)
        _require_str(errors, record, "reason", path, allow_empty=True)
        related = record.get("related_effect_id")
        if related is not None:
            if not isinstance(related, str):
                _error(errors, "invalid_type", f"{path}.related_effect_id", "related_effect_id must be a string or null")
            elif related not in effect_ids:
                _error(
                    errors,
                    "unknown_effect_ref",
                    f"{path}.related_effect_id",
                    f"{path}.related_effect_id={related!r} is not a registered effect_id",
                )
        if "evidence" in record:
            _optional_list(errors, record, "evidence", path)

    # -- checked scope expectations ---------------------------------------
    scope = _expect_dict(
        errors, data.get("checked_scope_expectations"), "checked_scope_expectations"
    )
    if scope is not None:
        for key in C.CHECKED_SCOPE_KEYS:
            if key not in scope:
                _error(
                    errors,
                    "missing_field",
                    f"checked_scope_expectations.{key}",
                    f"checked_scope_expectations.{key} is required",
                )
                continue
            value = scope.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                _error(
                    errors,
                    "invalid_type",
                    f"checked_scope_expectations.{key}",
                    f"checked_scope_expectations.{key} must be a non-negative integer",
                )
        for key, value in scope.items():
            if key in C.CHECKED_SCOPE_KEYS:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                _error(
                    errors,
                    "invalid_type",
                    f"checked_scope_expectations.{key}",
                    f"checked_scope_expectations.{key} must be a non-negative integer",
                )

    # -- presets -----------------------------------------------------------
    presets = data.get("presets")
    preset_list = _expect_list(errors, presets, "presets") if presets is not None else None
    preset_ids: set[str] = set()
    allowed_cast_keys = cast_state_keys | {"crescendoing_rage_applies", "preparation_ready"}
    for index, preset in enumerate(preset_list or []):
        path = f"presets[{index}]"
        if not isinstance(preset, dict):
            _error(errors, "invalid_type", path, f"{path} must be an object")
            continue
        preset_id = _require_str(errors, preset, "preset_id", path)
        if preset_id:
            if preset_id in preset_ids:
                _error(errors, "duplicate_id", path, f"duplicate preset_id {preset_id!r}")
            preset_ids.add(preset_id)
        for key in ("label", "description"):
            _require_str(errors, preset, key, path)
        states = _expect_dict(errors, preset.get("states"), f"{path}.states")
        for state_id, state_value in (states or {}).items():
            state_path = f"{path}.states.{state_id}"
            ref = state_index.get(state_id)
            if ref is None:
                _error(errors, "unknown_state", state_path, f"{state_path} references an unknown shared state")
            elif ref.get("kind") == "bool" and not isinstance(state_value, bool):
                _error(errors, "invalid_type", state_path, f"{state_path} must be a boolean")
            elif ref.get("kind") == "int" and (
                isinstance(state_value, bool) or not isinstance(state_value, int)
            ):
                _error(errors, "invalid_type", state_path, f"{state_path} must be an integer")
        effect_values = _expect_dict(errors, preset.get("effects"), f"{path}.effects")
        for effect_id, effect_value in (effect_values or {}).items():
            effect_path = f"{path}.effects.{effect_id}"
            if effect_id not in effect_ids:
                _error(errors, "unknown_effect", effect_path, f"{effect_path} references an unknown effect")
                continue
            if not isinstance(effect_value, dict):
                _error(errors, "invalid_type", effect_path, f"{effect_path} must be an object")
                continue
            if "enabled" in effect_value and not isinstance(effect_value.get("enabled"), bool):
                _error(errors, "invalid_type", f"{effect_path}.enabled", f"{effect_path}.enabled must be a boolean")
            if "stacks" in effect_value and (
                isinstance(effect_value.get("stacks"), bool)
                or not isinstance(effect_value.get("stacks"), int)
            ):
                _error(errors, "invalid_type", f"{effect_path}.stacks", f"{effect_path}.stacks must be an integer")
        single_cast = _expect_dict(errors, preset.get("single_cast"), f"{path}.single_cast")
        for cast_key, cast_value in (single_cast or {}).items():
            cast_path = f"{path}.single_cast.{cast_key}"
            if cast_key not in allowed_cast_keys:
                _error(
                    errors,
                    "unknown_single_cast_key",
                    cast_path,
                    f"{cast_path} is not a declared cast_state key",
                )
            if not isinstance(cast_value, bool):
                _error(errors, "invalid_type", cast_path, f"{cast_path} must be a boolean")
        loop = _expect_dict(errors, preset.get("loop"), f"{path}.loop")
        for loop_key, loop_value in (loop or {}).items():
            loop_path = f"{path}.loop.{loop_key}"
            if loop_key in ("crescendoing_rage_enabled",):
                if not isinstance(loop_value, bool):
                    _error(errors, "invalid_type", loop_path, f"{loop_path} must be a boolean")
            elif loop_key in ("initial_eligible_cast_count",):
                if isinstance(loop_value, bool) or not isinstance(loop_value, int):
                    _error(errors, "invalid_type", loop_path, f"{loop_path} must be an integer")
            elif loop_key in ("action_stream",):
                if not isinstance(loop_value, str):
                    _error(errors, "invalid_type", loop_path, f"{loop_path} must be a string")

    return errors


# ---------------------------------------------------------------------------
# Registry object
# ---------------------------------------------------------------------------


def _cast_state_key(control: dict[str, Any], effect_id: str) -> str:
    """Resolve the scenario ``single_cast`` key for a cast_state control."""
    for key in ("cast_state_id", "single_cast_key", "single_cast_state"):
        value = control.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    state_id = control.get("state_id")
    if isinstance(state_id, str) and state_id.strip():
        cleaned = state_id.strip()
        if cleaned.startswith("single_cast."):
            return cleaned[len("single_cast.") :]
        if cleaned.startswith("cast_state."):
            return cleaned[len("cast_state.") :]
        return cleaned
    if effect_id:
        tail = effect_id.rsplit(".", 1)[-1]
        return tail
    return ""


@dataclass
class Registry:
    """Loaded + validated conditional-effects registry."""

    data: dict[str, Any]
    path: Optional[str] = None
    sha256: Optional[str] = None
    effect_index: dict[str, dict[str, Any]] = field(default_factory=dict)
    shared_state_index: dict[str, dict[str, Any]] = field(default_factory=dict)
    preset_index: dict[str, dict[str, Any]] = field(default_factory=dict)
    module_ids: tuple[str, ...] = ()
    cast_state_keys: tuple[str, ...] = ()

    # -- accessors ---------------------------------------------------------
    @property
    def registry_id(self) -> str:
        return str(self.data.get("registry_id", C.REGISTRY_ID_DEFAULT))

    @property
    def schema(self) -> str:
        return str(self.data.get("schema", ""))

    @property
    def game_version(self) -> str:
        return str(self.data.get("game_version", C.GAME_VERSION_DEFAULT))

    @property
    def dataset_id(self) -> str:
        return str(self.data.get("dataset_id", C.DATASET_ID_DEFAULT))

    @property
    def context(self) -> dict[str, Any]:
        return self.data.get("context") or {}

    @property
    def effects(self) -> list[dict[str, Any]]:
        return list(self.data.get("effects") or [])

    @property
    def shared_states(self) -> list[dict[str, Any]]:
        return list(self.data.get("shared_states") or [])

    @property
    def presets(self) -> list[dict[str, Any]]:
        return list(self.data.get("presets") or [])

    @property
    def checked_records(self) -> list[dict[str, Any]]:
        return list(self.data.get("checked_records") or [])

    @property
    def checked_scope_expectations(self) -> dict[str, Any]:
        return dict(self.data.get("checked_scope_expectations") or {})

    @property
    def computation_modules(self) -> list[dict[str, Any]]:
        return list(self.data.get("computation_modules") or [])

    @property
    def main_skill(self) -> dict[str, Any]:
        context = self.context
        return {
            "id": str(context.get("main_skill_id", "")),
            "name": str(context.get("main_skill_name", "")),
            "ruleset_id": str(context.get("ruleset_id", "")),
        }

    @property
    def modes(self) -> list[dict[str, Any]]:
        return list(self.context.get("modes") or [])

    @property
    def baseline_profiles(self) -> list[dict[str, Any]]:
        return list(self.context.get("baseline_profiles") or [])

    @property
    def disclaimer(self) -> str:
        return str(self.context.get("disclaimer", ""))

    def mode(self, mode_id: str | None = None) -> dict[str, Any]:
        modes = self.modes
        if not modes:
            return {"id": C.DEFAULT_MODE_ID, "label": "固定条件估算", "description": ""}
        if mode_id:
            for mode in modes:
                if mode.get("id") == mode_id:
                    return mode
        return modes[0]

    def baseline_profile(self, profile_id: str | None = None) -> dict[str, Any]:
        profiles = self.baseline_profiles
        if not profiles:
            return {
                "id": C.DEFAULT_BASELINE_PROFILE_ID,
                "label": "构筑基础状态",
                "description": "",
            }
        if profile_id:
            for profile in profiles:
                if profile.get("id") == profile_id:
                    return profile
        for profile in profiles:
            if profile.get("default"):
                return profile
        return profiles[0]

    def effect(self, effect_id: str) -> Optional[dict[str, Any]]:
        return self.effect_index.get(effect_id)

    def state(self, state_id: str) -> Optional[dict[str, Any]]:
        return self.shared_state_index.get(state_id)

    def preset(self, preset_id: str) -> Optional[dict[str, Any]]:
        return self.preset_index.get(preset_id)

    def default_preset(self) -> Optional[dict[str, Any]]:
        for preset in self.presets:
            if preset.get("preset_id") == C.DEFAULT_BASELINE_PROFILE_ID:
                return preset
        return self.presets[0] if self.presets else None

    def user_states(self) -> list[dict[str, Any]]:
        return [state for state in self.shared_states if state.get("source") == "user"]

    def auto_states(self) -> list[dict[str, Any]]:
        return [state for state in self.shared_states if state.get("source") == "auto"]

    def control_binding(self, effect: dict[str, Any]) -> dict[str, Any]:
        """Resolved control binding used by scenario validation/evaluation/view."""
        control = effect.get("control") or {}
        kind = str(control.get("kind") or "none")
        binding: dict[str, Any] = {"kind": kind}
        if kind == "toggle":
            binding["state_id"] = control.get("state_id")
        elif kind == "stacks":
            state_id = control.get("stack_state_id")
            binding["stack_state_id"] = state_id
            state = self.state(state_id) if state_id else None
            bounds = (state or {}).get("bounds")
            if isinstance(bounds, dict):
                binding["bounds"] = {
                    "min": int(bounds.get("min", 0)),
                    "max": int(bounds.get("max", 0)),
                }
            default = control.get("default")
            binding["default"] = int(default) if isinstance(default, int) else int((state or {}).get("default") or 0)
        elif kind == "cast_state":
            binding["cast_state_id"] = _cast_state_key(control, str(effect.get("effect_id") or ""))
        return binding


def _build_registry(data: dict[str, Any], *, path: Optional[str], sha256: Optional[str]) -> Registry:
    effects = list(data.get("effects") or [])
    effect_index = {
        str(effect.get("effect_id")): effect
        for effect in effects
        if isinstance(effect, dict) and effect.get("effect_id")
    }
    state_index = {
        str(state.get("id")): state
        for state in (data.get("shared_states") or [])
        if isinstance(state, dict) and state.get("id")
    }
    preset_index = {
        str(preset.get("preset_id")): preset
        for preset in (data.get("presets") or [])
        if isinstance(preset, dict) and preset.get("preset_id")
    }
    module_ids = tuple(
        str(module.get("id"))
        for module in (data.get("computation_modules") or [])
        if isinstance(module, dict) and module.get("id")
    )
    cast_keys: list[str] = []
    for effect in effects:
        if not isinstance(effect, dict):
            continue
        control = effect.get("control") or {}
        if control.get("kind") != "cast_state":
            continue
        key = _cast_state_key(control, str(effect.get("effect_id") or ""))
        if key and key not in cast_keys:
            cast_keys.append(key)
    return Registry(
        data=data,
        path=path,
        sha256=sha256,
        effect_index=effect_index,
        shared_state_index=state_index,
        preset_index=preset_index,
        module_ids=module_ids,
        cast_state_keys=tuple(cast_keys),
    )


def load_registry(path: str | Path | None = None) -> Registry:
    """Load and validate the registry JSON; raises :class:`RegistryError`."""
    resolved = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    if not resolved.is_file():
        raise RegistryError(
            [
                {
                    "code": "registry_missing",
                    "path": None,
                    "message": f"conditional-effects registry not found: {resolved.name}",
                }
            ]
        )
    raw = resolved.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryError(
            [
                {
                    "code": "registry_invalid_json",
                    "path": None,
                    "message": f"conditional-effects registry is not valid JSON: {exc}",
                }
            ]
        ) from exc
    errors = validate_registry(data)
    if errors:
        raise RegistryError(errors)
    return _build_registry(
        data,
        path=str(resolved),
        sha256=C.sha256_text(raw.decode("utf-8-sig")),
    )


# ---------------------------------------------------------------------------
# Actual value resolution (per_point x points / constant / per_stack x stacks)
# ---------------------------------------------------------------------------


def resolve_actuals(
    effect: dict[str, Any],
    *,
    points: Optional[int] = None,
    stacks: Optional[int] = None,
) -> dict[str, Any]:
    """Resolve one effect's declarative values into concrete build numbers.

    Returns ``{"values": {key: entry, ...}, "unresolved": [keys...]}`` where an
    entry is ``{"key", "label", "value", "unit", "display", "display_actual",
    "basis", "per_point", "constant", "per_stack", "points", "stacks"}``.
    ``value`` is ``None`` when the registry value needs points/stacks that the
    build source could not provide (the discovery layer turns that into a data
    gap instead of inventing a zero).
    """
    values: dict[str, Any] = {}
    unresolved: list[str] = []
    for value in effect.get("values") or []:
        if not isinstance(value, dict):
            continue
        key = str(value.get("key") or "")
        if not key:
            continue
        unit = str(value.get("unit") or "")
        per_point = value.get("per_point")
        constant = value.get("constant")
        per_stack = value.get("per_stack")
        actual: Optional[float] = None
        basis = ""
        if per_point is not None:
            if points is None:
                unresolved.append(key)
            else:
                actual = float(per_point) * float(points)
                basis = f"每点 {C.format_actual(per_point, unit)} × {points} 点"
        elif constant is not None:
            actual = float(constant)
            basis = "固定值"
        elif per_stack is not None:
            if stacks is None:
                unresolved.append(key)
            else:
                actual = float(per_stack) * float(stacks)
                basis = f"每层 {C.format_actual(per_stack, unit)} × {stacks} 层"
        else:
            unresolved.append(key)
        entry = {
            "key": key,
            "label": str(value.get("label") or key),
            "value": actual,
            "unit": unit,
            "display": str(value.get("display") or ""),
            "display_actual": C.format_actual(actual, unit),
            "basis": basis,
            "per_point": per_point,
            "constant": constant,
            "per_stack": per_stack,
            "points": points,
            "stacks": stacks,
        }
        values[key] = entry
    return {"values": values, "unresolved": unresolved}


def value_actual(
    effect: dict[str, Any],
    value_key: str,
    *,
    points: Optional[int] = None,
    stacks: Optional[int] = None,
) -> Optional[float]:
    resolved = resolve_actuals(effect, points=points, stacks=stacks)["values"].get(value_key)
    if resolved is None:
        return None
    return resolved.get("value")


__all__ = [
    "DEFAULT_REGISTRY_PATH",
    "PROJECT_ROOT",
    "Registry",
    "RegistryError",
    "load_registry",
    "resolve_actuals",
    "validate_registry",
    "value_actual",
]
