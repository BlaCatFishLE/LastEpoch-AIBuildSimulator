"""Buff/conditional-effect screening for the Flame Reave hit model.

Owner: fr-engine (task-2).  Consumers: ``hit_report``/``cli`` (buff screening
artifact + HTML conditional page).

Turns ``ruleset.effects`` + the snapshot into one flat, display-ready candidate
list.  Every candidate answers:

    effect id / label / source_kind / source_path / condition
    condition_status (satisfied | not_satisfied | unknown | always)
    coverage → status (modeled | not_modeled | unresolved)
    included_in_baseline / applied (under the scenario states)
    modifiers (when modelled), evidence locators, next_probe

Unique-tooltip description lines from the frozen calculation input that no
effect maps are appended as ``unresolved`` candidates, so no candidate source is
silently dropped.

Entry point::

    screen_buffs(snapshot, ruleset, scenario=None) -> dict
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from . import model
from .hit_damage import (
    _effect_condition_satisfied,
    resolve_states,
    resolve_target,
)

SCHEMA_VERSION = "le-hit-buff-screening-v1"

COVERAGE_STATUS = {"modeled": "modeled", "not_modeled": "not_modeled", "unresolved": "unresolved"}


def _condition_status(satisfied: Optional[bool]) -> str:
    if satisfied is None:
        return "unknown"
    return "satisfied" if satisfied else "not_satisfied"


def _state_rows(states: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in sorted(states):
        value = states[key]
        if isinstance(value, bool):
            active = value
        elif isinstance(value, (int, float)):
            active = float(value) > 0
        else:
            active = bool(value)
        rows.append({"state": key, "value": value, "active": active})
    return rows


def _baseline_states(
    ruleset: Any,
    scenario: Optional[dict[str, Any]],
    snapshot: Optional[model.Snapshot] = None,
) -> dict[str, Any]:
    """Baseline states: ruleset defaults + snapshot facts + scenario baseline profile."""
    profile = None
    if isinstance(scenario, dict):
        profile = scenario.get("baseline_profile")
    profile_scenario = {"baseline_profile": profile} if profile else {}
    return resolve_states(ruleset, profile_scenario, section=None, snapshot=snapshot)


def _candidate_from_effect(
    effect: dict[str, Any],
    *,
    baseline_states: dict[str, Any],
    scenario_states: dict[str, Any],
) -> dict[str, Any]:
    coverage = str(effect.get("coverage") or "unresolved")
    status = COVERAGE_STATUS.get(coverage, "unresolved")
    baseline_satisfied = _effect_condition_satisfied(effect, baseline_states)
    scenario_satisfied = _effect_condition_satisfied(effect, scenario_states)
    include_in = list(effect.get("include_in") or [])
    modeled = coverage == "modeled"
    included_in_baseline = bool(
        modeled and baseline_satisfied is True and ("baseline" in include_in or "build_base" in include_in)
    )
    applied = bool(modeled and scenario_satisfied is True)
    return {
        "buff_id": str(effect.get("id")),
        "effect_id": str(effect.get("id")),
        "label": effect.get("label"),
        "source_kind": effect.get("source_kind"),
        "source_path": effect.get("source_path"),
        "condition": effect.get("condition"),
        "condition_status": _condition_status(scenario_satisfied),
        "baseline_condition_status": _condition_status(baseline_satisfied),
        "status": status,
        "coverage": coverage,
        "parameter_status": effect.get("status"),
        "modeled": modeled,
        "included_in_baseline": included_in_baseline,
        "applied": applied,
        "include_in": include_in,
        "modifiers": list(effect.get("modifiers") or []),
        "stacks_state": effect.get("stacks_state"),
        "max_stacks": effect.get("max_stacks"),
        "evidence": list(effect.get("evidence") or []),
        "next_probe": effect.get("next_probe"),
        "notes": effect.get("notes"),
        "assumption": effect.get("assumption"),
        "unique_text": effect.get("unique_text"),
    }


def _unmapped_unique_candidates(
    snapshot: model.Snapshot,
    mapped_texts: set[str],
) -> list[dict[str, Any]]:
    derived = snapshot.derived or {}
    out: list[dict[str, Any]] = []
    for number, description in enumerate(derived.get("unique_descriptions") or []):
        text = str(description.get("text") or "")
        if not text or text in mapped_texts:
            continue
        out.append(
            {
                "buff_id": f"unique-description:{number:02d}",
                "effect_id": f"unique-description:{number:02d}",
                "label": text,
                "source_kind": "unique_tooltip",
                "source_path": description.get("source_path") or description.get("mod_id"),
                "condition": None,
                "condition_status": "always",
                "baseline_condition_status": "always",
                "status": "unresolved",
                "coverage": "unresolved",
                "parameter_status": model.STATUS_UNRESOLVED,
                "modeled": False,
                "included_in_baseline": False,
                "applied": False,
                "include_in": [],
                "modifiers": [],
                "evidence": [
                    {
                        "kind": "catalog",
                        "detail": "calculation-input-v1 unique description line",
                        "locator": str(description.get("mod_id") or ""),
                    }
                ],
                "next_probe": (
                    "classify this unique-tooltip line: does it modify Flame Reave hit "
                    "damage, and with which numeric parameters?"
                ),
                "notes": "informational unique-tooltip line; not consumed until classified",
                "unique_text": text,
            }
        )
    return out


def screen_buffs(
    snapshot: model.Snapshot,
    ruleset: Any,
    scenario: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Screen every conditional effect declared by the ruleset + snapshot."""
    ruleset_data = getattr(ruleset, "data", ruleset) or {}
    effects = list((ruleset_data.get("effects") or []) if isinstance(ruleset_data, dict) else [])
    baseline_states = _baseline_states(ruleset, scenario)
    scenario_states = resolve_states(ruleset, scenario, section="simulate")
    target = resolve_target(ruleset, scenario)

    candidates: list[dict[str, Any]] = []
    mapped_texts: set[str] = set()
    for effect in effects:
        candidate = _candidate_from_effect(
            effect,
            baseline_states=baseline_states,
            scenario_states=scenario_states,
        )
        unique_text = effect.get("unique_text")
        if unique_text:
            mapped_texts.add(str(unique_text))
        candidates.append(candidate)
    candidates.extend(_unmapped_unique_candidates(snapshot, mapped_texts))

    summary = {
        "total": len(candidates),
        "modeled": sum(1 for candidate in candidates if candidate["status"] == "modeled"),
        "not_modeled": sum(1 for candidate in candidates if candidate["status"] == "not_modeled"),
        "unresolved": sum(1 for candidate in candidates if candidate["status"] == "unresolved"),
        "included_in_baseline": sum(
            1 for candidate in candidates if candidate["included_in_baseline"]
        ),
        "applied": sum(1 for candidate in candidates if candidate["applied"]),
        "by_source_kind": _counts(candidate["source_kind"] for candidate in candidates),
    }

    return {
        "schema": SCHEMA_VERSION,
        "baseline_profile": (scenario or {}).get("baseline_profile") if isinstance(scenario, dict) else None,
        "baseline_states": baseline_states,
        "scenario_states": scenario_states,
        "applied_states": _state_rows(scenario_states),
        "target": target,
        "candidates": candidates,
        "summary": summary,
        "assumptions": [
            "buff screening is a static classification of declared conditional effects; "
            "it does not simulate uptime, cooldowns or resource generation",
            "included_in_baseline means: coverage=modeled AND the effect is active under the "
            "baseline profile states (build_base opens enchant_weapon_active)",
        ],
    }


def _counts(values: Iterable[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


__all__ = ["COVERAGE_STATUS", "SCHEMA_VERSION", "screen_buffs"]
