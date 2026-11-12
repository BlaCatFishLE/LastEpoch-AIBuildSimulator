"""Page view builder for the conditional Buff page (contract §9).

``build_view(context)`` turns the assembled ``result["conditional"]`` dict into
the deterministic, JSON-serialisable ``view`` block consumed by
``report_ui.pages.conditions``.  The builder is defensive: a degraded context
(no registry / no catalog / empty evaluation) still renders a small explanation
page instead of raising.

No absolute host paths and no timestamps are emitted; evidence locators are
plain relative text.
"""

from __future__ import annotations

from typing import Any, Optional

from . import constants as C

CATEGORY_FILTER_ORDER = (
    "other_skill",
    "equipment",
    "idol",
    "passive",
    "main_skill",
    "target",
    "action",
)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _num(value: Any) -> Optional[float | int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    return None


def _condition_text(effect: dict[str, Any], registry_states: dict[str, str]) -> str:
    conditions = effect.get("conditions") or {}
    note = _text(conditions.get("note"))
    if note:
        return note
    labels: list[str] = []
    for group in ("all", "any"):
        for entry in conditions.get(group) or []:
            if not isinstance(entry, dict):
                continue
            state_id = _text(entry.get("state"))
            label = registry_states.get(state_id) or state_id
            expected = entry.get("equals")
            if isinstance(expected, bool):
                labels.append(label if expected else f"非 {label}")
            else:
                labels.append(f"{label} = {expected}")
    if not labels:
        return ""
    if conditions.get("any"):
        return " 或 ".join(labels)
    return " 且 ".join(labels)


def _activation_text(effect: dict[str, Any]) -> str:
    activation = effect.get("activation") or {}
    kind = _text(activation.get("kind"))
    kind_labels = {
        "persistent_buff": "持续 Buff",
        "passive": "常驻被动",
        "conditional_passive": "条件被动",
        "stacks_buff": "叠层 Buff",
        "numeric_threshold": "数值门槛自动判定",
        "cast_state": "单次施法状态",
        "action": "动作机制",
        "baseline_included": "已计入基础",
        "layout": "布局条件",
    }
    pieces: list[str] = []
    if kind in kind_labels:
        pieces.append(kind_labels[kind])
    source_action = _text(activation.get("source_action"))
    if source_action:
        pieces.append(f"来源动作：{source_action}")
    duration = activation.get("duration_s")
    if duration is not None:
        pieces.append(f"持续 {C.format_actual(float(duration), 'seconds')}")
    max_stacks = activation.get("max_stacks")
    if isinstance(max_stacks, int):
        pieces.append("不叠层" if max_stacks <= 1 else f"最多 {max_stacks} 层")
    refresh = _text(activation.get("refresh"))
    if refresh == "recast_refreshes":
        pieces.append("重复施放刷新")
    description = _text(activation.get("description"))
    if description:
        pieces.append(description)
    return "；".join(pieces)


def _effect_evidence(effect: dict[str, Any]) -> list[dict[str, Any]]:
    return C.evidence_entries(effect.get("evidence"))


def _group_label(name: str, effects: list[dict[str, Any]]) -> str:
    if not effects:
        return name
    first = effects[0]
    source = first.get("source") or {}
    display = _text(source.get("display"))
    effect_name = _text(first.get("name"))
    tree = _text(source.get("tree")) or _text((first.get("match") or {}).get("tree"))
    if tree and effect_name and tree.casefold() not in effect_name.casefold():
        return f"{tree} · {effect_name}"
    if effect_name:
        return effect_name
    if display:
        return display.split(" · ")[0]
    return name


def _empty_view(context: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any]:
    disclaimer = _text((context.get("context") or {}).get("disclaimer"))
    return {
        "schema": C.SCHEMA_VIEW,
        "available": False,
        "header": {
            "title": "条件 Buff 配置",
            "question": "哪些条件效果可能改变 Black Hole？如何设置？",
            "mode_label": "",
            "baseline_label": "",
            "disclaimer": disclaimer,
            "dps_recomputed": False,
            "dps_note": "本页只生成场景与求值说明，不重算 DPS；DPS 页仍为生成基线。",
        },
        "counts": {
            "checked_objects": 0,
            "denominator_breakdown": {
                "items": 0,
                "item_mods": 0,
                "specializations": 0,
                "skill_nodes_allocated": 0,
                "passive_nodes": 0,
                "snapshot_contributions": 0,
                "excluded_sources": 0,
            },
            "candidates": 0,
            "configured": 0,
            "relevant": 0,
            "scenario_dependent": 0,
            "unconfirmed": 0,
            "not_relevant": 0,
            "auto_active": 0,
            "auto_inactive": 0,
            "auto_unknown": 0,
            "engine_supported": 0,
            "needs_rules": 0,
            "needs_time": 0,
            "mechanism_unresolved": 0,
            "not_selected": 0,
            "condition_met": 0,
            "condition_unmet": 0,
        },
        "filters": _filters(),
        "states": [],
        "presets": [],
        "effects": [],
        "groups": [],
        "auto_notes": [],
        "checked": {"summary": [], "records": [], "excluded_sources": [], "data_gaps": issues},
        "dependency": {"note": "", "modules": []},
        "export": {
            "scenario_filename": C.DEFAULT_SCENARIO_FILENAME,
            "command_hint": "",
        },
        "client_rules": {},
        "scenario": {},
        "status": _text(context.get("status") or "unavailable"),
        "issues": issues,
    }


def _filters() -> dict[str, Any]:
    categories = [{"id": "all", "label": "全部"}]
    for category in CATEGORY_FILTER_ORDER:
        categories.append({"id": category, "label": C.CATEGORY_LABELS.get(category, category)})
    relevance = [{"id": "all", "label": "全部"}]
    for status in C.RELEVANCE_STATUSES:
        relevance.append({"id": status, "label": C.RELEVANCE_LABELS[status]})
    support = [{"id": "all", "label": "全部"}]
    for level in C.SUPPORT_LEVELS:
        support.append({"id": level, "label": C.SUPPORT_LABELS[level]})
    statuses = [{"id": "all", "label": "全部"}]
    for status in C.STATUS_ORDER:
        statuses.append({"id": status, "label": C.STATUS_LABELS[status]})
    return {
        "categories": categories,
        "relevance": relevance,
        "support": support,
        "statuses": statuses,
    }


def build_view(context: dict[str, Any]) -> dict[str, Any]:
    """Build the §9 page view for one conditional context."""
    try:
        return _build_view(context)
    except Exception:  # noqa: BLE001 - the page must always render
        try:
            issues = list(context.get("issues") or [])
        except Exception:  # pragma: no cover - defensive
            issues = []
        return _empty_view(context if isinstance(context, dict) else {}, issues)


def _build_view(context: dict[str, Any]) -> dict[str, Any]:
    context = context if isinstance(context, dict) else {}
    context_block = context.get("context") or {}
    catalog = context.get("catalog") or {}
    evaluation = context.get("evaluation") or {}
    scenario = context.get("scenario") or {}
    rules = context.get("rules") or {}
    issues = list(context.get("issues") or [])
    status = _text(context.get("status") or "")
    if not context_block or not catalog.get("effects"):
        return _empty_view(context, issues)

    mode = context_block.get("mode") or {}
    baseline = context_block.get("baseline_profile") or {}
    state_labels: dict[str, str] = {}
    for state in rules.get("states") or []:
        if isinstance(state, dict):
            state_labels[_text(state.get("id"))] = _text(state.get("label"))
    for state in context_block.get("shared_states") or []:
        if isinstance(state, dict):
            state_labels.setdefault(_text(state.get("id")), _text(state.get("label")))
    for state in catalog.get("auto_states") or []:
        if isinstance(state, dict):
            state_labels.setdefault(_text(state.get("id")), _text(state.get("label")))
    affects = catalog.get("affects") if isinstance(catalog.get("affects"), dict) else {}
    effect_by_id: dict[str, dict[str, Any]] = {}
    for entry in catalog.get("effects") or []:
        if isinstance(entry, dict) and entry.get("effect_id"):
            effect_by_id[_text(entry["effect_id"])] = entry
    evaluation_by_id: dict[str, dict[str, Any]] = {}
    for record in evaluation.get("effects") or []:
        if isinstance(record, dict) and record.get("effect_id"):
            evaluation_by_id[_text(record["effect_id"])] = record
    registry_effect_by_id: dict[str, dict[str, Any]] = {}
    for effect_id, entry in effect_by_id.items():
        effect = entry.get("effect")
        if isinstance(effect, dict):
            registry_effect_by_id[effect_id] = effect

    # -- states ------------------------------------------------------------
    scenario_states = scenario.get("states") if isinstance(scenario.get("states"), dict) else {}
    states: list[dict[str, Any]] = []
    for state in rules.get("states") or []:
        if not isinstance(state, dict):
            continue
        state_id = _text(state.get("id"))
        states.append(
            {
                "id": state_id,
                "label": _text(state.get("label")),
                "kind": _text(state.get("kind")),
                "source": "user",
                "value": scenario_states.get(state_id, state.get("default")),
                "description": _text(state.get("description")),
                "affects_labels": _affects_labels(affects, state_id),
                "editable": True,
            }
        )
    for entry in catalog.get("auto_states") or []:
        if not isinstance(entry, dict):
            continue
        state_id = _text(entry.get("id"))
        states.append(
            {
                "id": state_id,
                "label": _text(entry.get("label")),
                "kind": "number" if entry.get("kind") == "number" else "bool",
                "source": "auto",
                "value": entry.get("value"),
                "display": entry.get("display"),
                "editable": False,
                "note": _text(entry.get("note")),
                "affects_labels": _affects_labels(affects, state_id),
            }
        )
    for entry in catalog.get("baseline_states") or []:
        if not isinstance(entry, dict):
            continue
        state_id = _text(entry.get("id"))
        states.append(
            {
                "id": state_id,
                "label": _text(entry.get("label")),
                "kind": "bool",
                "source": "baseline",
                "value": entry.get("value"),
                "editable": False,
                "note": _text(entry.get("note")),
                "affects_labels": _affects_labels(affects, state_id),
            }
        )

    # -- presets -----------------------------------------------------------
    presets: list[dict[str, Any]] = []
    default_preset_id = ""
    for preset in rules.get("presets") or []:
        if not isinstance(preset, dict):
            continue
        preset_id = _text(preset.get("preset_id"))
        if preset.get("is_default") and not default_preset_id:
            default_preset_id = preset_id
        presets.append(
            {
                "id": preset_id,
                "label": _text(preset.get("label")),
                "description": _text(preset.get("description")),
                "is_default": bool(preset.get("is_default")),
            }
        )

    # -- effects -----------------------------------------------------------
    effects: list[dict[str, Any]] = []
    groups_map: dict[str, list[dict[str, Any]]] = {}
    for effect_id in sorted(effect_by_id):
        entry = effect_by_id[effect_id]
        effect = registry_effect_by_id.get(effect_id) or entry.get("effect") or {}
        record = evaluation_by_id.get(effect_id) or {}
        conditions = effect.get("conditions") or {}
        condition_checks: list[dict[str, Any]] = []
        for result in record.get("condition_results") or []:
            if not isinstance(result, dict):
                continue
            condition_checks.append(
                {
                    "label": _text(result.get("label")),
                    "state_id": _text(result.get("state_id")),
                    "expected": result.get("expected"),
                    "actual": result.get("actual"),
                    "met": result.get("met"),
                    "source": _text(result.get("source")),
                }
            )
        group_id = _text(effect.get("group_id"))
        relevance_status = _text((effect.get("relevance") or {}).get("status")) or "unconfirmed"
        support_level = _text((effect.get("support") or {}).get("level")) or "mechanism_unresolved"
        entry_status = record.get("status") or "not_selected"
        control = entry.get("control") or {}
        control_payload = {
            "kind": _text(control.get("kind")) or "none",
            "state_id": control.get("state_id"),
            "stack_state_id": control.get("stack_state_id"),
            "cast_state_id": control.get("cast_state_id"),
            "label": _text((effect.get("control") or {}).get("label")),
            "help": _text((effect.get("control") or {}).get("help")),
        }
        relevance = {
            "status": relevance_status,
            "label": C.RELEVANCE_LABELS.get(relevance_status, relevance_status),
            "tones": C.RELEVANCE_TONES.get(relevance_status, "muted"),
        }
        support = {
            "level": support_level,
            "label": C.SUPPORT_LABELS.get(support_level, support_level),
            "tone": C.SUPPORT_TONES.get(support_level, "muted"),
            "gap": _text((effect.get("support") or {}).get("engine_gap")),
            "next_probe": _text((effect.get("support") or {}).get("next_probe")),
            "open_questions": list((effect.get("support") or {}).get("open_questions") or []),
        }
        status_payload = {
            "initial": entry_status,
            "label": C.STATUS_LABELS.get(entry_status, entry_status),
            "tone": C.STATUS_TONES.get(entry_status, "muted"),
            "reason": _text(record.get("status_reason")),
        }
        existing = entry.get("existing_handling") or record.get("existing_handling") or []
        baseline_note = None
        dedupe_note = "无重复计入；现有来源账未包含此项。"
        if existing:
            active = [
                row
                for row in existing
                if isinstance(row, dict)
                and row.get("included")
                and row.get("condition_status") in ("always", "satisfied")
            ]
            dedupe_note = (
                f"现有来源账包含 {len(existing)} 条相关贡献（{len(active)} 条当前生效）；"
                "拟议贡献已列出重复 ids，重算时需先去重。"
            )
            if relevance_status and (effect.get("relevance") or {}).get("baseline_relation") == "already_in_baseline":
                ids = "、".join(_text(row.get("contribution_id")) for row in active[:3])
                baseline_note = (
                    f"已计入基础来源账（{len(active)} 条生效贡献"
                    + (f"：{ids}" if ids else "")
                    + "）；本页不重复生成。"
                )
                if _text((effect.get("activation") or {}).get("kind")) == "numeric_threshold":
                    unmet = any(check.get("met") is not True for check in condition_checks)
                    baseline_note += (
                        " 基础值已计入，翻倍部分未计入。"
                        if unmet
                        else " 基础值已计入；门槛条件当前满足，翻倍/增量部分需单独复核。"
                    )
        evidence = _effect_evidence(effect)
        advanced = {
            "effect_id": effect_id,
            "modifier_ids": [str(m.get("modifier_id")) for m in (effect.get("modifiers") or []) if isinstance(m, dict)],
            "engine_sink": [str(m.get("engine_sink")) for m in (effect.get("modifiers") or []) if isinstance(m, dict)],
            "research_status": _text((effect.get("research") or {}).get("status")),
            "open_questions": list((effect.get("research") or {}).get("open_questions") or []),
            "qualification": entry.get("qualification") or {},
        }
        effect_view = {
            "effect_id": effect_id,
            "group_id": group_id,
            "name": _text(effect.get("name")),
            "name_en": _text(effect.get("name_en")),
            "category": _text(entry.get("category")),
            "category_label": C.CATEGORY_LABELS.get(_text(entry.get("category")), _text(entry.get("category"))),
            "source_label": _text((effect.get("source") or {}).get("display")),
            "source_detail": entry.get("source_detail") or {},
            "summary": _text(entry.get("summary")) or _text(effect.get("name")),
            "value_lines": list(entry.get("value_lines") or []),
            "condition_text": _condition_text(effect, state_labels),
            "impact_path": _text((effect.get("relevance") or {}).get("path")),
            "branches": list((effect.get("relevance") or {}).get("branches") or []),
            "relevance": relevance,
            "support": support,
            "control": control_payload,
            "status": status_payload,
            "condition_checks": condition_checks,
            "activation_text": _activation_text(effect),
            "baseline_note": baseline_note,
            "dedupe_note": dedupe_note,
            "evidence": evidence,
            "advanced": advanced,
        }
        effects.append(effect_view)
        group_key = group_id or effect_id
        groups_map.setdefault(group_key, []).append(effect_view)

    groups: list[dict[str, Any]] = []
    for group_id in sorted(groups_map):
        members = groups_map[group_id]
        groups.append(
            {
                "id": group_id,
                "label": _group_label(group_id, [registry_effect_by_id.get(m["effect_id"], {}) for m in members]),
                "effect_ids": [m["effect_id"] for m in members],
            }
        )

    # -- auto notes --------------------------------------------------------
    auto_notes: list[dict[str, Any]] = []
    for entry in catalog.get("auto_states") or []:
        if not isinstance(entry, dict):
            continue
        entry_label = _text(entry.get("label"))
        for comparison in entry.get("comparisons") or []:
            if not isinstance(comparison, dict):
                continue
            met = comparison.get("met")
            threshold = comparison.get("threshold")
            value = entry.get("value")
            op = _text(comparison.get("op")) or ">="
            explain = _auto_explain(entry_label, value, op, threshold, met)
            auto_notes.append(
                {
                    "id": _text(comparison.get("state")),
                    "label": _text(comparison.get("label")) or entry_label,
                    "met": met,
                    "explain": explain,
                }
            )
    for entry in catalog.get("baseline_states") or []:
        if not isinstance(entry, dict):
            continue
        auto_notes.append(
            {
                "id": _text(entry.get("id")),
                "label": _text(entry.get("label")),
                "met": entry.get("value"),
                "baseline_fixed": True,
                "explain": _text(entry.get("note")),
            }
        )

    # -- checked -----------------------------------------------------------
    scope = catalog.get("checked_scope") or {}
    checked_summary: list[dict[str, Any]] = []
    for category in scope.get("categories") or []:
        if not isinstance(category, dict):
            continue
        checked_summary.append(
            {
                "category": _text(category.get("id")),
                "label": _text(category.get("label")),
                "examined": int(category.get("examined") or 0),
                "candidates": int(category.get("candidates") or 0),
                "excluded": int(category.get("excluded") or 0),
                "baseline": int(category.get("baseline") or 0),
            }
        )
    checked_records: list[dict[str, Any]] = []
    for record in catalog.get("checked_records") or []:
        if not isinstance(record, dict):
            continue
        disposition = _text(record.get("disposition"))
        checked_records.append(
            {
                "record_id": _text(record.get("record_id")),
                "category": _text(record.get("category")),
                "source_label": _text(record.get("source_label")),
                "effect_summary": _text(record.get("effect_summary")),
                "disposition": disposition,
                "disposition_label": C.DISPOSITION_LABELS.get(disposition, disposition),
                "reason": _text(record.get("reason")),
                "relevance": _text(record.get("relevance")),
            }
        )
    checked = {
        "summary": checked_summary,
        "records": checked_records,
        "excluded_sources": [
            {
                "id": _text(source.get("id")),
                "label": _text(source.get("label")),
                "reason": _text(source.get("reason")),
                "category": _text(source.get("category")),
                "points_total": source.get("points_total"),
            }
            for source in scope.get("excluded_sources") or []
            if isinstance(source, dict)
        ],
        "data_gaps": [
            {
                "id": _text(gap.get("id")),
                "severity": _text(gap.get("severity")),
                "area": _text(gap.get("area")),
                "message": _text(gap.get("message")),
                "probe": _text(gap.get("probe")),
            }
            for gap in catalog.get("data_gaps") or []
            if isinstance(gap, dict)
        ],
    }

    # -- dependency / export ----------------------------------------------
    modules: list[dict[str, Any]] = []
    for module in context_block.get("computation_modules") or []:
        if not isinstance(module, dict):
            continue
        modules.append(
            {
                "id": _text(module.get("id")),
                "label": _text(module.get("label")),
                "question": _text(module.get("question")),
                "consumes": list(module.get("consumes") or []),
                "calc_refs": list(module.get("calc_refs") or []),
                "rule_ids": list(module.get("rule_ids") or []),
                "notes": _text(module.get("notes")),
                "evidence": C.evidence_entries(module.get("evidence")),
            }
        )
    dependency = {
        "note": "条件页只声明状态；模块的实际求值仍由引擎规则完成，本页不重算 DPS。",
        "modules": modules,
    }
    scenario_filename = C.DEFAULT_SCENARIO_FILENAME
    files = context.get("input", {}).get("files") if isinstance(context.get("input"), dict) else []
    calc_hint = ""
    if isinstance(files, list):
        for item in files:
            if isinstance(item, dict) and item.get("role") == "calc_input" and item.get("path"):
                calc_hint = _text(item.get("path"))
                break
    export = {
        "scenario_filename": scenario_filename,
        "command_hint": (
            "$env:PYTHONPATH='tools\\phase3\\src'; .\\.venv\\Scripts\\python.exe -m le_calc.cli conditions "
            "--snapshot <case>/snapshot/input-snapshot.affix-db.json "
            f"--calc-input {calc_hint or '<case>/inputs/calculation-input.affix-db.json'} "
            "--out <out-dir>"
        ),
    }

    summary = evaluation.get("summary") or {}
    denominator_breakdown = summary.get("denominator_breakdown")
    if not isinstance(denominator_breakdown, dict):
        denominator_breakdown = {
            "items": int((catalog.get("checked_scope") or {}).get("counts", {}).get("items") or 0),
            "item_mods": 0,
            "specializations": 0,
            "skill_nodes_allocated": 0,
            "passive_nodes": 0,
            "snapshot_contributions": 0,
            "excluded_sources": 0,
        }
    counts = {
        "checked_objects": int(summary.get("checked_objects") or 0),
        "denominator_breakdown": dict(denominator_breakdown),
        "candidates": int(summary.get("candidates") or 0),
        "configured": int(summary.get("configured") or 0),
        "relevant": int(summary.get("relevant") or 0),
        "scenario_dependent": int(summary.get("scenario_dependent") or 0),
        "unconfirmed": int(summary.get("unconfirmed") or 0),
        "not_relevant": int(summary.get("not_relevant") or 0),
        "auto_active": int(summary.get("auto_active") or 0),
        "auto_inactive": int(summary.get("auto_inactive") or 0),
        "auto_unknown": int(summary.get("auto_unknown") or 0),
        "engine_supported": int(summary.get("engine_supported") or 0),
        "needs_rules": int(summary.get("needs_rule_mapping") or 0),
        "needs_time": int(summary.get("needs_time") or 0),
        "mechanism_unresolved": int(summary.get("mechanism_unresolved") or 0),
        "not_selected": int(summary.get("not_selected") or 0),
        "condition_met": int(summary.get("condition_met") or 0),
        "condition_unmet": int(summary.get("condition_unmet") or 0),
    }
    mode_label = _text(mode.get("label"))
    baseline_label = _text(baseline.get("label"))
    disclaimer = _text(context_block.get("disclaimer"))
    view = {
        "schema": C.SCHEMA_VIEW,
        "available": _text(context.get("status")) in ("ready", "degraded") and bool(catalog.get("effects")),
        "header": {
            "title": "条件 Buff 配置",
            "question": "哪些条件效果可能改变 Black Hole？如何设置？",
            "mode_label": mode_label,
            "baseline_label": baseline_label,
            "disclaimer": disclaimer,
            "dps_recomputed": False,
            "dps_note": "本页只生成场景与求值说明，不重算 DPS；DPS 页仍为生成基线。",
        },
        "counts": counts,
        "filters": _filters(),
        "states": states,
        "presets": presets,
        "effects": effects,
        "groups": groups,
        "auto_notes": auto_notes,
        "checked": checked,
        "dependency": dependency,
        "export": export,
        "client_rules": rules,
        "scenario": scenario,
        "status": status,
        "issues": issues,
    }
    return C.json_safe(view)


def _affects_labels(affects: dict[str, Any], state_id: str) -> list[str]:
    labels = affects.get(state_id) if isinstance(affects, dict) else None
    if isinstance(labels, list):
        return [str(label) for label in labels]
    return []


def _auto_explain(
    label: str,
    value: Any,
    op: str,
    threshold: Any,
    met: Any,
) -> str:
    if value is None:
        return f"{label} 无法从当前输入派生，判定未完成。"
    display = C.fmt_number(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else _text(value)
    if met is True:
        return f"当前 {label} = {display}，满足 {op} {threshold}。"
    if met is False:
        return f"当前 {label} = {display}，不满足 {op} {threshold}。"
    return f"当前 {label} = {display}，门槛条件无法确认。"


__all__ = ["build_view"]
