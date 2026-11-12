"""Display-data layer for the offline Black Hole damage report.

``build_view(result)`` turns one ``result-v1`` dict into a render-ready view::

    context        report identity, fixed scenario, single-cast and loop KPIs
    sources        player-readable catalogue (see report_ui.sources)
    composition    fire / cold body branches and the ignite single-layer branch
    ignite         four-step ignite explanation, cohorts and tail split
    timeline       derived single-cast curves, binned DPS and loop samples
    validation     rule ledger, issues, exclusions, consistency checks
    sensitivity    monster-level sensitivity rows (does not change the report)
    conditions     conditional-Buff configuration payload (forwarded from
                   ``le_calc.conditional``; this layer never evaluates effects)

The layer never re-implements the damage rules.  Every number is either read
from the engine result or derived by exact integration of data that result
already contains (body tick sums, ignite cohort intervals).  Derived values
carry a closure check so the page can show whether they agree with the engine
totals.
"""

from __future__ import annotations

import bisect
import datetime as _dt
import math
import re
from typing import Any, Iterable

from . import labels, sources as source_catalog, units
from .player_text import clean_engineering_text

__all__ = ["build_view", "PAGE_IDS", "PAGE_LABELS", "PAGE_TITLES", "empty_conditions_view"]

PAGE_IDS = ["overview", "composition", "ignite", "timeline", "validation", "conditions"]
PAGE_LABELS = {
    "overview": "总览",
    "composition": "伤害构成",
    "ignite": "点燃",
    "timeline": "时间轴",
    "validation": "增伤来源",
    "conditions": "Buff 设置",
}
#: short page names only: the shell renders tabs/nav from these, so no question
#: sentence is duplicated into the page head (guide §5.1 / §13)
PAGE_TITLES = {
    "overview": "总览",
    "composition": "伤害构成",
    "ignite": "点燃",
    "timeline": "时间轴",
    "validation": "增伤来源",
    "conditions": "Buff 设置",
}

_BRANCH_LABELS = {
    "body-fire": "火焰本体",
    "body-cold": "冰霜本体",
    "body": "本体",
    "ignite": "点燃",
    "mechanism:heat-wave": "Heat Wave 施加",
    "mechanism:bh-duration": "黑洞持续时间",
    "mechanism:ignite-duration": "点燃持续时间",
    "mechanism:cast-time": "施法时间",
    "mechanism:cooldown": "冷却与充能",
    "mechanism:charges": "最大充能",
    "attribute:intelligence": "智力属性",
    "sensitivity": "敏感性分析",
}

_BRANCH_ORDER = ["body-fire", "body-cold", "body", "ignite"]

_MORE_CONDITIONS = {
    "node:bh2:10": "无可变条件，已分配点数下生效",
    "node:bh2:2": "代价为降低牵引范围，本次不改变伤害",
    "node:bh2:23": "目标为稀有/首领时生效（本场景满足）",
    "node:bh2:27": "目标位于中心 1 米内；仅本体，点燃不继承",
    "node:bh2:28": "每次施放仅 1 个黑洞；本场景满足",
    "node:bh2:7": "仅火焰伤害",
    "node:bh2:9": "双星结构；另有 +100% 结构与单体作用",
    "passive:mg-1:23:e2": "仅火焰持续伤害",
}


def empty_conditions_view(reason: str = "") -> dict:
    """Graceful page payload when a result has no conditional-context block.

    Old result.json files (generated before the conditions phase) and degraded
    runs must still render an explanatory page instead of raising.
    """

    note = reason or "本报告由旧版引擎生成，未包含条件 Buff 配置数据；请用当前命令重新生成报告。"
    return {
        "schema": "bh-conditional-view-v1",
        "available": False,
        "unavailable_reason": note,
        "header": {
            "title": "条件 Buff",
            "question": "哪些条件效果可能改变主技能？当前如何设置？",
            "mode_label": "固定条件估算",
            "baseline_label": "构筑基础状态",
            "disclaimer": "条件配置只生成场景与求值说明；本页不重算 DPS。",
            "dps_recomputed": False,
            "dps_note": "本页不重算 DPS。",
        },
        "counts": {},
        "filters": {"categories": [], "relevance": [], "support": [], "statuses": []},
        "states": [],
        "presets": [],
        "effects": [],
        "groups": [],
        "auto_notes": [],
        "checked": {"summary": [], "records": [], "excluded_sources": [], "data_gaps": []},
        "dependency": {"modules": [], "note": ""},
        "export": {},
        "client_rules": {},
    }


# --------------------------------------------------------------------------- #
# tiny coercion helpers
# --------------------------------------------------------------------------- #


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> list:
    return value if isinstance(value, list) else []


def _s(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _p(obj: Any, *keys: str, default=None):
    current = obj
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _row_status(row: dict) -> str:
    return _s(row.get("status"))


def _signed_fraction(value: Any) -> str:
    number = _num(value)
    return units.signed_pct(number) if number is not None else ""


def _local_time(iso: Any) -> str:
    text = _s(iso)
    if not text:
        return ""
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%d %H:%M")
    local = parsed.astimezone(_dt.timezone(_dt.timedelta(hours=8)))
    return local.strftime("%Y-%m-%d %H:%M")


def _first_num(*values):
    for value in values:
        number = _num(value)
        if number is not None:
            return number
    return None


def _checks_close(*values, rel: float = 1e-9) -> bool:
    numbers = [value for value in (_num(v) for v in values) if value is not None]
    if len(numbers) < 2:
        return False
    reference = max(abs(value) for value in numbers)
    if reference == 0:
        return all(value == 0 for value in numbers)
    return abs(numbers[0] - sum(numbers[1:])) <= reference * rel


def _first_dict(*values) -> dict:
    for value in values:
        if isinstance(value, dict) and value:
            return value
    return {}


def _unique(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        text = _s(value).strip()
        if text and text not in seen:
            seen.add(text)
            ordered.append(text)
    return ordered


# --------------------------------------------------------------------------- #
# scenario conditions: one identity per meaning (aliases are joined, not OR-ed)
# --------------------------------------------------------------------------- #

#: Canonical scenario conditions.  Old results name the same condition in
#: ``coverage`` and ``target_conditions`` with different field names; every
#: alias maps onto one identity and one user-facing label, so the summary,
#: drawer, overview and export reference the same condition exactly once.
_CONDITION_SPECS = (
    {
        "id": "both_stars_cover",
        "label": "双星同时覆盖目标",
        "aliases": (
            ("coverage", "both_stars_cover_target"),
            ("target_conditions", "both_stars_cover"),
        ),
    },
    {
        "id": "target_is_boss_or_rare",
        "label": "目标是 Boss 或稀有敌人",
        "aliases": (("target_conditions", "target_is_boss_or_rare"),),
    },
    {
        "id": "target_within_center_1m",
        "label": "目标位于中心 1 米内",
        "aliases": (
            ("coverage", "center_condition_met"),
            ("target_conditions", "target_within_center_1m"),
        ),
    },
)


def _normalize_conditions(coverage: dict, target_conditions: dict) -> tuple[list[dict], list[dict]]:
    """Merge alias fields of the same condition into one identity.

    A result can carry the same condition twice (legacy ``coverage`` field plus
    the newer ``target_conditions`` field).  Values are compared per alias; if
    aliases disagree the condition is marked as a conflict requiring review and
    is **not** OR-ed to ``True``.  Reading order is preserved for display.
    """

    conditions: list[dict] = []
    conflicts: list[dict] = []
    for spec in _CONDITION_SPECS:
        observed = []
        for container_key, field in spec["aliases"]:
            container = coverage if container_key == "coverage" else target_conditions
            if not isinstance(container, dict) or field not in container:
                continue
            observed.append(
                {
                    "field": f"{container_key}.{field}",
                    "value": bool(container.get(field)),
                }
            )
        if not observed:
            continue
        values = {item["value"] for item in observed}
        conflict = len(values) > 1
        item = {
            "id": spec["id"],
            "key": spec["id"],
            "label": spec["label"],
            "met": None if conflict else observed[0]["value"],
            "conflict": conflict,
            "aliases": [entry["field"] for entry in observed],
            "alias_values": observed,
        }
        conditions.append(item)
        if conflict:
            conflicts.append(item)
    return conditions, conflicts


def _condition_summary(conditions: Iterable[dict]) -> dict:
    enabled: list[str] = []
    disabled: list[str] = []
    conflicts: list[str] = []
    for item in conditions:
        label = _s(item.get("label"))
        if item.get("conflict"):
            conflicts.append(label)
        elif item.get("met"):
            enabled.append(label)
        else:
            disabled.append(label)
    return {
        "enabled": enabled,
        "disabled": disabled,
        "conflicts": conflicts,
        "met_count": len(enabled),
        "total": len(enabled) + len(disabled) + len(conflicts),
    }


_OBSOLETE_SCENARIO_ASSUMPTION_MARKERS = (
    "最优理论场景",
    "不代表真实轨道物理可达",
)


def _current_scenario_assumptions(scenario: dict) -> list[str]:
    """Scenario assumptions that still describe the *current* result.

    The old generator text declared the optimal theoretical scenario (both stars
    + centre 1 m) as a fixed default.  Those conditions are user-configurable
    now, so that sentence is superseded; the theoretical-target caveat is
    generated from the current result in the limitation block instead.
    """

    assumptions: list[str] = []
    for item in _l(scenario.get("assumptions")):
        text = _s(item)
        if not text:
            continue
        if any(marker in text for marker in _OBSOLETE_SCENARIO_ASSUMPTION_MARKERS):
            continue
        assumptions.append(text)
    return assumptions


# --------------------------------------------------------------------------- #
# conditional application state (actual applied / skipped records per effect)
# --------------------------------------------------------------------------- #

#: short state vocabulary shared by the source browser and the limitation block
_EFFECT_STATE_DEFS = {
    "applied_both": ("已计入", "ok"),
    "applied": ("已计入", "ok"),
    "baseline": ("已计入", "ok"),
    "applied_single": ("仅单次", "info"),
    "applied_loop": ("仅循环", "info"),
    "not_selected": ("未启用", "muted"),
    "selected": ("已选择", "info"),
    "condition_unmet": ("条件不足", "muted"),
    "unsupported": ("暂不支持", "muted"),
    "not_in_baseline": ("未计入", "muted"),
    "missing": ("待确认", "muted"),
    "unknown": ("待复核", "muted"),
}

#: fallback for degraded/legacy results that carry no channel support or
#: application states: the effect's own server-rendered status still says what
#: the configuration looks like.
_INITIAL_STATE_MAP = {
    "not_selected": "not_selected",
    "already_in_baseline": "baseline",
    "auto_active": "not_in_baseline",
    "auto_inactive": "condition_unmet",
    "auto_unknown": "unknown",
    "unsupported_action": "unsupported",
    "selected_condition_unmet": "condition_unmet",
    "selected_ready_needs_rules": "missing",
    "selected_ready_needs_time": "missing",
    "selected_unconfirmed": "unknown",
    "active_supported": "baseline",
    "not_available": "unknown",
}

_UNSUPPORTED_SUPPORT_LEVELS = {
    "needs_action_or_time_simulation",
    "mechanism_unresolved",
}

#: support-gap sentences that describe the pre-recompute stage (the cross-skill
#: Buff channel exists now); they are not shown for effects that channel handles
_STALE_EFFECT_GAP_MARKERS = (
    "没有跨技能",
    "其他技能树整体排除",
    "旧 DPS 基线没有",
    "现有规则把其他技能树",
)


def _sanitize_effect_gap(text: str, channel: dict) -> str:
    """Drop superseded "no cross-skill channel" wording once the channel exists."""

    gap = _s(text)
    if not gap:
        return ""
    if _s(channel.get("state")) in ("supported", "baseline") and any(
        marker in gap for marker in _STALE_EFFECT_GAP_MARKERS
    ):
        return ""
    return gap

#: runtime skip codes -> user-facing classification
_SKIP_REASON_CLASS = {
    "needs_action_or_time_simulation": "unsupported",
    "mechanism_unresolved": "unsupported",
    "unsupported_stat_key": "unsupported",
    "no_damage_modifiers": "unsupported",
    "action_not_supported": "unsupported",
    "cast_state_single_cast_only": "unsupported",
    "condition_unmet": "condition_unmet",
    "condition_not_met": "condition_unmet",
    "dedupe_already_active": "baseline",
    "already_in_baseline": "baseline",
    "auto_excluded": "not_in_baseline",
}


def _normalize_state_map(raw: Any) -> dict[str, dict]:
    """Accept ``{effect_id: {...}}`` or ``[{effect_id: ...}]`` application maps."""

    if isinstance(raw, dict):
        return {
            _s(key): value
            for key, value in raw.items()
            if _s(key) and isinstance(value, dict)
        }
    if isinstance(raw, list):
        mapped: dict[str, dict] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            effect_id = _s(item.get("effect_id")) or _s(item.get("id"))
            if effect_id:
                mapped[effect_id] = item
        return mapped
    return {}


def _selected_effect_ids(scenario: dict) -> dict[str, dict]:
    """Which effects the saved conditional scenario selects (toggle/stacks/cast)."""

    effects = _d(scenario.get("effects"))
    states = _d(scenario.get("states"))
    single_cast = _d(scenario.get("single_cast"))
    selected: dict[str, dict] = {}
    for effect_id, config in effects.items():
        config = _d(config)
        enabled = config.get("enabled")
        stacks = _num(config.get("stacks"))
        if enabled is True or (stacks is not None and stacks > 0):
            selected[_s(effect_id)] = {"kind": "effect", "value": stacks}
            continue
        for key in ("cast_state_id", "state_id"):
            state_id = _s(config.get(key))
            if state_id and bool(states.get(state_id)):
                selected[_s(effect_id)] = {"kind": key, "state": state_id}
                break
    return selected


def _effect_scenario_selected(effect: dict, scenario: dict) -> bool:
    effect_id = _s(effect.get("effect_id"))
    if effect_id and effect_id in _selected_effect_ids(scenario):
        return True
    control = _d(effect.get("control"))
    states = _d(scenario.get("states"))
    single_cast = _d(scenario.get("single_cast"))
    state_id = _s(control.get("state_id"))
    if state_id and bool(states.get(state_id)):
        return True
    cast_id = _s(control.get("cast_state_id"))
    if cast_id and bool(single_cast.get(cast_id)):
        return True
    stack_id = _s(control.get("stack_state_id"))
    if stack_id and (_num(states.get(stack_id)) or 0) > 0:
        return True
    return False


def _is_single_cast_effect(effect: dict, applied_rows: Iterable[dict]) -> bool:
    control = _d(effect.get("control"))
    if _s(control.get("kind")) == "cast_state":
        return True
    for row in applied_rows:
        if not isinstance(row, dict):
            continue
        scope = _s(row.get("scope"))
        if scope and scope not in ("single", "both"):
            return False
    return False


def _classify_skipped(effect: dict, skipped_rows: Iterable[dict], channel: dict, *, reason_code: str = "") -> str:
    reasons = {_s(_d(row).get("reason")) for row in skipped_rows}
    reasons.discard("")
    if reason_code:
        reasons.add(_s(reason_code))
    support_level = _s(channel.get("support_level")) or _s(_d(effect.get("support")).get("level"))
    for reason in reasons:
        classified = _SKIP_REASON_CLASS.get(reason)
        if classified:
            return classified
    if any("condition" in item or "unmet" in item for item in reasons):
        return "condition_unmet"
    if support_level in _UNSUPPORTED_SUPPORT_LEVELS:
        return "unsupported"
    if support_level == "engine_supported":
        return "condition_unmet"
    return "unsupported"


def _resolve_effect_state(
    effect: dict,
    *,
    app_state: dict,
    applied_rows: list[dict],
    skipped_rows: list[dict],
    channel: dict,
    scenario_applied: bool,
    selected: bool,
    assumption_texts: Iterable[str] = (),
) -> dict:
    """Resolve one conditional effect to a short user-facing application state.

    Preference order:

    1. ``recompute.application_states`` (backend task-6/§R5 field; the
       ``single``/``loop`` booleans are authoritative, ``state`` is derived);
    2. the authoritative ``condition_application.applied/skipped`` records;
    3. the effect's baseline relation (``already_in_baseline``) and the
       ``channel_support`` matrix for display-only fallback.

    Missing fields degrade to "待复核" instead of raising, so old results still
    render.
    """

    support = _d(effect.get("support"))
    relevance = _d(effect.get("relevance"))
    support_level = _s(support.get("level")) or _s(channel.get("support_level"))
    baseline_related = _s(channel.get("state")) == "baseline" or bool(_s(effect.get("baseline_note")))
    texts: list[str] = [text for text in assumption_texts if _s(text)]
    assumption_ids: list[str] = []
    state_key = ""
    single: bool | None = None
    loop: bool | None = None
    reason = ""
    reason_detail = ""

    raw_state = _s(app_state.get("state") or app_state.get("status") or app_state.get("decision")).lower()
    reason_code = _s(app_state.get("reason"))
    if raw_state:
        single = app_state.get("single") if isinstance(app_state.get("single"), bool) else None
        loop = app_state.get("loop") if isinstance(app_state.get("loop"), bool) else None
        reason_detail = _s(app_state.get("reason_detail")) or _s(app_state.get("detail"))
        reason = reason_detail or reason_code
        support_level = _s(app_state.get("support_level")) or support_level
        if raw_state in _EFFECT_STATE_DEFS and not raw_state.startswith("applied"):
            state_key = raw_state
        elif raw_state.startswith("applied"):
            state_key = "applied_both"
        if raw_state == "not_selected":
            state_key = "baseline" if reason_code == "already_in_baseline" else "not_selected"
        if state_key and (single is True or loop is True) and state_key not in ("baseline",):
            # the backend's single/loop flags are authoritative: a "skipped" loop
            # run with an applied single cast is 仅单次, not 暂不支持
            if single and loop:
                state_key = "applied_both"
            elif single:
                state_key = "applied_single"
            elif loop:
                state_key = "applied_loop"
        if state_key == "applied_both" and single is True and loop is False:
            state_key = "applied_single"
        if state_key == "baseline" and raw_state == "not_selected":
            reason = reason_detail or ""
    # the backend's single/loop flags are authoritative even when the derived
    # state string still says "skipped" (e.g. skipped loops, applied single cast)
    if not state_key and (single is True or loop is True):
        if single and loop:
            state_key = "applied_both"
        elif single:
            state_key = "applied_single"
        else:
            state_key = "applied_loop"

    # The conditional recompute artifact can apply Buffs even when the displayed
    # report is still the generated baseline (scenario_applied=False).  In that
    # case those applications are a *preview*, not the displayed result: only
    # the effects already counted in the baseline may render as 已计入.
    if not scenario_applied and state_key.startswith("applied"):
        state_key = "baseline" if baseline_related else "not_in_baseline"
        single = None
        loop = None

    if not state_key:
        authoritative = bool(app_state)
        if baseline_related and raw_state in ("", "not_selected", "skipped"):
            state_key = "baseline"
        elif applied_rows and scenario_applied and not authoritative:
            if single is False and loop is True:
                state_key = "applied_loop"
            elif _is_single_cast_effect(effect, applied_rows):
                state_key = "applied_single"
                single = True if single is None else single
                loop = False if loop is None else loop
            else:
                state_key = "applied_both"
        elif applied_rows and not scenario_applied and not authoritative:
            state_key = "not_in_baseline"
            reason = reason or "条件重算通道可应用；当前显示的基础结果未计入"
        elif skipped_rows or raw_state in ("skipped", "skip", "not_applied"):
            state_key = _classify_skipped(effect, skipped_rows, channel, reason_code=reason_code)
            if not reason and skipped_rows:
                reason = _s(skipped_rows[0].get("detail")) or _s(skipped_rows[0].get("reason"))
        elif scenario_applied:
            state_key = "not_selected"
        elif selected:
            state_key = "selected"
        elif _s(channel.get("state")) == "supported":
            state_key = "not_in_baseline" if bool(channel.get("auto")) else "not_selected"
            if state_key == "not_in_baseline":
                reason = reason or "该效果可由条件重算应用；当前显示的基础结果未计入"
        elif _s(channel.get("state")) == "skipped":
            state_key = _classify_skipped(effect, skipped_rows, channel, reason_code=reason_code)
        elif relevance.get("status") == "unconfirmed":
            state_key = "unknown"
        else:
            state_key = _INITIAL_STATE_MAP.get(_s(_d(effect.get("status")).get("initial")), "missing")

    label, tone = _EFFECT_STATE_DEFS.get(state_key, _EFFECT_STATE_DEFS["unknown"])

    for row in applied_rows:
        note = _s(row.get("assumption_note")) or _s(row.get("assumption"))
        if note:
            texts.append(note)
        for assumption_id in _l(row.get("assumption_ids")):
            assumption_ids.append(_s(assumption_id))
    for item in _l(app_state.get("assumptions")):
        if isinstance(item, str):
            assumption_ids.append(item)
        elif isinstance(item, dict):
            texts.append(_s(item.get("text")) or _s(item.get("note")))
            assumption_ids.append(_s(item.get("id")))
    note = _s(app_state.get("assumption_note"))
    if note:
        texts.append(note)
    research_status = _s(_d(effect.get("advanced")).get("research_status"))
    if state_key in ("applied_both", "applied_single", "applied_loop", "baseline") and research_status in (
        "assumed",
        "unconfirmed",
    ):
        texts.append(f"机制证据状态为 {research_status}，当前结果按成立估算。")
    texts = _unique(texts)
    assumption_ids = [item for item in _unique(assumption_ids) if item not in texts]
    # display human-readable notes; the raw assumption ids are evidence only
    if not texts:
        texts = [item for item in assumption_ids if item and not item.startswith("assume:")]
        assumption_ids = [item for item in assumption_ids if item.startswith("assume:")]
    elif assumption_ids:
        assumption_ids = [item for item in assumption_ids if item not in texts]

    applied = state_key in ("applied_both", "applied_single", "applied_loop", "baseline")
    if applied:
        if single is None:
            single = True
        if loop is None:
            loop = state_key != "applied_single"
    else:
        single = False
        loop = False

    branch_source = _l(app_state.get("branches")) or _l(effect.get("branches"))
    branches = [
        source_catalog.BRANCH_LABELS.get(_s(branch), _s(branch))
        for branch in branch_source
    ]
    return {
        "effect_id": _s(effect.get("effect_id")),
        "name": source_catalog.conditional_source_name(effect),
        "state": state_key,
        "label": label,
        "tone": tone,
        "applied": applied,
        "single": bool(single),
        "loop": bool(loop),
        "selected": bool(selected),
        "reason": reason,
        "reason_code": reason_code,
        "support_level": support_level,
        "assumption": bool(texts),
        "assumption_texts": texts,
        "assumption_ids": assumption_ids,
        "applied_rows": applied_rows,
        "skipped_rows": skipped_rows,
        "modifier_ids": _unique(_s(item) for item in _l(app_state.get("modifier_ids"))),
        "branches": _unique(branches),
        "summary": _s(effect.get("summary")),
        "condition_text": _s(effect.get("condition_text")),
        "source_label": _s(effect.get("source_label")),
        "value_lines": [item for item in _l(effect.get("value_lines")) if isinstance(item, dict)],
        "control_label": _s(_d(effect.get("control")).get("label")),
        "support_gap": _sanitize_effect_gap(_s(support.get("gap")), channel),
        "relevance": _d(relevance).get("status") or "",
        "research_status": research_status,
    }


def _build_conditional_state(result: dict, conditions_view: dict) -> dict:
    """Normalise the conditional recompute application records for display.

    Prefers ``conditional.recompute.application_states`` (backend task-6/§R5),
    falls back to the applied/skipped condition-application records and then to
    the channel-support matrix.  Never raises: unknown/partial data renders as
    "待复核" states.
    """

    conditional = _d(result.get("conditional"))
    view = _d(conditional.get("view"))
    conditions_view = conditions_view or view
    recompute = _d(conditional.get("recompute"))
    application = _d(recompute.get("condition_application"))
    channel_support = _d(recompute.get("channel_support"))
    state_map = _normalize_state_map(
        _first_dict(recompute.get("application_states"), conditional.get("application_states"))
    )
    scenario_applied = bool(conditional.get("scenario_applied"))
    scenario = _first_dict(conditional.get("scenario"), view.get("scenario"))
    applied_rows = [row for row in _l(application.get("applied")) if isinstance(row, dict)]
    skipped_rows = [row for row in _l(application.get("skipped")) if isinstance(row, dict)]
    applied_effect_ids = _unique(_s(row.get("effect_id")) for row in applied_rows)
    for effect_id, app_state in state_map.items():
        raw = _s(app_state.get("state") or app_state.get("status")).lower()
        if raw.startswith("applied"):
            applied_effect_ids = _unique(applied_effect_ids + [effect_id])
    skipped_effect_ids = _unique(_s(row.get("effect_id")) for row in skipped_rows)
    for effect_id, app_state in state_map.items():
        if _s(app_state.get("state")).lower() in ("skipped", "skip", "not_applied"):
            skipped_effect_ids = _unique(skipped_effect_ids + [effect_id])

    effects_state: dict[str, dict] = {}
    assumption_rows = [row for row in _l(recompute.get("assumptions_applied")) if isinstance(row, dict)]
    assumption_texts_by_effect: dict[str, list[str]] = {}
    for row in assumption_rows:
        effect_id = _s(row.get("effect_id"))
        text = _s(row.get("text")) or _s(row.get("assumption_id"))
        if effect_id and text:
            assumption_texts_by_effect.setdefault(effect_id, []).append(text)
    for effect in _l(conditions_view.get("effects")):
        if not isinstance(effect, dict):
            continue
        effect_id = _s(effect.get("effect_id"))
        if not effect_id:
            continue
        app_state = state_map.get(effect_id) or {}
        state_applied_rows = [row for row in _l(app_state.get("applied_rows")) if isinstance(row, dict)]
        effects_state[effect_id] = _resolve_effect_state(
            effect,
            app_state=app_state,
            applied_rows=state_applied_rows
            or [row for row in applied_rows if _s(row.get("effect_id")) == effect_id],
            skipped_rows=[row for row in skipped_rows if _s(row.get("effect_id")) == effect_id],
            channel=_d(channel_support.get(effect_id)),
            scenario_applied=scenario_applied,
            selected=_effect_scenario_selected(effect, scenario),
            assumption_texts=assumption_texts_by_effect.get(effect_id) or [],
        )

    applied_effects = [
        state for state in effects_state.values() if state["state"] in ("applied_both", "applied_single", "applied_loop")
    ]
    baseline_effects = [state for state in effects_state.values() if state["state"] == "baseline"]
    applied_effect_ids = _unique(state["effect_id"] for state in applied_effects)
    recompute_applied_effect_ids = _unique(
        list(applied_effect_ids) + [state["effect_id"] for state in baseline_effects]
    )
    for effect_id in _l(recompute.get("applied_effects")):
        recompute_applied_effect_ids = _unique(recompute_applied_effect_ids + [_s(effect_id)])
    for row in applied_rows:
        recompute_applied_effect_ids = _unique(
            recompute_applied_effect_ids + [_s(row.get("effect_id"))]
        )
    selected_not_applied = [state for state in effects_state.values() if state["selected"] and not state["applied"]]
    unsupported = [state for state in effects_state.values() if state["state"] == "unsupported"]
    assumptions_applied = [
        state
        for state in effects_state.values()
        if state["assumption"] and (state["applied"] or state["selected"])
    ]
    target_conditions = _d(application.get("target_conditions")) or None
    effective_target = _first_dict(recompute.get("scenario_target"), conditional.get("scenario_target"))
    target_block = _first_dict(recompute.get("target"), conditional.get("target"))
    recompute_assumptions = [_s(item) for item in _l(recompute.get("assumptions")) if _s(item)]
    for row in assumption_rows:
        text = _s(row.get("text"))
        if text and text not in recompute_assumptions:
            recompute_assumptions.append(text)
    for state in applied_effects:
        for text in state["assumption_texts"]:
            recompute_assumptions.append(f"{state['name']}：{text}")
    return {
        "available": (bool(conditions_view.get("available")) and bool(conditions_view.get("effects")))
        or bool(recompute),
        "scenario_applied": scenario_applied,
        "dps_recomputed": bool(recompute.get("dps_recomputed")),
        "recompute_status": _s(recompute.get("status")),
        "applied_effect_ids": applied_effect_ids,
        "recompute_applied_effect_ids": recompute_applied_effect_ids,
        "skipped_effect_ids": skipped_effect_ids,
        "applied_count": len(applied_effects),
        "baseline_count": len(baseline_effects),
        "applied_effects": [
            {
                "effect_id": state["effect_id"],
                "name": state["name"],
                "label": state["label"],
                "single": state["single"],
                "loop": state["loop"],
                "assumption": state["assumption"],
                "applied_rows": state["applied_rows"],
            }
            for state in applied_effects
        ],
        "baseline_effects": [
            {"effect_id": state["effect_id"], "name": state["name"]} for state in baseline_effects
        ],
        "selected_not_applied": [
            {
                "effect_id": state["effect_id"],
                "name": state["name"],
                "label": state["label"],
                "reason": state["reason"],
            }
            for state in selected_not_applied
        ],
        "unsupported": [
            {
                "effect_id": state["effect_id"],
                "name": state["name"],
                "reason": state["reason"] or _s(_d(_d(result.get("conditional")).get("view")).get("available")),
            }
            for state in unsupported
        ],
        "assumptions_applied": [
            {
                "effect_id": state["effect_id"],
                "name": state["name"],
                "texts": state["assumption_texts"],
            }
            for state in assumptions_applied
        ],
        "assumptions": _unique(recompute_assumptions),
        "assumption_rows": assumption_rows,
        "target_conditions": target_conditions,
        "effective_target": effective_target,
        "target": target_block,
        "effects": effects_state,
        "channel_support": channel_support,
        "scenario": scenario,
        "scenario_effects": _d(scenario.get("effects")),
    }



# --------------------------------------------------------------------------- #
# report context
# --------------------------------------------------------------------------- #


def _build_context(result: dict) -> dict:
    run = _d(result.get("run"))
    scenario = _d(result.get("scenario"))
    target = _d(scenario.get("target"))
    coverage = _d(scenario.get("coverage"))
    conditions = _d(scenario.get("target_conditions"))
    snapshot = _d(result.get("snapshot"))
    character = _d(snapshot.get("character"))
    single = _d(result.get("single_cast"))
    totals = _d(single.get("totals"))
    cast = _d(single.get("cast"))
    body = _d(single.get("body"))
    ignite = _d(single.get("ignite"))
    ignite_totals = _d(ignite.get("totals"))

    body_components = _l(body.get("components"))
    body_parts = []
    for component in body_components:
        if not isinstance(component, dict):
            continue
        damage_type = _s(component.get("damage_type"))
        key = "fire" if damage_type.lower() == "fire" else "cold" if damage_type.lower() == "cold" else damage_type.lower()
        body_parts.append(
            {
                "key": key,
                "label": "火焰本体" if key == "fire" else "冰霜本体" if key == "cold" else f"{damage_type} 本体",
                "damage_type": damage_type,
                "total": _num(component.get("damage_total")),
                "share": None,
            }
        )
    body_total = _num(totals.get("body_total")) or _num(body.get("totals", {}).get("damage"))
    for part in body_parts:
        if body_total and part["total"] is not None:
            part["share"] = part["total"] / body_total

    status = _s(run.get("status"), "unknown")
    ruleset = _d(run.get("ruleset"))
    build = _d(_p(result, "input", "build")) or _d(_p(snapshot, "build_meta", "build"))
    revision = _s(build.get("revision_id"))
    build_id = _s(build.get("build_id"))
    build_label = build_id
    if revision:
        short = revision[:15] if len(revision) > 15 else revision
        build_label = f"{build_id} · {short}" if build_id else short

    fire_res = _d(_p(target, "resistances"))
    target_fire_res = _num(fire_res.get("fire"))

    condition_items, condition_conflicts = _normalize_conditions(coverage, conditions)
    condition_summary = _condition_summary(condition_items)
    conditional_state = _build_conditional_state(result, _d(_p(result, "conditional", "view")))
    conditional = _d(result.get("conditional"))
    conditional_applied = bool(conditional.get("scenario_applied"))
    recompute = _d(conditional.get("recompute"))
    if conditional_applied:
        # The displayed result is the re-computed conditional scenario: its
        # engine process may already carry the effective target; when the
        # recompute artifact exposes the consumed target use that as evidence of
        # record (alias equality is checked together with the result scenario).
        recompute_target = _first_dict(
            recompute.get("scenario_target"), conditional.get("scenario_target")
        )
        recompute_conditions = dict(_d(_p(recompute, "condition_application", "target_conditions")))
        for alias_key, target_key in (
            ("target_is_boss_or_rare", "boss_or_rare"),
            ("target_within_center_1m", "within_center_1m"),
            ("both_stars_cover", "both_stars_cover"),
        ):
            if alias_key in recompute_conditions or recompute_target.get(target_key) is None:
                continue
            recompute_conditions[alias_key] = bool(recompute_target.get(target_key))
        if recompute_conditions:
            merged_conditions = dict(conditions)
            for key, value in recompute_conditions.items():
                merged_conditions[_s(key)] = bool(value)
            condition_items, condition_conflicts = _normalize_conditions(coverage, merged_conditions)
            condition_summary = _condition_summary(condition_items)
        effective_level = _first_num(recompute_target.get("monster_level"), recompute.get("monster_level"))
        if effective_level is None:
            effective_level = _first_num(scenario.get("monster_level"), target.get("monster_level"))
        effective_fire = _first_num(
            recompute_target.get("fire_resistance"),
            _p(conditional, "scenario", "target", "fire_resistance"),
        )
        if effective_fire is None:
            effective_fire = target_fire_res
    else:
        effective_level = None
        effective_fire = None

    display_level = effective_level if effective_level is not None else _first_num(
        scenario.get("monster_level"), target.get("monster_level")
    )
    display_fire_res = effective_fire if effective_fire is not None else target_fire_res

    single_summary = {
        "total": _num(totals.get("single_cast_total")),
        "body_total": body_total,
        "body_parts": body_parts,
        "ignite_total": _num(totals.get("ignite_total")) or _num(ignite_totals.get("damage")),
        "ignite_tail": _num(totals.get("ignite_tail_damage")) or _num(ignite_totals.get("tail_damage")),
        "ignite_before_bh_end": _num(totals.get("ignite_damage_before_black_hole_end"))
        or _num(ignite_totals.get("damage_before_black_hole_end")),
        "stacks": _num(totals.get("expected_total_stacks_applied")),
        "last_expiry_s": _num(totals.get("last_ailment_expiry_s")),
        "observation_end_s": _num(totals.get("observation_end_s")) or _num(totals.get("last_ailment_expiry_s")),
        "window_damage_until_bh_end": _num(totals.get("window_damage_until_black_hole_end")),
        "bh_duration_s": _num(cast.get("black_hole_duration_s")),
        "bh_spawn_s": _num(cast.get("black_hole_spawn_s")),
        "bh_end_s": _num(cast.get("black_hole_end_s")),
        "cast_time_s": _num(cast.get("cast_time_s")),
        "cast_start_s": _num(cast.get("cast_start_s")),
        "tick_count": _num(body.get("tick_count")),
        "tick_interval_s": _num(body.get("tick_interval_s")),
        "duration_s": _num(body.get("duration", {}).get("duration_s")),
        "charges_before": _num(cast.get("charges_before")),
        "charges_after": _num(cast.get("charges_after")),
    }

    policy_meta = {
        _s(item.get("policy_id")): item
        for item in _l(_p(result, "action_policy", "loops"))
        if isinstance(item, dict)
    }
    loops = []
    for index, loop in enumerate(_l(result.get("loops"))):
        if not isinstance(loop, dict):
            continue
        policy_id = _s(loop.get("policy_id")) or f"policy-{index}"
        meta = _d(policy_meta.get(policy_id))
        loop_totals = _d(loop.get("totals"))
        windows = []
        for window in _l(loop.get("windows")):
            if not isinstance(window, dict):
                continue
            seconds = _num(window.get("window_s"))
            key = f"{seconds:g}" if seconds is not None else _s(window.get("window_s"))
            windows.append(
                {
                    "key": key,
                    "seconds": seconds,
                    "dps": _num(window.get("dps")),
                    "total": _num(window.get("total_damage")),
                    "body": _num(window.get("body_damage")),
                    "ignite": _num(window.get("ignite_damage")),
                    "tail": _num(window.get("unsettled_tail_damage")),
                    "casts": _num(window.get("cast_count")),
                }
            )
        loops.append(
            {
                "id": policy_id,
                "label": _s(meta.get("label")) or _s(loop.get("label")) or policy_id,
                "description": _s(meta.get("description")) or _s(loop.get("description")),
                "casts": _num(loop_totals.get("cast_count")),
                "cycle_s": _num(loop_totals.get("cycle_s")),
                "horizon_s": _num(loop.get("horizon_s")),
                "max_concurrent": _num(loop_totals.get("max_concurrent_black_holes")),
                "steady_state_dps": _num(loop_totals.get("steady_state_dps_cross_check")),
                "horizon_damage": _num(loop_totals.get("horizon_damage")),
                "horizon_dps": _num(loop_totals.get("horizon_dps")),
                "max_charges": _num(_p(loop, "resource", "max_charges")),
                "recharge_s": _num(_p(loop, "resource", "effective_recharge_s")),
                "windows": windows,
            }
        )

    return {
        "title": "Black Hole 伤害分析",
        "subtitle": "单技能伤害解释报告 · 离线单文件",
        "build_label": build_label,
        "build_id": build_id,
        "revision_id": revision,
        "generated_at": _local_time(run.get("generated_at")),
        "generated_at_raw": _s(run.get("generated_at")),
        "run_id": _s(run.get("run_id")),
        "status": status,
        "status_label": labels.STATUS_LABELS.get(status, "状态未知"),
        "status_tone": labels.STATUS_TONES.get(status, "muted"),
        "status_reason": _s(run.get("status_reason")),
        "model_label": "模型预期值",
        "engine": _d(run.get("engine")),
        "ruleset": {
            "id": _s(ruleset.get("id")),
            "sha256": _s(ruleset.get("sha256")),
            "game_version": _s(ruleset.get("game_version")),
        },
        "character": {
            "level": _num(character.get("level")),
            "class": _s(character.get("class")),
            "mastery": _s(character.get("mastery")),
        },
        "scenario": {
            "id": _s(scenario.get("scenario_id")),
            "name": _s(scenario.get("name")),
            "level": display_level,
            "dr": _first_num(scenario.get("monster_damage_reduction"), target.get("monster_damage_reduction")),
            "dr_factor": _first_num(scenario.get("monster_dr_factor"), target.get("monster_dr_factor")),
            "target_resistance": display_fire_res,
            "target_label": _s(target.get("label")),
            "target_notes": _s(target.get("notes")),
            "theoretical": bool(coverage.get("theoretical")),
            "coverage_note": _s(coverage.get("note")),
            "conditions": condition_items,
            "condition_summary": condition_summary,
            "condition_conflicts": condition_conflicts,
            "assumptions": _current_scenario_assumptions(scenario),
            "dr_source": _s(_p(result, "monster_dr", "source", "title")),
            "dr_source_url": _s(_p(result, "monster_dr", "source", "url")),
            "conditional_applied": conditional_applied,
            "result_kind": "conditional_scenario" if conditional_applied else "baseline",
            "result_kind_label": "条件场景重算结果" if conditional_applied else "生成基线结果",
        },
        "conditional": conditional_state,
        "single": single_summary,
        "loops": loops,
        "scope": {
            "included": [_s(item) for item in _l(_p(result, "scope", "included"))],
            "notes": [_s(item) for item in _l(_p(result, "scope", "notes"))],
            "note": _s(run.get("scope_note")),
        },
        "input": _d(result.get("input")),
        "fingerprints": _d(_p(result, "input", "fingerprints")),
        "repro_command": _s(run.get("repro_command")),
        "counts": {
            "sources": len(_l(result.get("source_ledger"))),
            "rules": len(_l(result.get("rule_ledger"))),
            "issues": len(_l(result.get("issues"))),
        },
    }


# --------------------------------------------------------------------------- #
# source usage scan (which factor really consumes which row)
# --------------------------------------------------------------------------- #


def _rules_by_id(result: dict) -> dict[str, dict]:
    return {
        _s(rule.get("rule_id")): rule
        for rule in _l(result.get("rule_ledger"))
        if isinstance(rule, dict)
    }


def _skill_source_for(
    node_index: dict[str, dict[str, str]], node: str, stat_preferences: Iterable[str]
) -> str | None:
    stats = node_index.get(str(node), {})
    for stat in stat_preferences:
        if stat in stats:
            return stats[stat]
    return None


def _resolve_ruleset_rule(
    source_id: str,
    node_index: dict[str, dict[str, str]],
    stat_preferences: Iterable[str],
) -> list[str]:
    """Map engine-local ids (``ruleset:node:17``) to ledger contribution ids.

    The mapping is explicit: skill ledger rows carry ``raw.node_id`` and are
    indexed by ``skill_node_index``; it never truncates strings or matches
    trailing digits of unrelated ids.
    """

    text = _s(source_id)
    match = re.match(r"^ruleset:node:(\d+)$", text)
    if match:
        resolved = _skill_source_for(node_index, match.group(1), stat_preferences)
        return [resolved] if resolved else []
    if text == "ruleset:attribute:intelligence":
        return ["derived:intelligence-scaling"]
    return [text] if text else []


def _row_detail(row: dict) -> str:
    value = _num(row.get("value"))
    if value is None:
        return ""
    tags = "、".join(_s(tag) for tag in _l(row.get("damage_tags")))
    suffix = f"（{tags}）" if tags else ""
    return f"{_signed_fraction(value)}{suffix}"


def _scan_usage(
    result: dict,
    by_id: dict[str, dict],
    node_index: dict[str, dict[str, str]],
    rules_by_id: dict[str, dict],
) -> dict[str, list[dict]]:
    usage: dict[str, list[dict]] = {}

    def add(source_id: str | None, scope: str, role: str, detail: str = "", rule_id: str | None = None) -> None:
        if not source_id or source_id not in by_id:
            return
        entry = {
            "scope": scope,
            "label": _BRANCH_LABELS.get(scope, scope),
            "role": role,
            "detail": detail,
            "rule_id": rule_id,
        }
        bucket = usage.setdefault(source_id, [])
        if entry not in bucket:
            bucket.append(entry)

    single = _d(result.get("single_cast"))
    body = _d(single.get("body"))
    ignite = _d(single.get("ignite"))

    for component in _l(body.get("components")):
        if not isinstance(component, dict):
            continue
        damage_type = _s(component.get("damage_type")).lower()
        scope = f"body-{damage_type}" if damage_type in ("fire", "cold") else "body"
        increased = _d(component.get("increased"))
        for row in _l(increased.get("rows")):
            if isinstance(row, dict):
                add(_s(row.get("contribution_id")), scope, "加算增伤", _row_detail(row))
        if _d(increased.get("attribute_row")):
            attribute_row = _d(increased.get("attribute_row"))
            add(
                "derived:intelligence-scaling",
                scope,
                "加算增伤（属性缩放）",
                f"智力 44 点 × 每点 +4% = {_signed_fraction(_num(attribute_row.get('value')))}",
            )
        for group in _l(component.get("more_groups")):
            if not isinstance(group, dict):
                continue
            factor = _num(group.get("factor"))
            for row in _l(group.get("rows")):
                if not isinstance(row, dict):
                    continue
                raw_id = _s(row.get("contribution_id"))
                resolved = [raw_id] if raw_id in by_id else _resolve_ruleset_rule(raw_id, node_index, ("damage", "more_damage"))
                for candidate in resolved:
                    add(
                        candidate,
                        scope,
                        f"独立增伤 ×{units.fmt_compact(factor, 'factor').lstrip('×')}",
                        _row_detail(row),
                        _s(row.get("rule_id")) or None,
                    )
        for row in _l(_d(component.get("penetration")).get("rows")):
            if isinstance(row, dict):
                add(_s(row.get("contribution_id")), scope, "火焰穿透池", _row_detail(row))
        for row in _l(_d(body.get("duration")).get("rows")):
            if not isinstance(row, dict):
                continue
            for source_id in _resolve_ruleset_rule(_s(row.get("source")), node_index, ("skill_duration",)):
                add(source_id, "mechanism:bh-duration", "黑洞持续时间", _signed_fraction(_num(row.get("value"))))

    scaling = _d(ignite.get("scaling"))
    for row in _l(scaling.get("increased_rows")):
        if isinstance(row, dict):
            add(_s(row.get("contribution_id")), "ignite", "点燃加算增伤", _row_detail(row))
    if _d(scaling.get("attribute_row")):
        attribute_row = _d(scaling.get("attribute_row"))
        add(
            "derived:intelligence-scaling",
            "ignite",
            "点燃加算增伤（属性缩放）",
            f"智力 44 点 × 每点 +4% = {_signed_fraction(_num(attribute_row.get('value')))}",
        )
    for group in _l(scaling.get("more_groups")):
        if not isinstance(group, dict):
            continue
        factor = _num(group.get("factor"))
        for row in _l(group.get("rows")):
            if not isinstance(row, dict):
                continue
            raw_id = _s(row.get("contribution_id"))
            resolved = [raw_id] if raw_id in by_id else _resolve_ruleset_rule(raw_id, node_index, ("damage", "more_damage"))
            for candidate in resolved:
                add(
                    candidate,
                    "ignite",
                    f"点燃独立增伤 ×{units.fmt_compact(factor, 'factor').lstrip('×')}",
                    _row_detail(row),
                    _s(row.get("rule_id")) or None,
                )
    for row in _l(_d(scaling.get("penetration")).get("rows")):
        if isinstance(row, dict):
            add(_s(row.get("contribution_id")), "ignite", "火焰穿透池（点燃共用）", _row_detail(row))
    for row in _l(scaling.get("penetration_with_ignite_rows")):
        if isinstance(row, dict):
            add(_s(row.get("contribution_id")), "ignite", "仅点燃火焰穿透", _row_detail(row))
    for row in _l(_d(ignite.get("duration")).get("rows")):
        if isinstance(row, dict):
            add(_s(row.get("contribution_id")), "mechanism:ignite-duration", "点燃持续时间", _row_detail(row))

    for ref in _l(_p(result, "snapshot", "derived", "flat_added_spell_damage_rows")):
        source_id = _s(ref)
        source = _d(by_id.get(source_id))
        for component in _l(body.get("components")):
            if not isinstance(component, dict):
                continue
            damage_type = _s(component.get("damage_type")).lower()
            scope = f"body-{damage_type}" if damage_type in ("fire", "cold") else "body"
            add(
                source_id,
                scope,
                "附加点伤（×点伤效用）",
                f"{units.fmt_compact(_num(source.get('value')), 'number')} 点",
            )

    heat_wave = _skill_source_for(node_index, "6", ("ailment_chance",))
    if heat_wave:
        add(heat_wave, "mechanism:heat-wave", "Heat Wave 点燃施加", "5 点 · 每次判定期望新增 47.447 层")

    for row in _l(_d(result.get("cast")).get("rows")):
        if isinstance(row, dict):
            add(_s(row.get("contribution_id")), "mechanism:cast-time", "施法时间", _row_detail(row))
    cooldown = _d(result.get("cooldown"))
    for key, role in (
        ("icr_rows", "冷却恢复速度"),
        ("cdr_rows", "冷却缩减"),
    ):
        for row in _l(cooldown.get(key)):
            if not isinstance(row, dict):
                continue
            if row.get("contribution_id"):
                add(_s(row.get("contribution_id")), "mechanism:cooldown", role, _row_detail(row))
            else:
                prefs = ("cooldown_reduction",) if key == "cdr_rows" else ("cooldown_recovery_speed",)
                for source_id in _resolve_ruleset_rule(_s(row.get("source")), node_index, prefs):
                    add(source_id, "mechanism:cooldown", role, _signed_fraction(_num(row.get("value"))))
    for row in _l(cooldown.get("flat_rows")):
        if not isinstance(row, dict):
            continue
        for source_id in _resolve_ruleset_rule(_s(row.get("source")), node_index, ("cooldown_flat",)):
            add(source_id, "mechanism:cooldown", "冷却固定调整", _signed_fraction(_num(row.get("value"))))
    for row in _l(cooldown.get("charge_rows")):
        if not isinstance(row, dict):
            continue
        for source_id in _resolve_ruleset_rule(_s(row.get("source")), node_index, ("charges",)):
            add(source_id, "mechanism:charges", "最大充能", units.fmt_compact(_num(row.get("value")), "points"))

    for source_id in list(by_id.keys()):
        if not source_id.endswith(":doubled"):
            continue
        base_id = source_id[: -len(":doubled")]
        for entry in usage.get(base_id, []):
            add(
                source_id,
                entry["scope"],
                "同词缀翻倍条件已满足 · " + entry["role"],
                entry.get("detail") or "",
                entry.get("rule_id"),
            )

    for source in list(by_id.values()):
        advanced = _d(source.get("advanced"))
        stat_key = _s(advanced.get("stat_key"))
        if stat_key == "resistance" and source.get("included"):
            target = _s(advanced.get("target"))
            if target in ("fire", "all", "elemental"):
                add(
                    _s(source.get("id")),
                    "mechanism:heat-wave",
                    "未封顶火焰抗性池",
                    f"抗性 {units.signed_pct(_num(source.get('value')))}",
                )
        if stat_key in ("attribute", "all_attributes") and source.get("included"):
            target = _s(advanced.get("target")) or _s(source.get("name"))
            if "intelligence" in target.lower() or "intelligence" in _s(advanced.get("stat_name")).lower() or stat_key == "all_attributes":
                add(_s(source.get("id")), "attribute:intelligence", "属性池 → 智力", _row_detail(source))
    return usage


def _attach_usage(sources_list: list[dict], usage: dict[str, list[dict]]) -> None:
    for source in sources_list:
        entries = usage.get(_s(source.get("id")), [])
        source["applies"] = [
            {
                **entry,
                "label": clean_engineering_text(entry.get("label")),
                "detail": clean_engineering_text(entry.get("detail")),
            }
            for entry in entries
        ]


# --------------------------------------------------------------------------- #
# factor tables shared by composition / ignite pages
# --------------------------------------------------------------------------- #


def _inc_table(
    rows: Iterable[dict],
    attribute_row: dict | None,
    total: float | None,
    by_id: dict[str, dict],
) -> dict:
    entries: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        source_id = _s(row.get("contribution_id"))
        source = _d(by_id.get(source_id))
        value = _num(row.get("value"))
        entries.append(
            {
                "source_id": source_id,
                "name": source.get("display_name") or _s(row.get("label")),
                "value": value,
                "kind": "fraction",
                "text": _signed_fraction(value),
                "tags": [_s(tag) for tag in _l(row.get("damage_tags"))],
                "status": _row_status(row),
                "group": _s(row.get("group")) or None,
            }
        )
    attribute_entry = None
    if isinstance(attribute_row, dict) and _num(attribute_row.get("value")) is not None:
        value = _num(attribute_row.get("value"))
        attribute_entry = {
            "source_id": "derived:intelligence-scaling",
            "name": "智力 44 点 · 每点 +4%",
            "value": value,
            "kind": "fraction",
            "text": _signed_fraction(value),
            "tags": [],
            "status": _row_status(attribute_row),
            "group": None,
        }
        entries.append(attribute_entry)
    groups: dict[str, dict] = {}
    for entry in entries:
        category = _d(by_id.get(entry["source_id"])).get("category") or "unknown"
        group = groups.setdefault(
            category,
            {
                "category": category,
                "category_label": labels.CATEGORY_LABELS.get(category, "其他来源"),
                "subtotal": 0.0,
                "rows": [],
            },
        )
        group["rows"].append(entry)
        if entry["value"] is not None:
            group["subtotal"] += entry["value"]
    ordered_groups = sorted(
        groups.values(),
        key=lambda group: labels.CATEGORY_ORDER.index(group["category"])
        if group["category"] in labels.CATEGORY_ORDER
        else 99,
    )
    row_sum = sum(entry["value"] for entry in entries if entry["value"] is not None)
    total_increase = total if total is not None else row_sum
    return {
        "entries": entries,
        "groups": ordered_groups,
        "attribute_entry": attribute_entry,
        "rows_total": row_sum,
        "total": total_increase,
        "closed": _checks_close(total_increase, *[e["value"] for e in entries if e["value"] is not None]),
        "factor": 1.0 + total_increase if total_increase is not None else None,
        "count": len(entries),
    }


def _more_table(
    groups: Iterable[dict],
    by_id: dict[str, dict],
    rules_by_id: dict[str, dict],
    branch: str,
    node_index: dict[str, dict[str, str]] | None = None,
) -> dict:
    entries: list[dict] = []
    product = 1.0
    node_index = node_index or {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        factor = _num(group.get("factor"))
        group_id = _s(group.get("group"))
        rows = [row for row in _l(group.get("rows")) if isinstance(row, dict)]
        source_ids: list[str] = []
        for row in rows:
            raw_id = _s(row.get("contribution_id"))
            resolved = [raw_id] if raw_id in by_id else _resolve_ruleset_rule(raw_id, node_index, ("damage", "more_damage"))
            for candidate in resolved:
                if candidate and candidate not in source_ids:
                    source_ids.append(candidate)
        primary = _d(by_id.get(source_ids[0])) if source_ids else {}
        name = _s(primary.get("name"))
        for row in rows:
            label = _s(row.get("label"))
            if label and label.lower() not in ("damage", "more") and not name:
                name = label
        if not name:
            name = group_id or "独立增伤"
        points = primary.get("points")
        if points is None and group_id:
            # structural more groups (e.g. Binary System) have no own damage
            # ledger row; recover the allocated points from the node's other
            # ledger rows so the table can still show points -> per-point effect
            for candidate in by_id.values():
                if candidate.get("group") == group_id:
                    points = candidate.get("points")
                    break
        value = _num(group.get("value"))
        rule_id = _s(rows[0].get("rule_id")) if rows else ""
        rule = _d(rules_by_id.get(rule_id))
        per_point = None
        if points and value is not None and points > 0:
            per_point = value / points
        condition = _MORE_CONDITIONS.get(group_id)
        if not condition:
            condition = _s(rule.get("title")) or "已分配点数下生效"
        entries.append(
            {
                "group": group_id,
                "name": name,
                "factor": factor,
                "value": value,
                "text": units.fmt_compact(factor, "factor"),
                "increase_text": _signed_fraction(value),
                "points": points,
                "per_point": per_point,
                "per_point_text": _signed_fraction(per_point) if per_point is not None else "",
                "condition": condition,
                "rule_id": rule_id or None,
                "rule_title": _s(rule.get("title")),
                "rule_statement": _s(rule.get("statement")),
                "source_ids": source_ids,
                "branch": branch,
            }
        )
        if factor is not None:
            product *= factor
    return {"entries": entries, "product": product, "count": len(entries)}


def _pen_table(
    rows: Iterable[dict],
    by_id: dict[str, dict],
    target: dict,
    *,
    extra_rows: Iterable[dict] | None = None,
) -> dict:
    entries = []
    for row in list(rows) + list(extra_rows or []):
        if not isinstance(row, dict):
            continue
        source_id = _s(row.get("contribution_id"))
        source = _d(by_id.get(source_id))
        entries.append(
            {
                "source_id": source_id,
                "name": source.get("display_name") or _s(row.get("label")),
                "value": _num(row.get("value")),
                "text": _signed_fraction(row.get("value")),
            }
        )
    target = _d(target)
    return {
        "entries": entries,
        "total": _num(target.get("penetration")),
        "target_resistance": _num(target.get("target_resistance")),
        "effective_resistance": _num(target.get("effective_resistance")),
        "resistance_multiplier": _num(target.get("resistance_multiplier")),
        "monster_dr": _num(target.get("monster_damage_reduction")),
        "dr_factor": _num(target.get("monster_dr_factor")),
        "multiplier": _num(target.get("multiplier")),
    }


# --------------------------------------------------------------------------- #
# composition page data
# --------------------------------------------------------------------------- #


def _build_composition(
    result: dict,
    by_id: dict[str, dict],
    node_index: dict[str, dict[str, str]],
    rules_by_id: dict[str, dict],
) -> dict:
    single = _d(result.get("single_cast"))
    body = _d(single.get("body"))
    ignite = _d(single.get("ignite"))
    totals = _d(single.get("totals"))
    components_view: list[dict] = []
    for component in _l(body.get("components")):
        if not isinstance(component, dict):
            continue
        damage_type = _s(component.get("damage_type"))
        key = "fire" if damage_type.lower() == "fire" else "cold" if damage_type.lower() == "cold" else damage_type.lower()
        label_text = "火焰本体" if key == "fire" else "冰霜本体" if key == "cold" else f"{damage_type} 本体"
        increased = _d(component.get("increased"))
        inc_view = _inc_table(
            _l(increased.get("rows")),
            _d(increased.get("attribute_row")) or None,
            _num(increased.get("factor")) - 1.0 if _num(increased.get("factor")) is not None else None,
            by_id,
        )
        more_view = _more_table(_l(component.get("more_groups")), by_id, rules_by_id, f"body-{key}", node_index)
        pen_view = _pen_table(
            _l(_d(component.get("penetration")).get("rows")),
            by_id,
            _d(component.get("target")),
        )
        ticks = _l(component.get("ticks"))
        base_value = _num(component.get("base_value"))
        added_total = _num(component.get("added_damage_total"))
        added_scaling = _num(component.get("added_damage_scaling"))
        added_effective = None
        if added_total is not None and added_scaling is not None:
            added_effective = added_total * added_scaling
        added_sources = []
        expression = _d(component.get("expression"))
        for child in _l(expression.get("children")):
            if not isinstance(child, dict):
                continue
            for sub in _l(child.get("children")) or [child]:
                if not isinstance(sub, dict):
                    continue
                label_text_sub = _s(sub.get("label"))
                if "附加" in label_text_sub or _s(sub.get("node_id")).endswith(":added"):
                    for ref in _l(sub.get("source_refs")):
                        ref = _s(ref)
                        source = _d(by_id.get(ref))
                        if ref and source:
                            added_sources.append(
                                {
                                    "source_id": ref,
                                    "name": source.get("display_name"),
                                    "value": _num(source.get("value")),
                                    "kind": source.get("kind"),
                                }
                            )
        if not added_sources:
            for ref in _l(_p(result, "snapshot", "derived", "flat_added_spell_damage_rows")):
                ref = _s(ref)
                source = _d(by_id.get(ref))
                if source:
                    added_sources.append(
                        {
                            "source_id": ref,
                            "name": source.get("display_name"),
                            "value": _num(source.get("value")),
                            "kind": source.get("kind"),
                        }
                    )
        steps = [
            {
                "key": "base",
                "label": "基础伤害与点伤",
                "value": _num(component.get("per_tick_base")),
                "kind": "damage",
                "text": units.fmt_compact(_num(component.get("per_tick_base")), "damage"),
                "formula": "技能基础值 + 附加点伤 × 点伤效用",
                "parts": [
                    {"label": "技能基础值", "value": base_value, "kind": "damage"},
                    {"label": "装备/被动附加点伤", "value": added_total, "kind": "number"},
                    {"label": "点伤效用", "value": added_scaling, "kind": "factor"},
                ],
                "detail": {
                    "added_effective": added_effective,
                    "sources": added_sources,
                    "note": "附加点伤逐条来自装备与被动；点伤效用由技能定义给出（本模型 240%）。",
                },
            },
            {
                "key": "inc",
                "label": "加算增伤",
                "value": inc_view,
                "kind": "factor",
                "text": units.fmt_compact(inc_view.get("factor"), "factor"),
                "formula": "1 + 加算增伤合计",
            },
            {
                "key": "more",
                "label": "独立增伤",
                "value": more_view,
                "kind": "factor",
                "text": units.fmt_compact(more_view.get("product"), "factor"),
                "formula": "各独立因子相乘",
            },
            {
                "key": "pen",
                "label": "抗性与穿透",
                "value": pen_view,
                "kind": "factor",
                "text": units.fmt_compact(pen_view.get("resistance_multiplier"), "factor"),
                "formula": "1 −（目标抗性 − 穿透）",
            },
            {
                "key": "dr",
                "label": "怪物减伤",
                "value": pen_view.get("dr_factor"),
                "kind": "factor",
                "text": units.fmt_compact(pen_view.get("dr_factor"), "factor"),
                "formula": "1 − 怪物 DR",
            },
        ]
        components_view.append(
            {
                "key": key,
                "label": label_text,
                "damage_type": damage_type,
                "color": key if key in ("fire", "cold") else "body",
                "component_id": _s(component.get("component_id")),
                "per_tick_base": _num(component.get("per_tick_base")),
                "per_tick": _num(component.get("per_tick_damage")),
                "tick_count": _num(component.get("tick_count")),
                "total": _num(component.get("damage_total")),
                "target": _d(component.get("target")),
                "formula_text": "{base} × {inc} × {more} × {pen} × {dr} = {tick} / 每次结算".format(
                    base=units.fmt_compact(_num(component.get("per_tick_base")), "damage"),
                    inc=units.fmt_compact(inc_view.get("factor"), "factor").lstrip("×"),
                    more=units.fmt_compact(more_view.get("product"), "factor").lstrip("×"),
                    pen=units.fmt_compact(pen_view.get("resistance_multiplier"), "factor").lstrip("×"),
                    dr=units.fmt_compact(pen_view.get("dr_factor"), "factor").lstrip("×"),
                    tick=units.fmt_compact(_num(component.get("per_tick_damage")), "damage"),
                ),
                "steps": steps,
                "inc": inc_view,
                "more": more_view,
                "pen": pen_view,
                "tick_preview": ticks[:3],
                "source_flags": [_s(flag) for flag in _l(component.get("source_flags"))],
            }
        )

    ignite_view = _build_ignite_branch(result, by_id, node_index)
    body_total = _num(totals.get("body_total"))
    if body_total is None:
        body_total = sum(part["total"] or 0.0 for part in components_view)
    ignite_total = _num(totals.get("ignite_total")) or _num(_d(ignite.get("totals")).get("damage"))
    single_total = _num(totals.get("single_cast_total"))
    bar = []
    palette = {"fire": "fire", "cold": "cold", "body": "body"}
    for component in components_view:
        bar.append(
            {
                "key": component["key"],
                "label": component["label"],
                "value": component["total"],
                "color": palette.get(component["key"], "body"),
            }
        )
    bar.append({"key": "ignite", "label": "点燃", "value": ignite_total, "color": "ignite"})
    for item in bar:
        item["share"] = (item["value"] / single_total) if (item["value"] is not None and single_total) else None
    return {
        "components": components_view,
        "ignite": ignite_view,
        "bar": bar,
        "body_total": body_total,
        "ignite_total": ignite_total,
        "single_total": single_total,
        "tick_count": _num(body.get("tick_count")),
        "tick_interval_s": _num(body.get("tick_interval_s")),
        "duration_s": _num(_p(body, "duration", "duration_s")),
        "duration_increased": _num(_p(body, "duration", "increased_total")),
        "duration_base_s": _num(_p(body, "duration", "base_duration_s")),
        "dr": _num(_p(components_view[0], "pen", "dr") if components_view else None),
        "dr_factor": _num(_p(components_view[0], "pen", "dr_factor") if components_view else None),
    }


def _build_ignite_branch(result: dict, by_id: dict[str, dict], node_index: dict[str, dict[str, str]] | None = None) -> dict:
    ignite = _d(_p(result, "single_cast", "ignite"))
    scaling = _d(ignite.get("scaling"))
    base_damage = _num(ignite.get("base_damage"))
    base_duration = _num(ignite.get("base_duration_s"))
    base_dps = (base_damage / base_duration) if (base_damage is not None and base_duration) else None
    inc_factor = _num(ignite.get("increased_factor"))
    inc_total = _num(ignite.get("increased_total"))
    more_product = _num(ignite.get("more_product"))
    target = _d(ignite.get("target"))
    pen_total = _num(target.get("penetration"))
    res_multiplier = _num(target.get("resistance_multiplier"))
    dr_factor = _num(target.get("monster_dr_factor"))
    per_stack_dps = _num(ignite.get("per_stack_dps"))
    rules_by_id = _rules_by_id(result)
    inc_rows = _l(scaling.get("increased_rows"))
    inc_view = _inc_table(inc_rows, _d(scaling.get("attribute_row")) or None, inc_total, by_id)
    more_view = _more_table(_l(scaling.get("more_groups")), by_id, rules_by_id, "ignite", node_index)
    pen_view = _pen_table(
        _l(_d(scaling.get("penetration")).get("rows")),
        by_id,
        target,
        extra_rows=_l(_d(scaling.get("penetration")).get("penetration_with_ignite_rows")),
    )
    return {
        "key": "ignite",
        "label": "点燃单层",
        "damage_type": _s(ignite.get("damage_type"), "Fire"),
        "ailment": _s(ignite.get("ailment"), "Ignite"),
        "base_damage": base_damage,
        "base_duration_s": base_duration,
        "base_dps": base_dps,
        "inc_factor": inc_factor,
        "inc_total": inc_total,
        "inc": inc_view,
        "more": more_view,
        "more_product": more_product,
        "pen": pen_view,
        "penetration": pen_total,
        "resistance_multiplier": res_multiplier,
        "dr": _num(target.get("monster_damage_reduction")),
        "dr_factor": dr_factor,
        "per_stack_dps": per_stack_dps,
        "per_stack_total": _num(ignite.get("per_stack_total")),
        "duration_s": _num(ignite.get("effective_duration_s")),
        "duration_rows": _duration_entries(result, by_id),
        "formula_text": "{base} × {inc} × {more} × {res} × {dr} = {dps} / 秒 / 层".format(
            base=units.fmt_compact(base_dps, "damage"),
            inc=units.fmt_compact(inc_factor, "factor").lstrip("×"),
            more=units.fmt_compact(more_product, "factor").lstrip("×"),
            res=units.fmt_compact(res_multiplier, "factor").lstrip("×"),
            dr=units.fmt_compact(dr_factor, "factor").lstrip("×"),
            dps=units.fmt_compact(per_stack_dps, "damage"),
        ),
        "note": "持续时间不进入这一行；单层每秒伤害与持续时间、层数分开计算。",
    }


def _duration_entries(result: dict, by_id: dict[str, dict]) -> list[dict]:
    entries = []
    for row in _l(_p(result, "single_cast", "ignite", "duration", "rows")):
        if not isinstance(row, dict):
            continue
        source_id = _s(row.get("contribution_id"))
        source = _d(by_id.get(source_id))
        entries.append(
            {
                "source_id": source_id,
                "name": source.get("display_name") or _s(row.get("label")),
                "value": _num(row.get("value")),
                "text": _signed_fraction(row.get("value")),
                "raw_text": source.get("raw_text"),
                "imported_display": source.get("imported_display"),
                "refracted": "idols." in source_id,
            }
        )
    return entries


# --------------------------------------------------------------------------- #
# ignite page data
# --------------------------------------------------------------------------- #


def _build_ignite(
    result: dict,
    by_id: dict[str, dict],
    rules_by_id: dict[str, dict],
    node_index: dict[str, dict[str, str]],
) -> dict:
    single = _d(result.get("single_cast"))
    ignite = _d(single.get("ignite"))
    application = _d(ignite.get("application"))
    duration = _d(ignite.get("duration"))
    totals = _d(ignite.get("totals"))
    cohorts_raw = [row for row in _l(ignite.get("cohorts")) if isinstance(row, dict)]
    bh_end = _num(_p(single, "cast", "black_hole_end_s")) or 0.0

    cohorts = []
    before_sum = 0.0
    tail_sum = 0.0
    for row in cohorts_raw:
        time_s = _num(row.get("time_s")) or 0.0
        end_s = _num(row.get("end_time_s")) or 0.0
        dps = _num(row.get("dps")) or 0.0
        damage = _num(row.get("damage_total")) or 0.0
        before = dps * max(0.0, min(end_s, bh_end) - time_s)
        tail = dps * max(0.0, end_s - max(time_s, bh_end))
        before_sum += before
        tail_sum += tail
        cohorts.append(
            {
                "index": int(_num(row.get("roll_index")) or len(cohorts)) + 1,
                "time_s": time_s,
                "stacks": _num(row.get("stacks")),
                "duration_s": _num(row.get("duration_s")),
                "expiry_s": end_s,
                "dps": dps,
                "damage": damage,
                "before_bh_end": before,
                "tail": tail,
                "damage_text": units.fmt_compact(damage, "damage"),
            }
        )
    cohort_damage_sum = sum(row["damage"] for row in cohorts)
    total_damage = _num(totals.get("damage")) or cohort_damage_sum

    stacks_curve = _stacks_changes(cohorts)
    peak_stacks = 0.0
    peak_time = 0.0
    for point in stacks_curve:
        if point["stacks"] > peak_stacks:
            peak_stacks = point["stacks"]
            peak_time = point["t"]

    pool = _fire_resistance_pool(result, by_id)
    heat_wave_id = _skill_source_for(node_index, "6", ("ailment_chance",))
    heat_wave_source = _d(by_id.get(heat_wave_id)) if heat_wave_id else {}
    declared_points = _num(application.get("points"))

    rule_ids = [
        "rule-ignite-heat-wave",
        "rule-ignite-heat-wave-timing",
        "rule-ignite-base",
        "rule-derived-ignite-damage",
        "rule-ignite-duration",
        "rule-ignite-inheritance",
        "rule-ignite-binary-excluded",
        "rule-ignite-elemental-tag",
    ]
    rules = []
    for rule_id in rule_ids:
        rule = _d(rules_by_id.get(rule_id))
        if rule:
            rules.append(
                {
                    "id": rule_id,
                    "title": _s(rule.get("title")),
                    "statement": _s(rule.get("statement")),
                    "status": _s(rule.get("status")),
                    "status_label": labels.RULE_STATUS_LABELS.get(_s(rule.get("status")), ""),
                    "evidence": _trim_evidence(_l(rule.get("evidence"))),
                }
            )

    inheritance = _d(_p(ignite, "scaling", "inheritance"))
    excluded_reasons = _d(inheritance.get("excluded_reasons"))
    node_names = source_catalog.collect_skill_node_names(result)
    exclusions = []
    for node, reason in excluded_reasons.items():
        node_text = _s(node)
        exclusions.append(
            {"node": node_text, "name": node_names.get(node_text, ""), "reason": _s(reason)}
        )
    for node in _l(inheritance.get("exclude_tree_nodes")):
        node_text = _s(node)
        if not any(item["node"] == node_text for item in exclusions):
            exclusions.append(
                {
                    "node": node_text,
                    "name": node_names.get(node_text, ""),
                    "reason": "未继承（规则排除）",
                }
            )

    before_end = _num(totals.get("damage_before_black_hole_end"))
    tail = _num(totals.get("tail_damage"))
    per_stack_total = _num(ignite.get("per_stack_total"))
    total_stacks = _num(totals.get("expected_total_stacks_applied")) or _num(application.get("expected_stacks_applied"))

    return {
        "application": {
            "uncapped_fire_resistance": _num(application.get("uncapped_fire_resistance")),
            "pool": pool,
            "points": declared_points,
            "rule": _s(application.get("rule")),
            "per_point_percent": _num(application.get("chance_per_point_per_uncapped_fire_res")),
            "chance_per_second_percent": _num(application.get("chance_per_second_percent")),
            "per_second": _num(application.get("expected_stacks_per_second")),
            "per_roll": _num(application.get("expected_stacks_per_roll")),
            "roll_interval_s": _num(application.get("roll_interval_s")),
            "roll_count": _num(application.get("roll_count")),
            "total_stacks": total_stacks,
            "first_roll_at": _s(application.get("first_roll_at")),
            "first_roll_s": _num(_p(single, "cast", "black_hole_spawn_s")),
            "last_roll_s": max((_num(value) or 0.0) for value in _l(application.get("roll_times_s"))) if application else None,
            "expected_value_model": bool(application.get("expected_value_model")),
            "heat_wave_source_id": heat_wave_id,
            "heat_wave_name": (
                "Heat Wave"
                if _s(application.get("rule")) == "heat_wave"
                else (heat_wave_source.get("name") or "Heat Wave")
            ),
            "heat_wave_points": _num(heat_wave_source.get("points")) or declared_points,
            "peak_stacks": peak_stacks,
            "peak_time_s": peak_time,
            "stack_curve": stacks_curve,
        },
        "per_stack": _build_ignite_branch(result, by_id, node_index),
        "duration": {
            "base_s": _num(duration.get("base_duration_s")),
            "increased_total": _num(duration.get("increased_total")),
            "multiplier": _num(duration.get("duration_multiplier")),
            "duration_s": _num(duration.get("duration_s")),
            "rows": _duration_entries(result, by_id),
            "per_stack_total": per_stack_total,
        },
        "totals": {
            "stacks": total_stacks,
            "per_stack_total": per_stack_total,
            "damage": total_damage,
            "before_bh_end": before_end,
            "tail": tail,
            "cohorts": cohorts,
            "before_sum": before_sum,
            "tail_sum": tail_sum,
            "cohort_damage_sum": cohort_damage_sum,
            "closed_damage": _checks_close(total_damage, cohort_damage_sum),
            "closed_split": _checks_close(total_damage, before_end or 0.0, tail or 0.0),
            "closed_before": _checks_close(before_end or 0.0, before_sum),
            "closed_tail": _checks_close(tail or 0.0, tail_sum),
            "formula_text": "每批层数 × 该批单层完整伤害，逐批相加",
            "simple_formula": "{stacks} 层 × {per} = {total}".format(
                stacks=units.fmt_compact(total_stacks, "stacks"),
                per=units.fmt_compact(per_stack_total, "damage"),
                total=units.fmt_compact(total_damage, "damage"),
            ),
        },
        "rules": rules,
        "exclusions": exclusions,
        "inheritance_note": _s(inheritance.get("note")),
        "inheritance_status": _s(inheritance.get("status")),
    }


def _fire_resistance_pool(result: dict, by_id: dict[str, dict]) -> dict:
    fire = _d(_p(result, "snapshot", "resistances", "fire"))
    groups = {"all": [], "elemental": [], "fire": []}
    for source in by_id.values():
        advanced = _d(source.get("advanced"))
        if advanced.get("stat_key") != "resistance" or not source.get("included"):
            continue
        target = _s(advanced.get("target"))
        if target in groups:
            groups[target].append(
                {
                    "source_id": source.get("id"),
                    "name": source.get("display_name"),
                    "value": _num(source.get("value")),
                    "text": units.signed_pct(_num(source.get("value"))),
                }
            )
    entries = []
    for key, label_text in (("all", "全抗性"), ("elemental", "元素抗性"), ("fire", "火焰抗性")):
        rows = groups[key]
        subtotal = sum((row["value"] or 0.0) for row in rows) * 100.0
        entries.append({"key": key, "label": label_text, "rows": rows, "subtotal": subtotal})
    total = _num(fire.get("total"))
    derived_total = sum(item["subtotal"] for item in entries)
    return {
        "groups": entries,
        "total": total or derived_total,
        "derived_total": derived_total,
        "closed": _checks_close(total or derived_total, derived_total, rel=1e-6),
        "cap": _num(fire.get("cap")),
        "capped": _num(fire.get("capped")),
        "overcapped": _num(fire.get("overcapped")),
        "breakdown": _d(fire.get("breakdown")),
    }


def _stacks_changes(cohorts: list[dict]) -> list[dict]:
    changes: list[tuple[float, float]] = []
    for cohort in cohorts:
        changes.append((cohort["time_s"], cohort["stacks"] or 0.0))
        changes.append((cohort["expiry_s"], -(cohort["stacks"] or 0.0)))
    changes.sort(key=lambda item: item[0])
    curve: list[dict] = []
    current = 0.0
    index = 0
    while index < len(changes):
        time_s = changes[index][0]
        delta = 0.0
        while index < len(changes) and changes[index][0] == time_s:
            delta += changes[index][1]
            index += 1
        current += delta
        curve.append({"t": time_s, "stacks": current})
    return curve


# --------------------------------------------------------------------------- #
# timeline page data
# --------------------------------------------------------------------------- #


def _trim_evidence(evidence: Iterable[Any]) -> list[dict]:
    entries = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        entries.append(
            {
                "kind": _s(item.get("kind")),
                "kind_label": labels.EVIDENCE_LABELS.get(_s(item.get("kind")), _s(item.get("kind"))),
                "detail": _s(item.get("detail"))[:700],
                "locator": _s(item.get("locator"))[:400],
            }
        )
    return entries


def _body_ticks(single: dict) -> list[dict]:
    ticks = []
    for event in _l(single.get("event_trace")):
        if not isinstance(event, dict) or event.get("kind") != "body_tick":
            continue
        ticks.append(
            {
                "t": _num(event.get("t")) or 0.0,
                "amount": _num(event.get("amount")) or 0.0,
                "component_id": _s(event.get("component_id")),
                "damage_type": _s(event.get("damage_type")),
            }
        )
    ticks.sort(key=lambda item: item["t"])
    return ticks


def _ignite_overlap(cohorts: list[dict], start: float, end: float) -> float:
    total = 0.0
    for cohort in cohorts:
        cohort_start = cohort["time_s"]
        cohort_end = cohort["expiry_s"]
        overlap = max(0.0, min(end, cohort_end) - max(start, cohort_start))
        total += (cohort["dps"] or 0.0) * overlap
    return total


def _build_single_timeline(result: dict) -> dict:
    single = _d(result.get("single_cast"))
    cast = _d(single.get("cast"))
    totals = _d(single.get("totals"))
    ignite = _d(single.get("ignite"))
    body = _d(single.get("body"))
    end_s = _num(totals.get("observation_end_s")) or _num(totals.get("last_ailment_expiry_s")) or 0.0
    bh_end = _num(cast.get("black_hole_end_s")) or 0.0
    spawn = _num(cast.get("black_hole_spawn_s")) or 0.0

    ticks = _body_ticks(single)
    cohorts = []
    for row in _l(ignite.get("cohorts")):
        if not isinstance(row, dict):
            continue
        cohorts.append(
            {
                "time_s": _num(row.get("time_s")) or 0.0,
                "expiry_s": _num(row.get("end_time_s")) or 0.0,
                "dps": _num(row.get("dps")) or 0.0,
                "stacks": _num(row.get("stacks")) or 0.0,
                "damage": _num(row.get("damage_total")) or 0.0,
            }
        )

    tick_times = [tick["t"] for tick in ticks]
    tick_cum = []
    running = 0.0
    for tick in ticks:
        running += tick["amount"]
        tick_cum.append(running)

    breakpoints = {0.0, end_s, spawn, bh_end}
    for tick in ticks:
        breakpoints.add(round(tick["t"], 6))
    for cohort in cohorts:
        breakpoints.add(round(cohort["time_s"], 6))
        breakpoints.add(round(cohort["expiry_s"], 6))
    times = sorted(point for point in breakpoints if 0.0 <= point <= max(end_s, 0.0))

    curve_t: list[float] = []
    curve_body: list[float] = []
    curve_ignite: list[float] = []
    curve_total: list[float] = []
    for time_s in times:
        pos = bisect.bisect_right(tick_times, time_s)
        body_cum = tick_cum[pos - 1] if pos > 0 else 0.0
        ignite_cum = _ignite_overlap(cohorts, 0.0, time_s)
        curve_t.append(round(time_s, 6))
        curve_body.append(body_cum)
        curve_ignite.append(ignite_cum)
        curve_total.append(body_cum + ignite_cum)

    stacks_curve = _stacks_changes(cohorts)
    stacks_t = [0.0] + [point["t"] for point in stacks_curve] + [end_s]
    stacks_values = [0.0] + [point["stacks"] for point in stacks_curve] + [0.0]

    bin_width = 0.5
    bins = []
    bin_count = int(math.ceil(end_s / bin_width - 1e-9)) if end_s else 0
    for index in range(bin_count):
        start = round(index * bin_width, 6)
        finish = round(min((index + 1) * bin_width, end_s), 6)
        body_damage = sum(tick["amount"] for tick in ticks if start <= tick["t"] < finish)
        ignite_damage = _ignite_overlap(cohorts, start, finish)
        total_damage = body_damage + ignite_damage
        bins.append(
            {
                "index": index,
                "start": start,
                "end": finish,
                "width": finish - start,
                "body": body_damage,
                "ignite": ignite_damage,
                "total": total_damage,
                "dps": total_damage / (finish - start) if finish > start else 0.0,
            }
        )

    last_expiry = _num(totals.get("last_ailment_expiry_s")) or end_s
    milestones = [
        {"key": "cast_start", "t": 0.0, "label": "施法开始", "tone": "cast"},
        {"key": "bh_spawn", "t": spawn, "label": "黑洞生成", "tone": "state"},
        {"key": "bh_end", "t": bh_end, "label": "黑洞结束", "tone": "state"},
        {"key": "last_expiry", "t": last_expiry, "label": "点燃结束", "tone": "ignite"},
    ]
    milestones = [item for item in milestones if item.get("t") is not None]

    key_events = []
    for event in _l(single.get("event_trace")):
        if not isinstance(event, dict):
            continue
        if event.get("kind") not in ("cast_start", "black_hole_spawn", "black_hole_destroyed", "observation_end"):
            continue
        key_events.append(
            {
                "t": _num(event.get("t")) or 0.0,
                "kind": _s(event.get("kind")),
                "label": {
                    "cast_start": "施法开始",
                    "black_hole_spawn": "黑洞生成",
                    "black_hole_destroyed": "黑洞结束",
                    "observation_end": "点燃结束",
                }.get(_s(event.get("kind")), _s(event.get("kind"))),
                "note": _s(event.get("note")),
                "amount": _num(event.get("amount")),
            }
        )

    detail_events = []
    detail_index = 0
    for event in _l(single.get("event_trace")):
        if not isinstance(event, dict):
            continue
        kind = _s(event.get("kind"))
        if kind in ("body_tick", "ignite_tick"):
            detail_index += 1
        label = {
            "cast_start": "施法开始",
            "black_hole_spawn": "黑洞生成",
            "body_tick": f"{'火焰' if _s(event.get('damage_type')) == 'Fire' else '冰霜'}本体结算",
            "ignite_roll": "点燃施加",
            "ignite_tick": "点燃结算",
            "black_hole_destroyed": "黑洞结束",
            "ignite_cohort_expiry": "点燃批次到期",
            "observation_end": "点燃结束",
        }.get(kind, kind)
        detail_events.append(
            {
                "t": _num(event.get("t")) or 0.0,
                "kind": kind,
                "label": label,
                "amount": _num(event.get("amount")),
                "note": _s(event.get("note")),
                "damage_type": _s(event.get("damage_type")),
                "component_id": _s(event.get("component_id")),
            }
        )

    tick_sum = sum(tick["amount"] for tick in ticks)
    ignite_sum = sum(cohort["damage"] for cohort in cohorts)
    bin_body_sum = sum(b["body"] for b in bins)
    bin_ignite_sum = sum(b["ignite"] for b in bins)
    body_total = _num(_p(body, "totals", "damage")) or _num(totals.get("body_total"))
    ignite_total = _num(_p(ignite, "totals", "damage"))
    return {
        "end_s": end_s,
        "spawn_s": spawn,
        "bh_end_s": bh_end,
        "horizon_s": end_s,
        "curve": {
            "t": curve_t,
            "body": curve_body,
            "ignite": curve_ignite,
            "total": curve_total,
        },
        "stacks": {"t": stacks_t, "values": stacks_values},
        "active": [
            {"start": 0.0, "end": spawn, "value": 0.0, "label": "施法"},
            {"start": spawn, "end": bh_end, "value": 1.0, "label": "黑洞存续"},
            {"start": bh_end, "end": end_s, "value": 0.0, "label": "仅点燃尾部"},
        ],
        "bins": bins,
        "bin_width": bin_width,
        "milestones": milestones,
        "key_events": key_events,
        "detail_events": detail_events,
        "tick_count": len(ticks),
        "ignite_tick_count": sum(1 for event in detail_events if event["kind"] == "ignite_tick"),
        "cohort_count": len(cohorts),
        "sample_interval_s": 0.25,
        "precision": "exact-derived",
        "precision_note": "单次释放的累计曲线、层数阶梯与 0.5 秒分箱由引擎的本体跳数与点燃批次逐段积分推导；点燃切片事件只作记账，不作为游戏 tick。",
        "closures": {
            "body": _checks_close(body_total, tick_sum),
            "ignite": _checks_close(ignite_total, ignite_sum),
            "bin_body": _checks_close(body_total, bin_body_sum),
            "bin_ignite": _checks_close(ignite_total, bin_ignite_sum),
        },
        "body_total": body_total,
        "ignite_total": ignite_total,
        "total": _num(totals.get("single_cast_total")),
        "peak_stacks": max((point["stacks"] for point in stacks_curve), default=0.0),
    }


def _build_loop_timelines(result: dict) -> dict:
    policies: dict[str, dict] = {}
    for loop in _l(result.get("loops")):
        if not isinstance(loop, dict):
            continue
        policy_id = _s(loop.get("policy_id"))
        if not policy_id:
            continue
        series = _d(loop.get("time_series"))
        points = [row for row in _l(loop.get("time_series")) if isinstance(row, dict)]
        casts = []
        for row in _l(loop.get("casts")):
            if not isinstance(row, dict):
                continue
            casts.append(
                {
                    "t": _num(row.get("t")),
                    "spawn_s": _num(row.get("spawn_s")),
                    "end_s": _num(row.get("black_hole_end_s")),
                    "accepted": bool(row.get("accepted")),
                    "charges_before": _num(row.get("charges_before")),
                    "charges_after": _num(row.get("charges_after")),
                    "blocked_reason": _s(row.get("blocked_reason")),
                    "cast_id": _s(row.get("cast_id")),
                }
            )
        charge_events = []
        for row in _l(_p(loop, "resource", "charge_events")):
            if isinstance(row, dict):
                charge_events.append({"t": _num(row.get("t")), "charges": _num(row.get("charges")), "kind": _s(row.get("kind"))})
        policies[policy_id] = {
            "id": policy_id,
            "horizon_s": _num(loop.get("horizon_s")),
            "sampled": True,
            "sample_interval_s": 0.25,
            "event_trace_limited": bool(loop.get("event_trace_limited")),
            "series": {
                "t": [_num(row.get("t")) for row in points],
                "body": [_num(row.get("body_cum")) for row in points],
                "ignite": [_num(row.get("ignite_cum")) for row in points],
                "total": [_num(row.get("total_cum")) for row in points],
                "stacks": [_num(row.get("ignite_stacks")) for row in points],
                "active": [_num(row.get("active_black_holes")) for row in points],
                "charges": [_num(row.get("charges")) for row in points],
            },
            "casts": casts,
            "charge_events": charge_events,
            "max_charges": _num(_p(loop, "resource", "max_charges")),
            "recharge_s": _num(_p(loop, "resource", "effective_recharge_s")),
            "precision": "sampled",
            "precision_note": "CD 循环曲线来自引擎 0.25 秒采样；逐次结算事件只保留了部分记录（event_trace_limited），窗口合计以引擎的精确汇总为准。",
        }
    return policies


def _build_timeline(result: dict) -> dict:
    single = _build_single_timeline(result)
    return {
        "basis": "cast_start",
        "basis_label": "t = 0 为开始施法",
        "single": single,
        "loops": _build_loop_timelines(result),
        "windows": [5, 30, 120],
        "window_default": 30,
    }


# --------------------------------------------------------------------------- #
# validation page data
# --------------------------------------------------------------------------- #


def _build_validation(result: dict, by_id: dict[str, dict]) -> dict:
    rules = []
    for rule in _l(result.get("rule_ledger")):
        if not isinstance(rule, dict):
            continue
        status = _s(rule.get("status"))
        rules.append(
            {
                "id": _s(rule.get("rule_id")),
                "title": _s(rule.get("title")),
                "statement": _s(rule.get("statement")),
                "status": status,
                "status_label": labels.RULE_STATUS_LABELS.get(status, status),
                "status_tone": labels.RULE_STATUS_TONES.get(status, "muted"),
                "group": _s(rule.get("group")),
                "affects": [
                    {"id": _s(item), "label": labels.affect_label(_s(item))}
                    for item in _l(rule.get("affects"))
                ],
                "evidence": _trim_evidence(_l(rule.get("evidence"))),
            }
        )
    issues = []
    for issue in _l(result.get("issues")):
        if not isinstance(issue, dict):
            continue
        severity = _s(issue.get("severity"))
        issues.append(
            {
                "id": _s(issue.get("issue_id")),
                "severity": severity,
                "severity_label": labels.SEVERITY_LABELS.get(severity, severity),
                "severity_tone": labels.SEVERITY_TONES.get(severity, "muted"),
                "rank": labels.SEVERITY_RANK.get(severity, 9),
                "title": _s(issue.get("title")),
                "detail": _s(issue.get("detail")),
                "scope": _s(issue.get("scope")),
            }
        )
    issues.sort(key=lambda item: (item["rank"], item["id"]))
    excluded = []
    for item in _l(result.get("excluded_sources")):
        if not isinstance(item, dict):
            continue
        excluded.append(
            {
                "id": _s(item.get("source_id")),
                "category": _s(item.get("category")),
                "label": _s(item.get("label")),
                "reason": _s(item.get("reason")),
                "points_total": _num(item.get("points_total")),
                "nodes": [
                    {"name": _s(node.get("name")), "node_id": _s(node.get("node_id")), "points": _num(node.get("points"))}
                    for node in _l(item.get("nodes"))
                    if isinstance(node, dict)
                ],
            }
        )
    consistency = []
    for key, value in _d(result.get("consistency")).items():
        if isinstance(value, bool):
            consistency.append(
                {
                    "key": key,
                    "label": labels.CONSISTENCY_LABELS.get(key, key),
                    "pass": value,
                }
            )
    assumptions = []

    def _keep_assumption(text: str) -> bool:
        return not any(
            marker in text
            for marker in (
                "排除 Meteor",
                "不考虑其他技能树",
                "最优理论场景",
                "不代表真实轨道物理可达",
            )
        )

    for item in _l(_p(result, "scenario", "assumptions")):
        if _keep_assumption(_s(item)):
            assumptions.append({"source": "场景", "text": _s(item)})
    for item in _l(_p(result, "snapshot", "derived", "assumptions")):
        if _keep_assumption(_s(item)):
            assumptions.append({"source": "输入推导", "text": _s(item)})
    for item in _l(_p(result, "scope", "notes")):
        if _keep_assumption(_s(item)):
            assumptions.append({"source": "范围", "text": _s(item)})

    counts = {
        "sources": len(by_id),
        "included": sum(1 for source in by_id.values() if source.get("included")),
        "excluded": sum(1 for source in by_id.values() if not source.get("included")),
        "rules": len(rules),
        "assumed": sum(1 for rule in rules if rule["status"] == "assumed"),
        "unresolved": sum(1 for rule in rules if rule["status"] == "unresolved"),
        "issues": len(issues),
        "warnings": sum(1 for issue in issues if issue["severity"] in ("warning", "error", "critical", "high")),
    }
    return {
        "rules": rules,
        "issues": issues,
        "excluded": excluded,
        "consistency": consistency,
        "assumptions": assumptions,
        "counts": counts,
    }


# --------------------------------------------------------------------------- #
# source browser: one row per real source (ledger contributions + conditional
# effects), split into "related" and the secondary "checked but not included"
# --------------------------------------------------------------------------- #

#: reading order of the source browser; cross-skill Buffs stay next to the
#: main-skill nodes so a conditional buff can be found without leaving the page
_BROWSER_CATEGORY_ORDER = [
    "equipment",
    "idol",
    "blessing",
    "idol_altar",
    "passive_node",
    "skill_node",
    "other_skill",
    "action",
    "base_character",
    "derived",
    "quest",
    "unknown",
]

_CHECKED_RECORD_CATEGORY_LABELS = {
    "other_skill": "其他技能",
    "skill_node": "技能节点",
    "main_skill": "主技能节点",
    "passive_node": "职业被动",
    "passive": "职业被动",
    "equipment": "装备",
    "idol": "神像",
    "idol_altar": "神像祭坛",
    "blessing": "祝福",
    "action": "动作",
    "scope": "检查范围",
    "unknown": "其他",
}


def _browser_category_label(category: str) -> str:
    return (
        labels.CATEGORY_LABELS.get(category)
        or source_catalog.CONDITIONAL_CATEGORY_LABELS.get(category)
        or _CHECKED_RECORD_CATEGORY_LABELS.get(category)
        or "其他来源"
    )


def _browser_category_rank(category: str) -> tuple[int, str]:
    if category in _BROWSER_CATEGORY_ORDER:
        return (_BROWSER_CATEGORY_ORDER.index(category), "")
    return (len(_BROWSER_CATEGORY_ORDER), category)


def _effect_badge_data(effect_state: dict) -> dict:
    return {
        "label": _s(effect_state.get("label")) or "待复核",
        "tone": _s(effect_state.get("tone")) or "muted",
        "state": _s(effect_state.get("state")),
        "assumption": bool(effect_state.get("assumption")),
        "name": _s(effect_state.get("name")),
    }


#: multi-column player status for one source row (one primary label only)
_PLAYER_ROW_STATUS = {
    "applied": labels.PLAYER_STATUS_LABELS["applied"],
    "single_cast": labels.PLAYER_STATUS_LABELS["single_cast"],
    "condition_unmet": labels.PLAYER_STATUS_LABELS["condition_unmet"],
    "unavailable": labels.PLAYER_STATUS_LABELS["unavailable"],
    "not_enabled": labels.PLAYER_STATUS_LABELS["not_enabled"],
    "unknown": labels.PLAYER_STATUS_LABELS["unknown"],
}


def _player_row_status(
    included_sources: list[dict],
    effect_states: list[dict],
    effects: list[dict],
) -> str:
    """One player-facing status for a source row (已生效/仅本次施法/…)."""

    states = [_s(state.get("state")) for state in effect_states]
    consumed = [source for source in included_sources if _l(source.get("applies"))]
    if "applied_both" in states or "baseline" in states or consumed:
        return "applied"
    if "applied_single" in states:
        return "single_cast"
    if states and all(state in {"unsupported", "missing", "unknown"} for state in states):
        return "unavailable"
    if "condition_unmet" in states:
        return "condition_unmet"
    if states:
        return "not_enabled"
    if effects:
        return "unknown"
    return "not_enabled"


#: player-facing label for an attribute-level state note (never raw audit text)
_PLAYER_ATTR_STATE = {
    "applied_both": labels.PLAYER_STATUS_LABELS["applied"],
    "applied_loop": labels.PLAYER_STATUS_LABELS["applied"],
    "baseline": labels.PLAYER_STATUS_LABELS["applied"],
    "applied_single": labels.PLAYER_STATUS_LABELS["single_cast"],
    "not_selected": labels.PLAYER_STATUS_LABELS["not_enabled"],
    "selected": labels.PLAYER_STATUS_LABELS["not_enabled"],
    "not_in_baseline": labels.PLAYER_STATUS_LABELS["not_enabled"],
    "condition_unmet": labels.PLAYER_STATUS_LABELS["condition_unmet"],
    "unsupported": labels.PLAYER_STATUS_LABELS["unavailable"],
    "missing": labels.PLAYER_STATUS_LABELS["unavailable"],
    "unknown": labels.PLAYER_STATUS_LABELS["unknown"],
}

#: engineering wording that must not leak into the player source table
_PLAYER_ATTR_NAME_SKIP_PREFIXES = ("未建模", "Unsupported", "未提供")


def _player_attributes(
    included_sources: list[dict],
    effects: list[dict],
    effect_states: list[dict],
) -> list[dict]:
    """Structured attribute rows: one name + exactly one value per row.

    A ledger contribution already carries a semantic ``stat_label`` and one
    value, so the value is never appended twice.  Conditional effects use their
    structured ``value_lines`` (name/value already separate); reference/total
    lines are skipped.  ``常驻`` / ``激活额外`` qualifiers keep the passive and
    active parts of the same Buff distinguishable.
    """

    attributes: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(attribute: dict, applies: Iterable[str], state_label: str = "") -> None:
        name = _s(attribute.get("name"))
        value_text = _s(attribute.get("value_text"))
        if not name or not value_text:
            return
        if name.startswith(_PLAYER_ATTR_NAME_SKIP_PREFIXES):
            return
        if attribute.get("kind") in ("unknown", "unsupported"):
            return
        key = (name, value_text, _s(attribute.get("qualifier")))
        if key in seen:
            return
        seen.add(key)
        item = {
            "name": name,
            "value": attribute.get("value"),
            "kind": _s(attribute.get("kind")) or "text",
            "value_text": value_text,
            "unit": _s(attribute.get("unit")),
            "qualifier": _s(attribute.get("qualifier")),
            "detail": _s(attribute.get("detail")),
            "applies": [label_text for label_text in _unique(applies) if label_text],
            "state_label": state_label,
        }
        attributes.append(item)

    for source in included_sources:
        # "未建模" rows captured for the audit layer do not add damage and are
        # not player-facing attributes
        if _s(_d(source.get("advanced")).get("stat_key")) == "unsupported":
            continue
        applies = [
            _s(_d(entry).get("label"))
            for entry in _l(source.get("applies"))
            if _s(_d(entry).get("label"))
        ]
        if not applies:
            # snapshot rows the damage model does not consume are not 增伤来源
            continue
        for attribute in source_catalog.player_source_attributes(source):
            _add(attribute, applies)

    for effect, state in zip(effects, effect_states):
        if not state or _s(state.get("state")) == "baseline":
            # already counted through the ledger contribution rows of this source
            continue
        name_parts = source_catalog.player_effect_name_parts(effect)
        branches: list[str] = []
        for branch in _l(state.get("branches")):
            label_text = _s(branch)
            if label_text:
                branches.append(label_text)
        state_key = _s(state.get("state"))
        state_label = "" if state.get("applied") else _PLAYER_ATTR_STATE.get(state_key, "")
        for attribute in source_catalog.player_effect_attributes(effect, name_parts["qualifier"]):
            _add(attribute, branches, state_label)

    # 常驻 (no qualifier) first, then 激活额外, then other named qualifiers
    attributes.sort(key=lambda item: 0 if _s(item.get("qualifier")) in ("", "常驻") else 1)
    return attributes


def _player_effect_owner_title(
    effects: list[dict],
) -> tuple[str, str, str]:
    """Title / source / condition for a conditional-effect source row."""

    titles: list[str] = []
    owner = ""
    condition = ""
    for effect in effects:
        parts = source_catalog.player_effect_name_parts(effect)
        if parts["title"] and parts["title"] not in titles:
            titles.append(parts["title"])
        if not owner and parts["owner"]:
            owner = parts["owner"]
        if not condition and parts["condition"]:
            condition = parts["condition"]
    return (titles[0] if titles else ""), owner, condition


def _player_row_condition(effects: list[dict], effect_states: list[dict]) -> str:
    for effect in effects:
        parts = source_catalog.player_effect_name_parts(effect)
        if parts["condition"]:
            return parts["condition"]
    for state in effect_states:
        condition_text = _s(state.get("condition_text")) or _s(state.get("condition"))
        if condition_text:
            text = source_catalog.player_effect_condition({"condition_text": condition_text})
            if text:
                return text
    return ""


def _player_row(
    group: dict,
    sources: list[dict],
    effects: list[dict],
    effect_states: list[dict],
) -> dict:
    """Player-facing fields for one source row (增伤来源 page).

    Main reading is title/source/status + attribute name + one value; raw ids,
    evidence, rule status and check-range bookkeeping are not part of this
    structure, so the page cannot render them by accident.
    """

    included_sources = [source for source in sources if source.get("included")]
    status_key = _player_row_status(included_sources, effect_states, effects)
    status_label = _PLAYER_ROW_STATUS.get(status_key, labels.PLAYER_STATUS_LABELS["unknown"])
    effect_titles = _unique(
        source_catalog.player_effect_name_parts(effect)["title"] for effect in effects
    )
    effect_owner = ""
    for effect in effects:
        effect_owner = source_catalog.player_effect_name_parts(effect)["owner"]
        if effect_owner:
            break
    if sources:
        # a Buff that already has ledger contribution rows is one physical
        # source: title it by the item/node name, not by the Buff name
        primary_source = included_sources[0] if included_sources else sources[0]
        title = source_catalog.player_source_title(primary_source)
        subtitle = source_catalog.player_source_detail(primary_source) or effect_owner
    else:
        title = effect_titles[0] if effect_titles else _s(group.get("label"))
        subtitle = effect_owner or _s(group.get("category_label"))
    if subtitle and subtitle == title:
        subtitle = ""
    condition = _player_row_condition(effects, effect_states)
    attributes = _player_attributes(included_sources, effects, effect_states)
    for attribute in attributes:
        attribute["show_state"] = bool(
            attribute.get("state_label")
            and attribute.get("state_label") != status_label
            and attribute.get("state_label") not in ("", labels.PLAYER_STATUS_LABELS["applied"])
        )
    consumed = [source for source in included_sources if _l(source.get("applies"))]
    has_contribution = (
        bool(attributes)
        or bool(effects)
        or any(
            _s(state.get("state")) in ("applied_both", "applied_single", "applied_loop", "baseline")
            for state in effect_states
        )
        or bool(consumed)
    )

    applies: list[str] = []
    for source in included_sources:
        for entry in _l(source.get("applies")):
            label_text = _s(_d(entry).get("label"))
            if label_text:
                applies.append(label_text)
    for state in effect_states:
        for branch in _l(state.get("branches")):
            if _s(branch):
                applies.append(_s(branch))
    applies = _unique(applies)

    # the effect family name is only kept when it differs from the ledger title
    effect_notes = [name for name in effect_titles if name and name != title]
    search_terms = [title, subtitle, status_label, condition, *effect_notes]
    for attribute in attributes:
        search_terms.extend(
            [
                _s(attribute.get("qualifier")),
                _s(attribute.get("name")),
                _s(attribute.get("value_text")),
            ]
        )
    return {
        "title": title,
        "subtitle": subtitle,
        "status_key": status_key,
        "status_label": status_label,
        "status_tone": labels.PLAYER_STATUS_TONES.get(status_key, "muted"),
        "condition": condition,
        "attributes": attributes,
        "attribute_count": len(attributes),
        "effect_names": effect_notes,
        # details only when the main row cannot hold the information (many
        # attributes); single-attribute rows show applies inline instead of a
        # nearly duplicate expansion container
        "needs_details": len(attributes) > 3,
        "inline_effect_names": effect_notes,
        "inline_applies": [label_text for label_text in applies if label_text] if len(attributes) <= 3 else [],
        "applies": [label_text for label_text in applies if label_text],
        "has_contribution": has_contribution,
        "enabled": status_key in ("applied", "single_cast"),
        "search": " ".join(term for term in search_terms if term).lower(),
    }


def _browser_row(
    group: dict,
    conditional_state: dict,
) -> dict:
    """Build one source-browser row: identity, contributions, effects, state."""

    sources = [source for source in group["sources"] if isinstance(source, dict)]
    effects = [effect for effect in group["effects"] if isinstance(effect, dict)]
    effect_states = [
        conditional_state.get("effects", {}).get(_s(effect.get("effect_id"))) or {}
        for effect in effects
    ]
    effect_states = [state for state in effect_states if state]

    included_sources = [source for source in sources if source.get("included")]
    included = bool(included_sources) or any(state.get("applied") for state in effect_states)
    badges = [_effect_badge_data(state) for state in effect_states]
    if not badges:
        if included_sources:
            badges.append({"label": "已计入", "tone": "ok", "state": "included", "assumption": False, "name": ""})
        else:
            badges.append({"label": "未计入", "tone": "muted", "state": "excluded", "assumption": False, "name": ""})
    assumption = any(state.get("assumption") for state in effect_states)

    branches: list[str] = []
    for source in included_sources:
        for entry in _l(source.get("applies")):
            label_text = _s(_d(entry).get("label"))
            if label_text:
                branches.append(label_text)
    for state in effect_states:
        branches.extend(_l(state.get("branches")))
    branches = _unique(branches)

    reason = ""
    if not included and not effects:
        for source in sources:
            if _s(source.get("reason")):
                reason = _s(source.get("reason"))
                break
    if not included and effects:
        for state in effect_states:
            if state.get("reason"):
                reason = state["reason"]
                break

    name = _s(group.get("label"))
    if not name:
        name = source_catalog.conditional_source_name(effects[0]) if effects else "未命名来源"
    detail = _s(group.get("detail"))
    if not detail and sources:
        detail = _s(sources[0].get("detail_line")) or _s(sources[0].get("category_label"))
    if not detail and effects:
        detail = _s(effects[0].get("source_label"))

    search_terms = [name, detail, _s(group.get("category_label"))]
    for source in sources:
        search_terms.extend(
            [
                _s(source.get("name")),
                _s(source.get("display_name")),
                _s(source.get("effect_label")),
                _s(source.get("slot_label")),
                _s(source.get("reason")),
            ]
        )
    for effect, state in zip(effects, effect_states):
        search_terms.extend(
            [
                _s(effect.get("name")),
                _s(effect.get("summary")),
                _s(effect.get("condition_text")),
                _s(effect.get("source_label")),
                _s(state.get("label")),
            ]
        )
    for badge in badges:
        search_terms.append(badge["label"])

    player = _player_row(group, sources, effects, effect_states)
    search_terms.extend(
        [
            player["title"],
            player["subtitle"],
            player["status_label"],
            player["condition"],
        ]
    )
    for attribute in player["attributes"]:
        search_terms.extend([attribute["name"], attribute["value_text"]])

    return {
        "identity": group["identity"],
        "category": group["category"],
        "category_label": group["category_label"],
        "name": name,
        "detail": detail,
        "id": _s(sources[0].get("id")) if sources else f"conditional:{_s(group.get('group_id'))}",
        "sources": sources,
        "effects": effects,
        "effect_states": effect_states,
        "included": included,
        "badges": badges,
        "assumption": assumption,
        "branches": branches,
        "reason": reason,
        "contribution_count": len(sources),
        "effect_count": len(effects),
        "related": bool(group.get("related")) or included,
        "conditional": bool(effects),
        "player": player,
        "search": " ".join(term for term in search_terms if term).lower(),
    }


def _build_source_browser(
    result: dict,
    sources_list: list[dict],
    by_id: dict[str, dict],
    conditions_view: dict,
    conditional_state: dict,
) -> dict:
    """Merge the ledger browser with conditional-effect sources.

    Rows are grouped by *source identity*: an item's several affix rows (and a
    ``:doubled`` variant) share one row, and an ``already_in_baseline``
    conditional effect is joined onto the ledger row it was counted as (via the
    effect's ``baseline_note`` contribution ids) instead of adding a second row.
    That is what keeps the contribution row and the buff row from double
    counting.

    Primary range = sources related to the current main skill (consumed ledger
    contributions + related conditional effects).  Excluded ledger groups and
    conditional check records are the secondary "已检查但未纳入" range; source,
    contribution, effect and check-record counts are exposed separately.
    """

    groups: dict[tuple, dict] = {}
    order: list[tuple] = []

    def _ensure(identity: tuple, category: str, label: str, detail: str = "") -> dict:
        if identity not in groups:
            groups[identity] = {
                "identity": identity,
                "category": category or "unknown",
                "category_label": _browser_category_label(category or "unknown"),
                "label": label,
                "detail": detail,
                "sources": [],
                "effects": [],
                "related": False,
                "conditional_group": "",
            }
            order.append(identity)
        return groups[identity]

    stale_reason_map = {
        "resistance-shred duration: shred is not modelled in v1; the Apex of Thought conversion condition (standing on Glyph of Dominion) is also not satisfied": (
            "resistance-shred duration: 旧 DPS 基线未建模；条件场景重算可按 glyph 模式应用（Glyph 站立为人工声明条件）。"
        ),
        "shred is not modelled in v1": "旧 DPS 基线未建模；条件场景重算可按 glyph 模式应用于火焰抗性削减。",
    }
    for source in sources_list:
        if not isinstance(source, dict):
            continue
        reason = _s(source.get("reason"))
        if reason:
            for old_text, new_text in stale_reason_map.items():
                if old_text in reason:
                    source["reason"] = reason.replace(old_text, new_text)
                    reason = _s(source.get("reason"))
        identity = source_catalog.source_identity(source)
        category = _s(source.get("category")) or "unknown"
        label = _s(source.get("name")) or _s(source.get("display_name"))
        if category == "idol" and source.get("idol_index") is not None:
            # several idol instances can share the same item name; keep the
            # slot number in the visible name so the rows stay distinct
            label = _s(source.get("display_name")) or label
        group = _ensure(identity, category, label)
        group["sources"].append(source)
        if source.get("included"):
            group["related"] = True

    effects = [effect for effect in _l(conditions_view.get("effects")) if isinstance(effect, dict)]
    conditional_groups = source_catalog.group_conditional_effects(
        effects, _l(conditions_view.get("groups"))
    )
    for bundle in conditional_groups:
        unmatched: list[dict] = []
        for effect in bundle["effects"]:
            ledger_match: dict | None = None
            # 1) exact join on the baseline ledger rows named by the effect
            for contribution_id in source_catalog.baseline_contribution_ids(effect):
                ledger = _d(by_id.get(contribution_id))
                if not ledger:
                    continue
                ledger_match = groups.get(source_catalog.source_identity(ledger))
                if ledger_match is not None:
                    break
            # 2) same physical source (item / passive node) that already has
            #    ledger rows but is not counted in the baseline
            if ledger_match is None:
                ledger_match = source_catalog.find_source_group(
                    source_catalog.effect_source_identity(effect), groups
                )
            if ledger_match is None:
                unmatched.append(effect)
                continue
            if effect not in ledger_match["effects"]:
                ledger_match["effects"].append(effect)
            ledger_match["related"] = True
        if not unmatched:
            continue
        related = any(
            _s(_d(effect.get("relevance")).get("status")) != "not_relevant_current_scenario"
            for effect in unmatched
        )
        group = _ensure(bundle["identity"], bundle["category"], bundle["name"], bundle["detail"])
        for effect in unmatched:
            if effect not in group["effects"]:
                group["effects"].append(effect)
        group["related"] = group["related"] or related
        group["conditional_group"] = bundle["group_id"]

    rows = [_browser_row(groups[identity], conditional_state) for identity in order]
    primary = sorted(
        [row for row in rows if row["related"]],
        key=lambda row: (_browser_category_rank(row["category"]), row["name"]),
    )
    checked_rows = sorted(
        [row for row in rows if not row["related"]],
        key=lambda row: (_browser_category_rank(row["category"]), row["name"]),
    )

    checked_records = []
    for record in _l(_d(conditions_view.get("checked")).get("records")):
        if not isinstance(record, dict):
            continue
        if _s(record.get("disposition")) != "excluded":
            continue
        checked_records.append(
            {
                "id": _s(record.get("record_id")),
                "category": _s(record.get("category")) or "unknown",
                "category_label": _CHECKED_RECORD_CATEGORY_LABELS.get(
                    _s(record.get("category")), _browser_category_label(_s(record.get("category")))
                ),
                "label": _s(record.get("source_label")) or _s(record.get("record_id")),
                "effect": _s(record.get("effect_summary")),
                "disposition": _s(record.get("disposition_label")) or "已检查未纳入",
                "reason": _s(record.get("reason")),
                "relevance": _s(record.get("relevance")),
            }
        )
    checked_records.sort(key=lambda item: (_browser_category_rank(item["category"]), item["label"]))

    primary_effect_count = sum(row["effect_count"] for row in primary)
    included_contributions = sum(
        1
        for row in primary
        for source in row["sources"]
        if source.get("included")
    )
    checked_total = len(checked_rows) + len(checked_records)
    effect_states = conditional_state.get("effects") or {}
    # category counts must match what the category filter actually shows
    # (primary rows + the collapsed checked rows and check records)
    category_counts: dict[str, int] = {}
    for row in primary + checked_rows:
        category = _s(row.get("category")) or "unknown"
        category_counts[category] = category_counts.get(category, 0) + 1
    for record in checked_records:
        category = _s(record.get("category")) or "unknown"
        category_counts[category] = category_counts.get(category, 0) + 1
    ordered_categories = [
        category
        for category in _BROWSER_CATEGORY_ORDER
        if category in category_counts
    ] + sorted(category for category in category_counts if category not in _BROWSER_CATEGORY_ORDER)
    return {
        "primary": primary,
        "checked_rows": checked_rows,
        "checked_records": checked_records,
        "counts": {
            "related_sources": len(primary),
            "included_contributions": included_contributions,
            "conditional_effects": primary_effect_count,
            "checked_records": checked_total,
            "checked_sources": len(checked_rows),
            "checked_condition_records": len(checked_records),
            "all_sources": len(rows),
        },
        "conditional_summary": {
            "applied": sum(
                1
                for state in effect_states.values()
                if state.get("applied") and state.get("state") != "baseline"
            ),
            "baseline": sum(
                1 for state in effect_states.values() if state.get("state") == "baseline"
            ),
            "selected_not_applied": len(conditional_state.get("selected_not_applied") or []),
            "unsupported": len(conditional_state.get("unsupported") or []),
            "not_selected": sum(
                1 for state in effect_states.values() if state.get("state") == "not_selected"
            ),
        },
        "categories": [
            {
                "id": category,
                "label": _browser_category_label(category),
                "count": category_counts[category],
            }
            for category in ordered_categories
        ],
    }


# --------------------------------------------------------------------------- #
# current-result limitations (generated, never a static stage description)
# --------------------------------------------------------------------------- #

#: strings that describe a superseded scope (the cross-skill Buff input channel
#: exists now, and center / both-stars are user-configurable conditions)
_OBSOLETE_LIMITATION_MARKERS = (
    "排除 Meteor",
    "Meteor / Enchant Weapon",
    "不考虑其他技能树",
    "跨技能 Buff",
    "默认中心",
    "默认双星",
    "双星全覆盖与中心 1 米条件同时满足是",
)


def _build_limitations(
    result: dict,
    context: dict,
    conditional_state: dict,
) -> dict:
    """Generate the limitation block from the current result and capability.

    Keeps still-true scope limits (statistics attribution, no action sequence,
    target assumptions), drops superseded stage-wide exclusions, and surfaces
    the real gaps of *this* result: selected-but-not-applied effects, adopted
    unconfirmed assumptions, and effects the fixed-condition mode cannot run.
    """

    scope = context.get("scope") or {}
    scenario = context.get("scenario") or {}
    conditional_applied = bool(conditional_state.get("scenario_applied"))

    statistics_scope = "、".join(
        text for text in (_s(item) for item in scope.get("included") or []) if text
    )
    if not statistics_scope:
        statistics_scope = "当前主技能本体及其已支持的衍生伤害（点燃）。"

    scenario_assumptions: list[dict] = []
    obsolete: list[str] = []
    for text in [_s(item) for item in scenario.get("assumptions") or []]:
        if not text:
            continue
        if any(marker in text for marker in _OBSOLETE_LIMITATION_MARKERS):
            obsolete.append(text)
            continue
        scenario_assumptions.append({"source": "目标场景", "text": text})

    limitations: list[str] = []
    for text in [_s(item) for item in scope.get("notes") or []]:
        if not text:
            continue
        if any(marker in text for marker in _OBSOLETE_LIMITATION_MARKERS):
            obsolete.append(text)
            continue
        limitations.append(text)
    scope_note = _s(scope.get("note"))
    if scope_note:
        limitations.insert(0, scope_note)

    estimation: list[str] = []
    gaps: list[dict] = []
    if conditional_applied:
        for item in conditional_state.get("assumptions") or []:
            text = _s(item)
            if text and text not in estimation:
                estimation.append(text)
        selected = conditional_state.get("selected_not_applied") or []
        if selected:
            gaps.append(
                {
                    "tone": "warn",
                    "text": f"{len(selected)} 项所选条件效果未计入当前结果",
                    "detail": [
                        f"{item.get('name')}：{item.get('reason') or item.get('label')}"
                        for item in selected
                    ],
                    "goto": "conditions",
                }
            )
        assumptions_applied = conditional_state.get("assumptions_applied") or []
        if assumptions_applied:
            gaps.append(
                {
                    "tone": "warn",
                    "text": f"当前结果采用 {len(assumptions_applied)} 项未确认机制假设",
                    "detail": [
                        f"{item.get('name')}：{'；'.join(item.get('texts') or [])}"
                        for item in assumptions_applied
                    ],
                    "goto": "conditions",
                }
            )
        unsupported = [item for item in conditional_state.get("unsupported") or []]
        if unsupported:
            action_like = sum(
                1
                for item in unsupported
                if "needs_action" in _s(item.get("reason"))
                or "time" in _s(item.get("reason"))
                or "needs_action" in _s(item.get("support_level"))
            )
            suffix = f"（其中 {action_like} 项需要动作/时间模拟）" if action_like else ""
            gaps.append(
                {
                    "tone": "info",
                    "text": f"固定条件估算暂不支持 {len(unsupported)} 项效果{suffix}",
                    "detail": [
                        f"{item.get('name')}：{item.get('reason') or '需要动作序列或时间模拟'}"
                        for item in unsupported
                    ],
                    "goto": "conditions",
                }
            )
    else:
        effects = conditional_state.get("effects") or {}
        not_in_baseline = [
            state for state in effects.values() if state.get("state") == "not_in_baseline"
        ]
        if not_in_baseline:
            gaps.append(
                {
                    "tone": "info",
                    "text": (
                        f"基础结果未包含 {len(not_in_baseline)} 项自动生效的跨技能条件效果；"
                        "可在「条件 Buff」页条件重算后查看"
                    ),
                    "detail": [f"{state.get('name')}：{state.get('label')}" for state in not_in_baseline],
                    "goto": "conditions",
                }
            )
        unsupported = [
            state
            for state in effects.values()
            if state.get("state") == "unsupported"
            and _s(state.get("relevance")) != "not_relevant_current_scenario"
        ]
        if unsupported:
            action_like = sum(
                1
                for state in unsupported
                if "needs_action" in _s(state.get("reason"))
                or "time" in _s(state.get("reason"))
                or "needs_action" in _s(state.get("support_level"))
            )
            suffix = f"（其中 {action_like} 项需要动作/时间模拟）" if action_like else ""
            gaps.append(
                {
                    "tone": "info",
                    "text": f"固定条件估算暂不支持 {len(unsupported)} 项效果{suffix}",
                    "detail": [f"{state.get('name')}：{state.get('reason') or state.get('support_gap')}" for state in unsupported],
                    "goto": "conditions",
                }
            )
        if conditional_state.get("available"):
            estimation.append(
                "基础结果的 DPS 使用引擎内建模型；跨技能条件 Buff 只在「条件 Buff」的条件重算通道中生效，不进入当前主 DPS。"
            )

    if scenario.get("theoretical"):
        enabled_conditions = (scenario.get("condition_summary") or {}).get("enabled") or []
        gaps.append(
            {
                "tone": "info",
                "text": "当前目标是理论最优合成靶，不代表真实轨道物理可达",
                "detail": (
                    ["当前场景条件：" + "、".join(_s(item) for item in enabled_conditions)]
                    if enabled_conditions
                    else []
                ),
                "goto": "",
            }
        )

    assumptions = scenario_assumptions
    for item in conditional_state.get("assumptions_applied") or []:
        for text in item.get("texts") or []:
            assumptions.append({"source": _s(item.get("name")), "text": _s(text)})

    return {
        "statistics_scope": statistics_scope,
        "estimation": estimation,
        "gaps": gaps,
        "assumptions": assumptions,
        "limitations": limitations,
        "warning_count": sum(1 for gap in gaps if gap.get("tone") == "warn"),
        "obsolete": obsolete,
        "result_kind": _s(scenario.get("result_kind")),
        "result_kind_label": _s(scenario.get("result_kind_label")),
    }


# --------------------------------------------------------------------------- #
# original-build source coverage (R6 layer added by discovery)
# --------------------------------------------------------------------------- #

_ORIGINAL_COVERAGE_LABELS = {
    "action_bar_skills": "技能栏技能",
    "skill_bar": "技能栏技能",
    "specialized_skills": "已专精技能",
    "unspecialized_skills": "未专精技能",
    "specialization_trees": "专精树",
    "equipment_items": "装备",
    "passive_tree_nodes": "职业被动节点",
    "explicit_unknowns": "显式未知",
    "specialization": "专精",
    "specialized": "已专精",
    "unspecialized": "未专精",
    "active_buffs": "保存的激活 Buff",
    "active_buff_entries": "保存的激活 Buff",
    "passive_nodes": "职业被动节点",
    "equipment": "装备",
    "equipment_instances": "装备",
    "idols": "神像",
    "idol_instances": "神像",
    "total": "原始来源合计",
    "checked": "已核对",
    "unknown": "未知",
}


def _build_original_coverage(result: dict) -> dict:
    """Expose ``conditional.catalog.original_source_coverage`` when present.

    This is the *original build source* audit (skill bar / specializations /
    active buff entries), deliberately kept separate from the internal
    checked-object count.  Old results do not carry it; the page omits the block.
    """

    coverage = _d(_p(result, "conditional", "catalog", "original_source_coverage"))
    if not coverage:
        return {"available": False}
    entries = [item for item in _l(coverage.get("sources")) if isinstance(item, dict)]
    unknowns = [item for item in _l(coverage.get("unknowns")) if isinstance(item, dict)]
    counts = _d(coverage.get("counts"))
    count_items = []
    for key, value in counts.items():
        number = _num(value)
        if number is None:
            continue
        count_items.append({"key": _s(key), "label": _ORIGINAL_COVERAGE_LABELS.get(_s(key), _s(key)), "value": number})
    return {
        "available": True,
        "counts": counts,
        "count_items": count_items,
        "entries": entries,
        "unknowns": unknowns,
        "notes": [_s(item) for item in _l(coverage.get("notes")) if _s(item)],
    }


# --------------------------------------------------------------------------- #
# sensitivity drawer (monster DR)
# --------------------------------------------------------------------------- #


def _build_sensitivity(result: dict) -> dict:
    monster = _d(result.get("monster_dr"))
    levels = _d(monster.get("levels"))
    dps_by_level = _d(monster.get("dps_by_level"))
    rows = []
    for key, payload in dps_by_level.items():
        if not isinstance(payload, dict):
            continue
        try:
            level = int(key)
        except (TypeError, ValueError):
            continue
        dr = _num(levels.get(str(level)))
        rows.append(
            {
                "level": level,
                "dr": dr,
                "factor": _num(payload.get("factor")),
                "single": _num(payload.get("single_cast_total")),
                "eager_dps": _num(payload.get("eager_dps")),
                "patient_dps": _num(payload.get("patient_dps")),
            }
        )
    rows.sort(key=lambda item: item["level"])
    current = _num(monster.get("monster_level"))
    return {
        "current_level": current,
        "rows": rows,
        "note": _s(monster.get("note")),
        "source": _d(monster.get("source")),
        "key_levels": [level for level in (75, 85, 95, 100) if any(row["level"] == level for row in rows)],
    }


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #


def _build_scenario(
    result: dict,
    context: dict,
    composition: dict,
    ignite_view: dict,
) -> dict:
    """Target-side scenario contract for the dynamic level / fire-resistance UI.

    The report keeps one calculation result.  Changing the target scenario only
    touches the target-side multipliers:

    * monster level -> engine-precomputed ``dps_by_level`` factor ratio;
    * target fire resistance -> the documented target multiplier
      ``1 - (target resistance - penetration)`` (negative resistance allowed),
      which is exactly what the engine stored in each branch's ``target`` block.

    Offensive factors (base damage, inc, more, application, duration) never
    change, so the client can apply a verified uniform transform instead of
    re-implementing the damage rules.
    """

    scenario = _d(result.get("scenario"))
    target = _d(scenario.get("target"))
    resistances = _d(target.get("resistances"))
    baseline_fire = _num(resistances.get("fire")) or 0.0
    baseline_cold = _num(resistances.get("cold")) or 0.0
    baseline_level = _num(scenario.get("monster_level")) or _num(target.get("monster_level"))
    baseline_dr = _num(scenario.get("monster_damage_reduction")) or _num(target.get("monster_damage_reduction"))
    baseline_dr_factor = _num(scenario.get("monster_dr_factor")) or _num(target.get("monster_dr_factor"))

    components = {component.get("key"): component for component in composition.get("components") or []}
    fire = components.get("fire") or {}
    cold = components.get("cold") or {}
    fire_pen = _d(fire.get("pen"))
    cold_pen = _d(cold.get("pen"))
    ignite_per_stack = _d(ignite_view.get("per_stack"))
    ignite_pen = _num(ignite_per_stack.get("penetration")) or 0.0
    fire_multiplier = _num(fire_pen.get("resistance_multiplier"))
    cold_multiplier = _num(cold_pen.get("resistance_multiplier"))
    ignite_multiplier = _num(ignite_per_stack.get("resistance_multiplier"))
    body_fire = _num(fire.get("total"))
    body_cold = _num(cold.get("total"))
    single = context.get("single") or {}
    ignite_damage = _num(single.get("ignite_total"))
    total_damage = _num(single.get("total"))
    body_total = _num(single.get("body_total"))
    if body_total is None and body_fire is not None and body_cold is not None:
        body_total = body_fire + body_cold

    def _share(part, whole):
        if part is None or not whole:
            return None
        return part / whole

    return {
        "baseline": {
            "level": baseline_level,
            "dr": baseline_dr,
            "dr_factor": baseline_dr_factor,
            "fire_resistance": baseline_fire,
            "cold_resistance": baseline_cold,
            "resistances": {key: _num(value) for key, value in resistances.items()},
            "negative_resistance_allowed": bool(target.get("negative_resistance_allowed", True)),
        },
        "limits": {
            "level_min": 1,
            "level_max": 100,
            "resistance_min": -1.0,
            "resistance_max": 2.0,
            "resistance_input_min": -100,
            "resistance_input_max": 100,
        },
        "penetration": {
            "body_fire": _num(fire_pen.get("total")) or 0.0,
            "body_cold": _num(cold_pen.get("total")) or 0.0,
            "ignite": ignite_pen,
        },
        "multipliers": {
            "body_fire": fire_multiplier,
            "body_cold": cold_multiplier,
            "ignite": ignite_multiplier,
        },
        "components": {
            "body_fire": body_fire,
            "body_cold": body_cold,
            "body": body_total,
            "ignite": ignite_damage,
            "ignite_tail": _num(single.get("ignite_tail")),
            "total": total_damage,
        },
        "shares": {
            "body_fire": _share(body_fire, body_total),
            "body_cold": _share(body_cold, body_total),
            "body": _share(body_total, total_damage),
            "ignite": _share(ignite_damage, total_damage),
        },
        "note": (
            "等级使用引擎预计算的全等级系数表；目标火抗使用目标侧乘区 "
            "1 -（目标抗性 - 穿透）。二者都是独立乘区，因此只做统一缩放，不重算伤害规则。"
        ),
    }


def build_view(result: dict) -> dict:
    """Build the complete display view used by every page and the client JS."""

    context = _build_context(result)
    sources_list, by_id = source_catalog.build_sources(result)
    node_index = source_catalog.skill_node_index(result)
    rules_by_id = _rules_by_id(result)

    intelligence = _num(_p(result, "snapshot", "attributes", "intelligence"))
    if intelligence is None:
        intelligence = _num(_p(result, "snapshot", "derived", "intelligence"))
    int_source = source_catalog.synthetic_source(
        "derived:intelligence-scaling",
        category="derived",
        name="智力属性缩放",
        display_name=f"派生属性 · 智力 {units.fmt_compact(intelligence, 'points') if intelligence is not None else '44'} 点",
        effect_label="每点智力 +4% 伤害增伤，合计 +176%",
        kind="fraction",
        value=0.04 * intelligence if intelligence is not None else 1.76,
        status="derived",
        detail_line="属性总览 → 技能增伤",
        notes="智力先作用于 Black Hole，再传递给点燃；不会作为独立 more 再乘一次。",
        order=20_000,
    )
    by_id[int_source["id"]] = int_source
    sources_list.append(int_source)

    usage = _scan_usage(result, by_id, node_index, rules_by_id)
    _attach_usage(sources_list, usage)
    sources_list.sort(key=source_catalog.source_order_key)

    composition = _build_composition(result, by_id, node_index, rules_by_id)
    ignite = _build_ignite(result, by_id, rules_by_id, node_index)
    timeline = _build_timeline(result)
    validation = _build_validation(result, by_id)
    sensitivity = _build_sensitivity(result)
    scenario = _build_scenario(result, context, composition, ignite)
    conditions_view = _build_conditions_view(result)
    conditional_state = _d(context.get("conditional"))
    source_browser = _build_source_browser(
        result, sources_list, by_id, conditions_view, conditional_state
    )
    limitations = _build_limitations(result, context, conditional_state)
    original_coverage = _build_original_coverage(result)

    for source in sources_list:
        source.pop("_raw", None)

    return {
        "schema": "black-hole-report-view-v1",
        "context": context,
        "sources": by_id,
        "source_order": [source["id"] for source in sources_list],
        "composition": composition,
        "ignite": ignite,
        "timeline": timeline,
        "validation": validation,
        "sensitivity": sensitivity,
        "scenario": scenario,
        "conditions": conditions_view,
        "conditional": conditional_state,
        "source_browser": source_browser,
        "limitations": limitations,
        "original_coverage": original_coverage,
        "pages": [
            {"id": page_id, "label": PAGE_LABELS[page_id], "question": PAGE_TITLES[page_id]}
            for page_id in PAGE_IDS
        ],
    }


_BASELINE_DISPLAY_LABELS = {
    "legacy_single_skill": "仅单技能基线（不含条件 Buff 基线）",
    "build_base": "构筑基础状态（含条件 Buff 基线）",
}


def _display_baseline(conditional: dict, payload: dict) -> dict:
    """Describe/align the baseline behind the displayed main result (LIVE-08)."""

    recompute = _d(conditional.get("recompute"))
    scenario = _d(payload.get("scenario"))
    profile = _s(scenario.get("baseline_profile")) or _s(recompute.get("baseline_profile")) or "build_base"
    applied = bool(conditional.get("scenario_applied"))
    delta = _d(recompute.get("delta"))
    has_delta = any(
        isinstance(value, (int, float)) and abs(float(value)) > 1e-9
        for value in delta.values()
    )
    aligned = None
    if not applied and profile == "build_base" and has_delta and scenario:
        # Main page numbers come from the engine's direct (legacy) baseline.
        profile = "legacy_single_skill"
        scenario = dict(scenario)
        scenario["baseline_profile"] = profile
        aligned = scenario
    label = _BASELINE_DISPLAY_LABELS.get(profile, "当前结果口径")
    if aligned is not None:
        note = (
            "页面主结果为引擎直接结果（未应用条件 Buff 基线）；默认计算请求沿用同一口径，"
            "未修改控件点击「计算 DPS」不会改变数值。选择条件预设后重算才会切换到对应基线。"
        )
    elif applied:
        note = "本次计算已应用该基线/条件预设；再次计算同一配置不会改变数值。"
    else:
        note = "默认计算请求沿用该口径。"
    return {
        "profile_id": profile,
        "label": label,
        "note": note,
        "aligned_scenario": aligned,
    }


def _build_conditions_view(result: dict) -> dict:
    """Read the conditions page payload produced by ``le_calc.conditional``.

    The display layer does not evaluate conditional effects itself; it forwards
    the deterministic view built by the conditional package.  Old results
    (without the block) get an explicit unavailable payload so the page can
    explain why instead of rendering a broken/blank tab.
    """

    conditional = _d(result.get("conditional"))
    view = _d(conditional.get("view"))
    if view:
        payload = dict(view)
        payload.setdefault(
            "available",
            conditional.get("status") in ("ready", "degraded"),
        )
        payload.setdefault("unavailable_reason", "")
        # server-side conditional recompute (cross-skill Buff input channel); optional
        recompute = _d(conditional.get("recompute"))
        payload["recompute"] = recompute
        scenario_applied = bool(conditional.get("scenario_applied"))
        payload["scenario_applied"] = scenario_applied
        # LIVE-08: the default recompute request must use the same baseline as
        # the main result shown on the page.  The engine's direct result
        # (``single_cast``) is the legacy_single_skill baseline; the server
        # recompute channel defaults to build_base, which would make an untouched
        # "计算 DPS" click jump by the baseline delta.  When the displayed result
        # has no applied conditions yet, align the forwarded scenario with the
        # displayed main口径 and label it in the page.
        display_baseline = _display_baseline(conditional, payload)
        if display_baseline.get("aligned_scenario") is not None:
            payload["scenario"] = display_baseline.pop("aligned_scenario")
        payload["display_baseline"] = display_baseline
        # The service adapter injects the real endpoint at render time
        # (shell.render(recalc_endpoint=...)); offline reports get an explicit
        # placeholder instead of a hardcoded loopback port.
        payload["live_recompute"] = {
            "url": "",
            "available": False,
            "command": "le_calc serve --snapshot <snapshot> --ruleset <ruleset> --calc-input <calc-input> --build <bd.json> --monster-level 100",
        }
        return payload
    status = _s(conditional.get("status"))
    reason = ""
    for issue in _l(conditional.get("issues")):
        if isinstance(issue, dict) and _s(issue.get("message")):
            reason = _s(issue.get("message"))
            break
    if not reason and status == "unavailable":
        reason = "条件 Buff 模块不可用：未找到或无法读取条件效果注册表。"
    return empty_conditions_view(reason)
