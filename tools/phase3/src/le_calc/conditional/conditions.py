"""Client-side evaluation rules payload (contract §8.5).

The page must be able to recompute "selected / condition / support" statuses
offline without a second damage engine.  This module turns the registry +
catalog into a *declarative* payload (no functions, no expressions): shared
states, auto states with comparison results, per-effect control binding and
conditions, status labels, presets and the resolved baseline scenario.

The status precedence itself is declared explicitly (``status_precedence`` +
``status_rules``) so the page JS and the Python evaluator share one contract.
"""

from __future__ import annotations

from typing import Any, Optional

from . import constants as C
from .discover import effect_sort_key
from .registry import Registry
from .scenario import apply_preset, default_scenario, scenario_fingerprint


def _status_when_met(effect: dict[str, Any], registry: Registry) -> str:
    """Status returned when the user selects this effect and conditions hold."""
    support = str((effect.get("support") or {}).get("level") or "mechanism_unresolved")
    relevance = effect.get("relevance") or {}
    baseline_relation = str(relevance.get("baseline_relation") or "new")
    activation = effect.get("activation") or {}
    control_kind = registry.control_binding(effect).get("kind")
    needs_action = activation.get("kind") == "action" or support == "needs_action_or_time_simulation"
    if needs_action and control_kind not in ("toggle", "stacks", "cast_state"):
        return "unsupported_action"
    if baseline_relation == "already_in_baseline":
        return "already_in_baseline"
    if control_kind == "none":
        return "auto_active"
    if support == "engine_supported":
        return "active_supported"
    if support == "needs_rule_mapping":
        return "selected_ready_needs_rules"
    if support == "needs_action_or_time_simulation":
        return "selected_ready_needs_time"
    return "selected_unconfirmed"


def _control_payload(effect: dict[str, Any], registry: Registry) -> dict[str, Any]:
    binding = registry.control_binding(effect)
    control = effect.get("control") or {}
    kind = str(binding.get("kind") or "none")
    if kind == "toggle":
        return {
            "kind": "toggle",
            "state_id": binding.get("state_id"),
            "label": str(control.get("label") or ""),
        }
    if kind == "stacks":
        return {
            "kind": "stacks",
            "stack_state_id": binding.get("stack_state_id"),
            "bounds": binding.get("bounds"),
            "default": binding.get("default", 0),
            "label": str(control.get("label") or ""),
        }
    if kind == "cast_state":
        return {
            "kind": "cast_state",
            "cast_state_id": binding.get("cast_state_id"),
            "state_id": control.get("state_id"),
            "label": str(control.get("label") or ""),
        }
    return {"kind": "none", "label": str(control.get("label") or "")}


def _condition_payload(effect: dict[str, Any], registry: Registry) -> dict[str, Any]:
    conditions = effect.get("conditions") or {}

    def entries(group: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for entry in conditions.get(group) or []:
            if not isinstance(entry, dict):
                continue
            out.append(
                {
                    "state": str(entry.get("state") or ""),
                    "equals": entry.get("equals"),
                    "label": str((registry.state(str(entry.get("state") or "")) or {}).get("label") or ""),
                }
            )
        return out

    return {
        "all": entries("all"),
        "any": entries("any"),
        "note": str(conditions.get("note") or ""),
    }


def build_client_rules(
    registry: Registry,
    catalog: dict[str, Any],
    scenario: Optional[dict[str, Any]] = None,
    evaluation: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build the ``rules`` payload embedded into ``result["conditional"]``."""
    catalog = catalog if isinstance(catalog, dict) else {}
    binding = catalog.get("binding") or {}
    if not isinstance(scenario, dict):
        scenario = default_scenario(registry, {"binding": binding})
    entry_by_effect: dict[str, dict[str, Any]] = {}
    for entry in catalog.get("effects") or []:
        if isinstance(entry, dict) and entry.get("effect_id"):
            entry_by_effect[str(entry["effect_id"])] = entry

    states: list[dict[str, Any]] = []
    for state in registry.user_states():
        states.append(
            {
                "id": str(state.get("id") or ""),
                "label": str(state.get("label") or ""),
                "kind": str(state.get("kind") or "bool"),
                "source": "user",
                "default": state.get("default"),
                "bounds": state.get("bounds"),
                "description": str(state.get("description") or ""),
            }
        )
    auto_states: list[dict[str, Any]] = []
    for entry in catalog.get("auto_states") or []:
        if not isinstance(entry, dict):
            continue
        auto_states.append(
            {
                "id": str(entry.get("id") or ""),
                "label": str(entry.get("label") or ""),
                "kind": "number" if entry.get("kind") == "number" else "bool",
                "value": entry.get("value"),
                "display": entry.get("display"),
                "comparisons": [
                    {
                        "state": comparison.get("state"),
                        "label": comparison.get("label"),
                        "op": comparison.get("op"),
                        "ref": comparison.get("ref"),
                        "threshold": comparison.get("threshold"),
                        "met": comparison.get("met"),
                    }
                    for comparison in entry.get("comparisons") or []
                    if isinstance(comparison, dict)
                ],
            }
        )

    effects: list[dict[str, Any]] = []
    for effect in sorted(registry.effects, key=effect_sort_key):
        if not isinstance(effect, dict):
            continue
        effect_id = str(effect.get("effect_id") or "")
        entry = entry_by_effect.get(effect_id) or {}
        evaluation_record = None
        if isinstance(evaluation, dict):
            for record in evaluation.get("effects") or []:
                if isinstance(record, dict) and record.get("effect_id") == effect_id:
                    evaluation_record = record
                    break
        support = str((effect.get("support") or {}).get("level") or "mechanism_unresolved")
        relevance = effect.get("relevance") or {}
        baseline_relation = str(relevance.get("baseline_relation") or "new")
        qualification = entry.get("qualification") or {}
        effects.append(
            {
                "effect_id": effect_id,
                "name": str(effect.get("name") or ""),
                "category": str(entry.get("category") or effect.get("category") or ""),
                "group_id": str(effect.get("group_id") or ""),
                "control": _control_payload(effect, registry),
                "condition": _condition_payload(effect, registry),
                "support": support,
                "baseline_relation": baseline_relation,
                "status_when_met": _status_when_met(effect, registry),
                "already_in_baseline": baseline_relation == "already_in_baseline",
                "available": bool(qualification.get("available")),
                "qualification": {
                    "available": bool(qualification.get("available")),
                    "points": qualification.get("points"),
                    "max_points": qualification.get("max_points"),
                    "reason": qualification.get("reason"),
                },
                "summary": str(entry.get("summary") or ""),
                "initial_status": (evaluation_record or {}).get("status"),
            }
        )

    presets: list[dict[str, Any]] = []
    for preset in registry.presets:
        if not isinstance(preset, dict):
            continue
        preset_id = str(preset.get("preset_id") or "")
        try:
            resolved = apply_preset(registry, preset_id, {"binding": binding})
        except Exception:  # noqa: BLE001 - never fail the payload for one preset
            continue
        presets.append(
            {
                "preset_id": preset_id,
                "label": str(preset.get("label") or ""),
                "description": str(preset.get("description") or ""),
                "is_default": bool(registry.default_preset() and registry.default_preset().get("preset_id") == preset_id),
                "mode": resolved.get("mode"),
                "baseline_profile": resolved.get("baseline_profile"),
                "states": resolved.get("states"),
                "effects": resolved.get("effects"),
                "single_cast": resolved.get("single_cast"),
                "loop": resolved.get("loop"),
            }
        )

    baseline_scenario = catalog.get("baseline_scenario")
    if not isinstance(baseline_scenario, dict):
        baseline_scenario = default_scenario(registry, {"binding": binding})
    mode = registry.mode()
    return {
        "schema": C.SCHEMA_RULES,
        "mode": {
            "id": str(mode.get("id") or C.DEFAULT_MODE_ID),
            "label": str(mode.get("label") or ""),
            "description": str(mode.get("description") or ""),
        },
        "baseline_profile": {
            "id": str(registry.baseline_profile().get("id") or C.DEFAULT_BASELINE_PROFILE_ID),
            "label": str(registry.baseline_profile().get("label") or ""),
        },
        "states": states,
        "auto_states": auto_states,
        "baseline_states": list(catalog.get("baseline_states") or []),
        "effects": effects,
        "status_labels": dict(C.STATUS_LABELS),
        "status_precedence": list(C.STATUS_ORDER),
        "status_rules": {
            "already_in_baseline_overrides_auto_and_not_selected": True,
            "unsupported_action": {
                "activation_kind": "action",
                "support": "needs_action_or_time_simulation",
                "controllable_kinds": ["toggle", "stacks", "cast_state"],
            },
            "unknown_state_makes_auto_unknown": True,
            "condition_source": "scenario.states + auto_states values",
            "cast_state_source": "scenario.single_cast",
            "stacks_source": "scenario.effects[effect_id].stacks",
        },
        "presets": presets,
        "baseline_scenario": baseline_scenario,
        "baseline_fingerprint": scenario_fingerprint(baseline_scenario),
    }


__all__ = ["build_client_rules"]
