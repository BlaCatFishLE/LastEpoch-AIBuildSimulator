"""Scenario evaluation: selection/condition/support statuses and consumer payload.

Contract references: ``docs/contracts/conditional-interface.md`` sections 8.3,
8.4 (status precedence shared with the page JS) and 8.5-adjacent view fields.

The engine never recomputes damage here.  Evaluation answers three separate
questions for every registered effect:

1. **source qualification** — does the current build actually own the source?
2. **user selection** — did the scenario select it (toggle / stacks / cast state)?
3. **actual activation** — are the declared conditions satisfied?
4. **engine support** — can a later re-calculation consume a proposed
   contribution, and if not, which rule/action mapping is missing?

``proposed_contributions`` are documentation/interface only: nothing in this
module mutates the snapshot or any damage number.
"""

from __future__ import annotations

from typing import Any, Optional

from . import constants as C
from .discover import effect_sort_key
from .registry import Registry, resolve_actuals
from .scenario import default_scenario, normalize_scenario, scenario_fingerprint, validate_scenario

#: statuses whose selected + satisfied values would feed a later re-calculation
PROPOSAL_STATUSES = (
    "selected_ready_needs_rules",
    "selected_ready_needs_time",
    "selected_unconfirmed",
    "active_supported",
    "auto_active",
)


# ---------------------------------------------------------------------------
# State / condition helpers
# ---------------------------------------------------------------------------


def _user_state_values(scenario: dict[str, Any], registry: Registry) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for state in registry.user_states():
        state_id = str(state.get("id") or "")
        if state_id:
            values[state_id] = state.get("default")
    states = scenario.get("states") if isinstance(scenario.get("states"), dict) else {}
    for state_id, value in states.items():
        state = registry.state(str(state_id))
        if state is not None and state.get("source") == "user":
            values[str(state_id)] = value
    return values


def _state_label(registry: Registry, state_id: str) -> str:
    state = registry.state(state_id)
    if state is not None:
        return str(state.get("label") or state_id)
    return state_id


def condition_results(
    effect: dict[str, Any],
    registry: Registry,
    user_values: dict[str, Any],
    auto_values: dict[str, Any],
) -> list[dict[str, Any]]:
    """Evaluate every declared condition into a readable result row."""
    conditions = effect.get("conditions") or {}
    rows: list[dict[str, Any]] = []
    for group in ("all", "any"):
        for entry in conditions.get(group) or []:
            if not isinstance(entry, dict):
                continue
            state_id = str(entry.get("state") or "")
            if state_id in auto_values:
                actual = auto_values.get(state_id)
                source = "auto"
            elif state_id in user_values:
                actual = user_values.get(state_id)
                source = "user"
            else:
                actual = None
                source = "unknown"
            expected = entry.get("equals")
            if actual is None:
                met: Optional[bool] = None
            elif isinstance(expected, bool):
                met = bool(actual) == expected
            else:
                try:
                    met = float(actual) == float(expected)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    met = actual == expected
            rows.append(
                {
                    "kind": "state",
                    "group": group,
                    "state_id": state_id,
                    "label": _state_label(registry, state_id)
                    + (" = " + str(expected) if not isinstance(expected, bool) else ""),
                    "expected": expected,
                    "actual": actual,
                    "met": met,
                    "source": source,
                }
            )
    return rows


def _overall_met(rows: list[dict[str, Any]], conditions: dict[str, Any]) -> tuple[bool, bool]:
    """Return ``(all_met, has_unknown)`` honouring all/any semantics."""
    all_rows = [row for row in rows if row.get("group") == "all"]
    any_rows = [row for row in rows if row.get("group") == "any"]
    has_unknown = any(row.get("met") is None for row in rows)
    all_ok = all(row.get("met") is True for row in all_rows) if all_rows else True
    if any_rows:
        any_ok = any(row.get("met") is True for row in any_rows)
    else:
        any_ok = True
    if any_rows:
        all_ok = all_ok and any_ok
    return bool(all_ok), has_unknown


# ---------------------------------------------------------------------------
# Effect evaluation
# ---------------------------------------------------------------------------


def _selection(
    effect: dict[str, Any], scenario: dict[str, Any], registry: Registry
) -> tuple[Optional[bool], Optional[int]]:
    binding = registry.control_binding(effect)
    kind = binding.get("kind")
    effect_id = str(effect.get("effect_id") or "")
    config: dict[str, Any] = {}
    effects = scenario.get("effects")
    if isinstance(effects, dict) and isinstance(effects.get(effect_id), dict):
        config = dict(effects.get(effect_id) or {})
    if kind == "toggle":
        return bool(config.get("enabled", False)), None
    if kind == "stacks":
        stacks = config.get("stacks")
        if isinstance(stacks, bool) or not isinstance(stacks, int):
            stacks = int(binding.get("default") or 0)
        return stacks > 0, stacks
    if kind == "cast_state":
        key = str(binding.get("cast_state_id") or "")
        single_cast = scenario.get("single_cast")
        value = bool(single_cast.get(key, False)) if isinstance(single_cast, dict) else False
        return value, None
    return None, None


def _status_for(
    effect: dict[str, Any],
    registry: Registry,
    *,
    qualification: dict[str, Any],
    selected: Optional[bool],
    all_met: bool,
    has_unknown: bool,
    condition_rows: list[dict[str, Any]],
) -> str:
    """Implementation of contract §8.4 status precedence (first match wins)."""
    if not qualification.get("available"):
        return "not_available"
    activation = effect.get("activation") or {}
    support = str((effect.get("support") or {}).get("level") or "mechanism_unresolved")
    relevance = effect.get("relevance") or {}
    baseline_relation = str(relevance.get("baseline_relation") or "new")
    control_kind = registry.control_binding(effect).get("kind")
    needs_action = activation.get("kind") == "action" or support == "needs_action_or_time_simulation"
    if needs_action and control_kind not in ("toggle", "stacks", "cast_state"):
        return "unsupported_action"
    # rule 10: already_in_baseline wins over auto_* / not_selected
    if baseline_relation == "already_in_baseline":
        return "already_in_baseline"
    if control_kind == "none":
        if has_unknown:
            return "auto_unknown"
        return "auto_active" if all_met else "auto_inactive"
    if not selected:
        return "not_selected"
    if not all_met:
        return "selected_condition_unmet"
    if support == "engine_supported":
        return "active_supported"
    if support == "needs_rule_mapping":
        return "selected_ready_needs_rules"
    if support == "needs_action_or_time_simulation":
        return "selected_ready_needs_time"
    return "selected_unconfirmed"


def _unmet_labels(condition_rows: list[dict[str, Any]]) -> str:
    labels = [str(row.get("label")) for row in condition_rows if row.get("met") is not True]
    return "、".join(labels) if labels else "（条件未知）"


def _baseline_split_note(effect: dict[str, Any], condition_rows: list[dict[str, Any]]) -> str:
    """Honest wording for baseline-included effects whose *increment* is conditional.

    Registry research models effects such as the idol mana threshold as
    ``already_in_baseline`` for the base roll and a separate ``doubling_extra``
    value for the conditional increment.  Contract §8.4 keeps the
    ``already_in_baseline`` status; this helper makes the reason explain that
    only the base part is counted.
    """
    activation = effect.get("activation") or {}
    if str(activation.get("kind")) != "numeric_threshold":
        return ""
    unmet = any(row.get("met") is not True for row in condition_rows)
    if unmet:
        text = "基础值已计入，翻倍部分未计入。"
    else:
        text = "基础值已计入；门槛条件当前满足，翻倍/增量部分需单独复核。"
    notes = str((effect.get("relevance") or {}).get("notes") or "").strip()
    if notes and any(token in notes for token in ("基础", "翻倍", "增量")):
        text += "注册表注记：" + notes
    return text


def _status_reason(
    effect: dict[str, Any],
    registry: Registry,
    *,
    status: str,
    qualification: dict[str, Any],
    condition_rows: list[dict[str, Any]],
    selected: Optional[bool],
    control_kind: str,
) -> str:
    if status == "not_available":
        return str(qualification.get("reason") or "当前构筑缺少该效果的来源（未装备/未分配/点数不足）。")
    if status == "unsupported_action":
        return "动作机制未支持：固定条件模式不模拟动作序列 / 时间模拟。"
    if status == "already_in_baseline":
        met_labels = [str(row.get("label")) for row in condition_rows if row.get("met") is True]
        unmet_labels = [str(row.get("label")) for row in condition_rows if row.get("met") is not True]
        if met_labels and not unmet_labels:
            detail = "条件当前已满足：" + "；".join(met_labels) + "。"
        elif unmet_labels:
            detail = "条件当前未满足：" + "；".join(unmet_labels) + "。"
        else:
            detail = "无条件判定项。"
        split = _baseline_split_note(effect, condition_rows)
        if split:
            detail += split
        return "该效果已计入基础来源账，本页不重复生成拟议贡献。" + detail
    if status == "auto_active":
        support = str((effect.get("support") or {}).get("level") or "mechanism_unresolved")
        relevance_status = str((effect.get("relevance") or {}).get("status") or "")
        baseline_relation = str((effect.get("relevance") or {}).get("baseline_relation") or "new")
        prefix = (
            "当前场景不相关；"
            if relevance_status == "not_relevant_current_scenario"
            else ""
        )
        if support == "engine_supported" or baseline_relation == "already_in_baseline":
            return prefix + "自动判定：生效条件已满足，且已计入基础来源账。"
        return (
            prefix
            + "构筑常驻自动提供 → 已具备资格，但现有引擎未接入该输入通道；"
            "未计入本报告 DPS；已生成拟议贡献。"
        )
    if status == "auto_inactive":
        relevance_status = str((effect.get("relevance") or {}).get("status") or "")
        support = str((effect.get("support") or {}).get("level") or "mechanism_unresolved")
        prefix = (
            "当前场景不相关；"
            if relevance_status == "not_relevant_current_scenario"
            else ""
        )
        unmet = _unmet_labels(condition_rows)
        if support == "mechanism_unresolved":
            return (
                prefix
                + f"构筑自动提供，但机制未确认，且当前条件未满足（{unmet}）；"
                "当前未计入本报告 DPS。"
            )
        return prefix + f"自动判定：未满足（{unmet}）。"
    if status == "auto_unknown":
        relevance_status = str((effect.get("relevance") or {}).get("status") or "")
        prefix = (
            "当前场景不相关；"
            if relevance_status == "not_relevant_current_scenario"
            else ""
        )
        return prefix + f"自动判定：包含无法从当前输入确认的状态（{_unmet_labels(condition_rows)}）。"
    if status == "not_selected":
        if control_kind == "stacks":
            return "层数为 0（未选择）；固定条件模式只声明状态，不模拟获取与维持。"
        if control_kind == "cast_state":
            return "单次施法状态未选择。"
        return "toggle 关闭（未选择）；固定条件模式只声明状态，不模拟获取与维持过程。"
    if status == "selected_condition_unmet":
        return f"已选择，但条件未满足：{_unmet_labels(condition_rows)}。"
    if status == "selected_ready_needs_rules":
        return "条件已满足；现有引擎缺少该 Buff/条件的输入通道，已生成拟议贡献供后续重算。"
    if status == "selected_ready_needs_time":
        return "条件已满足；需要动作/时间模拟才能直接消费该效果。"
    if status == "selected_unconfirmed":
        return "条件已满足；机制尚未确认，仅生成拟议贡献供复核。"
    if status == "active_supported":
        return "条件已满足，且现有引擎可直接消费该贡献。"
    return ""


def _trim_evidence(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            out.append(
                {
                    "kind": str(item.get("kind") or "derivation"),
                    "detail": C.trim_text(item.get("detail"), 300),
                    "locator": C.trim_text(item.get("locator"), 300) or None,
                    "snippet": C.trim_text(item.get("snippet"), 300) or None,
                }
            )
    return out


def _modifier_results(
    effect: dict[str, Any],
    actuals: dict[str, Any],
    *,
    active: bool,
) -> list[dict[str, Any]]:
    if not active:
        return []
    results: list[dict[str, Any]] = []
    for modifier in effect.get("modifiers") or []:
        if not isinstance(modifier, dict):
            continue
        value_ref = str(modifier.get("value_ref") or "")
        value_entry = actuals.get(value_ref) or {}
        value = value_entry.get("value")
        results.append(
            {
                "modifier_id": str(modifier.get("modifier_id") or ""),
                "branches": list(modifier.get("branches") or []),
                "stat_key": str(modifier.get("stat_key") or ""),
                "stat_label": str(modifier.get("stat_label") or ""),
                "operation": str(modifier.get("operation") or ""),
                "value": value,
                "unit": str(modifier.get("unit") or value_entry.get("unit") or ""),
                "target": modifier.get("target"),
                "damage_tags": list(modifier.get("damage_tags") or []),
                "value_ref": value_ref,
                "value_text": C.format_actual(value, modifier.get("unit") or value_entry.get("unit")),
                "engine_sink": str(modifier.get("engine_sink") or ""),
                "dedupe_contribution_ids": list(modifier.get("dedupe_contribution_ids") or []),
                "note": str(modifier.get("note") or ""),
            }
        )
    return results


def _condition_summary(effect: dict[str, Any], registry: Registry) -> str:
    conditions = effect.get("conditions") or {}
    pieces: list[str] = []
    for group in ("all", "any"):
        for entry in conditions.get(group) or []:
            if isinstance(entry, dict):
                state_id = str(entry.get("state") or "")
                expected = entry.get("equals")
                text = _state_label(registry, state_id)
                if isinstance(expected, bool):
                    text = text if expected else f"非 {text}"
                pieces.append(text)
    return " AND ".join(pieces)


def evaluate_effect(
    effect: dict[str, Any],
    entry: Optional[dict[str, Any]],
    scenario: dict[str, Any],
    registry: Registry,
    auto_values: dict[str, Any],
) -> dict[str, Any]:
    effect_id = str(effect.get("effect_id") or "")
    qualification = dict((entry or {}).get("qualification") or {})
    if entry is None:
        qualification = {"available": True, "points": None, "max_points": None, "reason": None}
    user_values = _user_state_values(scenario, registry)
    rows = condition_results(effect, registry, user_values, auto_values)
    all_met, has_unknown = _overall_met(rows, effect.get("conditions") or {})
    binding = registry.control_binding(effect)
    control_kind = str(binding.get("kind") or "none")
    selected, stacks = _selection(effect, scenario, registry)
    status = _status_for(
        effect,
        registry,
        qualification=qualification,
        selected=selected,
        all_met=all_met,
        has_unknown=has_unknown,
        condition_rows=rows,
    )
    support = str((effect.get("support") or {}).get("level") or "mechanism_unresolved")
    relevance = effect.get("relevance") or {}
    baseline_relation = str(relevance.get("baseline_relation") or "new")
    already_in_baseline = baseline_relation == "already_in_baseline"
    active = status in PROPOSAL_STATUSES or (
        status == "already_in_baseline" and already_in_baseline
    ) or (status == "auto_active")
    actuals = resolve_actuals(
        effect,
        points=qualification.get("points"),
        stacks=stacks,
    )["values"]
    modifier_results = _modifier_results(effect, actuals, active=active)
    auto_parts: list[dict[str, Any]] = []
    if status in ("auto_active", "already_in_baseline") and modifier_results:
        for result in modifier_results:
            auto_parts.append(
                {
                    "label": f"{result['stat_label']} {result['value_text']}".strip(),
                    "value": result["value"],
                    "unit": result["unit"],
                    "condition": _condition_summary(effect, registry),
                }
            )
    blocked = None
    if status in (
        "unsupported_action",
        "selected_ready_needs_rules",
        "selected_ready_needs_time",
        "selected_unconfirmed",
        "auto_unknown",
    ):
        blocked = {
            "support": support,
            "reason": str((effect.get("support") or {}).get("engine_gap") or ""),
            "next_probe": str((effect.get("support") or {}).get("next_probe") or ""),
            "open_questions": list((effect.get("support") or {}).get("open_questions") or []),
        }
    record = {
        "effect_id": effect_id,
        "group_id": str(effect.get("group_id") or ""),
        "name": str(effect.get("name") or ""),
        "category": str((entry or {}).get("category") or effect.get("category") or ""),
        "selected": selected,
        "active": active and status != "already_in_baseline",
        "status": status,
        "status_reason": _status_reason(
            effect,
            registry,
            status=status,
            qualification=qualification,
            condition_rows=rows,
            selected=selected,
            control_kind=control_kind,
        ),
        "qualification": qualification,
        "control": binding,
        "stacks": stacks,
        "condition_results": rows,
        "conditions_met": all_met and not has_unknown,
        "modifier_results": modifier_results,
        "auto_parts": auto_parts,
        "blocked": blocked,
        "already_in_baseline": already_in_baseline,
        "support": support,
        "baseline_relation": baseline_relation,
        "relevance_status": str(relevance.get("status") or ""),
        "existing_handling": list((entry or {}).get("existing_handling") or []),
        "source_id": (entry or {}).get("source_id"),
        "summary": str((entry or {}).get("summary") or ""),
        "value_lines": list((entry or {}).get("value_lines") or []),
    }
    return record


def _proposals(evaluation_records: list[dict[str, Any]], registry: Registry) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    for record in evaluation_records:
        if record.get("status") not in PROPOSAL_STATUSES:
            continue
        if record.get("already_in_baseline"):
            continue
        # Guard against future double counting: an auto-active effect that the
        # engine already consumes must not also be proposed as a new
        # contribution (baseline_relation covers the already-counted case;
        # this covers auto effects marked engine_supported with relation "new").
        if (
            record.get("status") == "auto_active"
            and record.get("support") == "engine_supported"
            and not record.get("already_in_baseline")
        ):
            continue
        effect = registry.effect(str(record.get("effect_id") or ""))
        if effect is None:
            continue
        condition = _condition_summary(effect, registry)
        for result in record.get("modifier_results") or []:
            modifier = None
            for candidate in effect.get("modifiers") or []:
                if isinstance(candidate, dict) and candidate.get("modifier_id") == result.get("modifier_id"):
                    modifier = candidate
                    break
            if modifier is None:
                continue
            proposals.append(
                {
                    "proposal_id": f"prop:{result.get('modifier_id')}",
                    "effect_id": str(effect.get("effect_id") or ""),
                    "modifier_id": str(result.get("modifier_id") or ""),
                    "stat_key": result.get("stat_key"),
                    "stat_name": result.get("stat_label"),
                    "operation": result.get("operation"),
                    "value": result.get("value"),
                    "unit": result.get("unit"),
                    "target": result.get("target"),
                    "damage_tags": list(result.get("damage_tags") or []),
                    "branches": list(result.get("branches") or []),
                    "condition": condition,
                    "engine_sink": result.get("engine_sink"),
                    "requires": str(record.get("support") or ""),
                    "dedupe_contribution_ids": list(result.get("dedupe_contribution_ids") or []),
                    "status": "proposed",
                    "effect_status": record.get("status"),
                    "evidence": _trim_evidence(effect.get("evidence")),
                }
            )
    proposals.sort(key=lambda item: str(item.get("proposal_id") or ""))
    return proposals


def _unsupported(evaluation_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in evaluation_records:
        if record.get("status") not in ("unsupported_action", "selected_ready_needs_time"):
            continue
        rows.append(
            {
                "effect_id": record.get("effect_id"),
                "support": record.get("support"),
                "status": record.get("status"),
                "reason": record.get("status_reason"),
            }
        )
    rows.sort(key=lambda item: str(item.get("effect_id") or ""))
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _summary(
    catalog: dict[str, Any],
    evaluation_records: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    unsupported: list[dict[str, Any]],
) -> dict[str, Any]:
    counts = dict((catalog.get("checked_scope") or {}).get("counts") or {})
    candidates = list(catalog.get("candidates") or [])
    relevance = {status: 0 for status in C.RELEVANCE_STATUSES}
    for candidate in candidates:
        status = str(candidate.get("relevance_status") or "")
        if status in relevance:
            relevance[status] += 1
    denominator_breakdown = {
        "items": int(counts.get("items") or 0),
        "item_mods": int(counts.get("item_mods") or 0),
        "specializations": int(counts.get("specializations") or 0),
        "skill_nodes_allocated": int(counts.get("skill_nodes_allocated") or 0),
        "passive_nodes": int(counts.get("passive_nodes") or 0),
        "snapshot_contributions": int(counts.get("snapshot_contributions") or 0),
        "excluded_sources": int(counts.get("excluded_sources") or 0),
    }
    checked_objects = sum(denominator_breakdown.values())
    status_counts: dict[str, int] = {status: 0 for status in C.STATUS_ORDER}
    for record in evaluation_records:
        status = str(record.get("status") or "")
        if status in status_counts:
            status_counts[status] += 1
    support_counts = {level: 0 for level in C.SUPPORT_LEVELS}
    for record in evaluation_records:
        support = str(record.get("support") or "")
        if support in support_counts:
            support_counts[support] += 1
    configured = sum(
        1
        for record in evaluation_records
        if record.get("selected") is True and (record.get("qualification") or {}).get("available")
    )
    condition_met = sum(
        1
        for record in evaluation_records
        if record.get("selected") is True and record.get("conditions_met") is True
    )
    condition_unmet = sum(
        1
        for record in evaluation_records
        if record.get("selected") is True and record.get("conditions_met") is not True
    )
    not_selected = sum(
        1
        for record in evaluation_records
        if record.get("status") == "not_selected"
    )
    return {
        "checked_objects": checked_objects,
        "denominator_breakdown": denominator_breakdown,
        "candidates": len(candidates),
        "registered": len(catalog.get("effects") or []),
        "relevant": relevance.get("relevant", 0),
        "scenario_dependent": relevance.get("scenario_dependent", 0),
        "unconfirmed": relevance.get("unconfirmed", 0),
        "not_relevant": relevance.get("not_relevant_current_scenario", 0),
        "configured": configured,
        "condition_met": condition_met,
        "condition_unmet": condition_unmet,
        "not_selected": not_selected,
        "auto_active": status_counts.get("auto_active", 0),
        "auto_inactive": status_counts.get("auto_inactive", 0),
        "auto_unknown": status_counts.get("auto_unknown", 0),
        "already_in_baseline": status_counts.get("already_in_baseline", 0),
        "engine_supported": support_counts.get("engine_supported", 0),
        "needs_rule_mapping": support_counts.get("needs_rule_mapping", 0),
        "needs_time": support_counts.get("needs_action_or_time_simulation", 0),
        "mechanism_unresolved": support_counts.get("mechanism_unresolved", 0),
        "unsupported": len(unsupported),
        "proposed_contributions": len(proposals),
        "not_available": status_counts.get("not_available", 0),
        "engine_supported_active": status_counts.get("active_supported", 0),
    }


def evaluate_scenario(
    scenario: Optional[dict[str, Any]],
    registry: Registry,
    catalog: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate one scenario against the registry + discovery catalog.

    Returns the ``evaluation`` block from contract §8.1/§8.3.  Never raises for
    a malformed scenario: structural problems end up in ``validation.errors``.
    """
    catalog = catalog if isinstance(catalog, dict) else {}
    raw_scenario: Any = scenario if isinstance(scenario, dict) else {}
    target_context = catalog.get("target_context")
    scenario_context = {
        "binding": catalog.get("binding") or {},
        "target": target_context,
    }
    resolved = normalize_scenario(raw_scenario, registry, scenario_context)
    validation = validate_scenario(raw_scenario if isinstance(raw_scenario, dict) else resolved, registry, catalog)
    auto_values = dict(catalog.get("auto_values") or {})
    entries: dict[str, dict[str, Any]] = {}
    for entry in catalog.get("effects") or []:
        if isinstance(entry, dict) and entry.get("effect_id"):
            entries[str(entry["effect_id"])] = entry
    evaluation_records: list[dict[str, Any]] = []
    for effect in sorted(registry.effects, key=effect_sort_key):
        if not isinstance(effect, dict):
            continue
        effect_id = str(effect.get("effect_id") or "")
        entry = entries.get(effect_id)
        evaluation_records.append(evaluate_effect(effect, entry, resolved, registry, auto_values))
    proposals = _proposals(evaluation_records, registry)
    unsupported = _unsupported(evaluation_records)
    baseline_scenario = catalog.get("baseline_scenario")
    if not isinstance(baseline_scenario, dict):
        baseline_scenario = default_scenario(registry, scenario_context)
    baseline_fingerprint = scenario_fingerprint(baseline_scenario)
    current_fingerprint = scenario_fingerprint(resolved)
    changed = current_fingerprint != baseline_fingerprint
    summary = _summary(catalog, evaluation_records, proposals, unsupported)
    return {
        "schema": C.SCHEMA_EVALUATION,
        "summary": summary,
        "effects": evaluation_records,
        "proposed_contributions": proposals,
        "unsupported": unsupported,
        "validation": validation,
        "scenario_fingerprint": current_fingerprint,
        "baseline_fingerprint": baseline_fingerprint,
        "changed_from_baseline": changed,
        "dps_recomputed": False,
        "scenario_resolved": resolved,
    }


__all__ = [
    "PROPOSAL_STATUSES",
    "condition_results",
    "evaluate_effect",
    "evaluate_scenario",
]
