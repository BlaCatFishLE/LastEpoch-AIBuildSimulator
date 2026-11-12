"""Shared constants and small helpers for the conditional-effects package.

Owner: backend-conditional (phase-3 conditional Buff configuration).

Everything in this module is data-only: schemas, enum vocabularies, display
labels, ordering and JSON/path helpers.  Keeping them in one place makes the
Python evaluator and the client-side ``rules`` payload (see ``conditions.py``)
share exactly the same vocabulary.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Schemas / default file names
# ---------------------------------------------------------------------------

SCHEMA_REGISTRY = "conditional-effects-registry-v1"
SCHEMA_CONTEXT = "bh-conditional-context-v1"
SCHEMA_CATALOG = "bh-conditional-catalog-v1"
SCHEMA_SCENARIO = "bh-conditional-scenario-v1"
SCHEMA_EVALUATION = "bh-conditional-evaluation-v1"
SCHEMA_RULES = "bh-conditional-rules-v1"
SCHEMA_VIEW = "bh-conditional-view-v1"
SCHEMA_EFFECTS_ARTIFACT = "bh-conditional-effects-artifact-v1"

REGISTRY_ID_DEFAULT = "bh-conditional-effects-v1"
RULESET_ID_DEFAULT = "bh-stage1-v4"
GAME_VERSION_DEFAULT = "1.5.2"
DATASET_ID_DEFAULT = "phase1-dataset-003"

DEFAULT_SCENARIO_FILENAME = "bh-conditional-scenario.json"
DEFAULT_REGISTRY_RELPATH = "tools/phase3/rules/conditional-effects-bh-stage1-v1.json"
DEFAULT_RULESET_RELPATH = "tools/phase3/rules/ruleset-bh-stage1-v4.json"
# NOTE: the public release ships no case inputs, so there are no default
# calculation-input / snapshot / bd paths here; every caller passes explicit
# case-scoped paths (see ``case.json``).

DEFAULT_MODE_ID = "fixed_conditions"
DEFAULT_BASELINE_PROFILE_ID = "build_base"

# ---------------------------------------------------------------------------
# Enumerations (contract section 5 / 8)
# ---------------------------------------------------------------------------

REGISTRY_SCHEMAS = (SCHEMA_REGISTRY,)

CATEGORIES = (
    "other_skill",
    "equipment",
    "passive",
    "main_skill",
    "target",
    "action",
)

#: category values accepted by the loader.  The contract only enumerates the
#: six values above, but earlier drafts of the page/data used ``idol`` /
#: ``blessing`` as distinct categories; tolerating them keeps the loader
#: forward-compatible and the view maps them onto the page category.
CATEGORY_ALIASES = {
    "idol": "equipment",
    "blessing": "equipment",
    "idol_altar": "equipment",
}

SOURCE_KINDS = (
    "skill_node",
    "passive_node",
    "equipment",
    "idol",
    "blessing",
    "idol_altar",
    "main_skill_node",
)

MATCH_TYPES = ("skill_node", "passive_node", "equipment_mod", "mod_id")

CONTROL_KINDS = ("toggle", "stacks", "cast_state", "none")

ACTIVATION_KINDS = (
    "persistent_buff",
    "passive",
    "conditional_passive",
    "stacks_buff",
    "numeric_threshold",
    "cast_state",
    "action",
    "baseline_included",
    "layout",
)

RELEVANCE_STATUSES = (
    "relevant",
    "scenario_dependent",
    "unconfirmed",
    "not_relevant_current_scenario",
)

BASELINE_RELATIONS = ("new", "replaces", "already_in_baseline", "action_not_supported")

SUPPORT_LEVELS = (
    "engine_supported",
    "needs_rule_mapping",
    "needs_action_or_time_simulation",
    "mechanism_unresolved",
)

RESEARCH_STATUSES = ("confirmed", "assumed", "unresolved")

STATE_KINDS = ("bool", "int")
STATE_SOURCES = ("user", "auto")

CHECKED_DISPOSITIONS = ("candidate", "excluded", "baseline_included")

CHECKED_CATEGORIES = (
    "other_skill",
    "equipment",
    "passive",
    "main_skill",
    "target",
    "action",
    "scope",
)

COMPUTATION_MODULE_IDS = (
    "body.fire",
    "body.cold",
    "body.aggregate",
    "ignite",
    "ignite.application",
    "cast",
    "cooldown",
    "duration",
    "penetration.body",
    "penetration.ignite",
    "target.state",
    "ailment",
)

CHECKED_SCOPE_KEYS = (
    "equipment_items",
    "idols",
    "blessings",
    "idol_altar",
    "passive_nodes",
    "specializations",
    "snapshot_contributions",
    "excluded_sources",
)

#: modifier operations understood by the damage engine (``model.Contribution``)
MODIFIER_OPERATIONS = ("added", "increased", "more", "base", "flag")

# ---------------------------------------------------------------------------
# Evaluation status vocabulary (contract section 8.4)
# ---------------------------------------------------------------------------

STATUS_ORDER = (
    "not_available",
    "unsupported_action",
    "already_in_baseline",
    "auto_active",
    "auto_inactive",
    "auto_unknown",
    "not_selected",
    "selected_condition_unmet",
    "selected_ready_needs_rules",
    "selected_ready_needs_time",
    "selected_unconfirmed",
    "active_supported",
)

STATUS_LABELS = {
    "not_available": "构筑无此来源",
    "unsupported_action": "动作机制未支持",
    "auto_active": "自动判定·已具备",
    "auto_inactive": "自动判定·未满足",
    "auto_unknown": "自动判定·待确认",
    "not_selected": "未选择",
    "selected_condition_unmet": "已选择·条件不足",
    "selected_ready_needs_rules": "已选择·条件满足·缺规则映射",
    "selected_ready_needs_time": "已选择·条件满足·需时间/动作模拟",
    "selected_unconfirmed": "已选择·机制待确认",
    "active_supported": "已选择·已接入计算",
    "already_in_baseline": "已计入基础",
}

STATUS_TONES = {
    "not_available": "muted",
    "unsupported_action": "warn",
    "auto_active": "ok",
    "auto_inactive": "muted",
    "auto_unknown": "warn",
    "not_selected": "muted",
    "selected_condition_unmet": "warn",
    "selected_ready_needs_rules": "warn",
    "selected_ready_needs_time": "warn",
    "selected_unconfirmed": "warn",
    "active_supported": "ok",
    "already_in_baseline": "info",
}

RELEVANCE_LABELS = {
    "relevant": "相关",
    "scenario_dependent": "场景依赖",
    "unconfirmed": "待确认",
    "not_relevant_current_scenario": "本场景不相关",
}

RELEVANCE_TONES = {
    "relevant": "ok",
    "scenario_dependent": "warn",
    "unconfirmed": "warn",
    "not_relevant_current_scenario": "muted",
}

SUPPORT_LABELS = {
    "engine_supported": "已接入",
    "needs_rule_mapping": "需要规则映射",
    "needs_action_or_time_simulation": "需要时间/动作模拟",
    "mechanism_unresolved": "机制待确认",
}

SUPPORT_TONES = {
    "engine_supported": "ok",
    "needs_rule_mapping": "warn",
    "needs_action_or_time_simulation": "warn",
    "mechanism_unresolved": "warn",
}

CATEGORY_LABELS = {
    "other_skill": "其他技能",
    "equipment": "装备",
    "idol": "神像",
    "passive": "职业被动",
    "main_skill": "主技能",
    "target": "目标状态",
    "action": "动作",
}

#: page filter category order ("all" is prepended by the view builder)
PAGE_CATEGORY_ORDER = (
    "other_skill",
    "equipment",
    "idol",
    "passive",
    "main_skill",
    "target",
    "action",
)

DISPOSITION_LABELS = {
    "candidate": "候选",
    "registered": "已注册",
    "checked_candidate": "已检查候选",
    "unregistered": "待确认候选",
    "excluded": "已检查未纳入",
    "baseline_included": "已计入基础",
}

EVIDENCE_KIND_LABELS = {
    "game_data": "游戏数据",
    "catalog": "目录数据",
    "derivation": "推导计算",
    "user_rule": "用户规则",
    "third_party_reference": "第三方参考",
    "public_doc": "公开文档",
    "reverse_engineering": "逆向证据",
    "external_service": "外部服务",
    "analyst_inference": "分析推断",
}

#: fixed display order used when sorting effects for view/evaluation output
SORT_CATEGORY_ORDER = (
    "main_skill",
    "other_skill",
    "equipment",
    "idol",
    "passive",
    "target",
    "action",
)

# ---------------------------------------------------------------------------
# Auto state derivations (registry ``shared_states`` with source=="auto")
# ---------------------------------------------------------------------------

#: id -> list of accepted snapshot lookup keys.  These are the engine-derived
#: values the current build actually has; the registry can point at them with
#: ``derive.key`` / ``derived_from`` / ``ref`` instead of relying on aliases.
AUTO_STATE_ALIASES = {
    "auto.max_mana": ("derived.mana_max", "mana_max"),
    "auto.mana_max": ("derived.mana_max", "mana_max"),
    "auto.cast_time": ("derived.cast_time_s", "cast_time_s"),
    "auto.cooldown_recovery_speed": (
        "derived.cooldown_recovery_speed_increased",
        "cooldown_recovery_speed_increased",
    ),
    "auto.skill_duration": ("derived.skill_duration_s", "skill_duration_s"),
    "auto.ignite_duration": ("derived.ignite_duration_s", "ignite_duration_s"),
    "auto.uncapped_fire_resistance": (
        "derived.uncapped_fire_resistance_percent",
        "resistances.fire.total",
    ),
    "auto.overcapped_fire_resistance": (
        "derived.overcapped_fire_resistance_percent",
        "resistances.fire.overcapped",
    ),
    "auto.overcapped_cold_resistance": (
        "derived.overcapped_cold_resistance_percent",
        "resistances.cold.overcapped",
    ),
    "auto.intelligence": ("attributes.intelligence",),
    "auto.strength": ("attributes.strength",),
    "auto.dexterity": ("attributes.dexterity",),
    "auto.vitality": ("attributes.vitality",),
    "auto.attunement": ("attributes.attunement",),
    "auto.player_level": ("character.level", "level"),
}

#: baseline (target-side) auto states derived from the DPS scenario / ruleset.
BASELINE_STATE_LABELS = {
    "baseline.target_boss": "目标为稀有/首领",
    "baseline.centered": "目标位于中心 1 米内",
    "baseline.both_stars_cover": "双星同时覆盖目标",
}

#: comparison operator spellings accepted in registry/state definitions
COMPARISON_OPS = {
    ">=": ">=",
    "gte": ">=",
    "ge": ">=",
    "<=": "<=",
    "lte": "<=",
    "le": "<=",
    ">": ">",
    "gt": ">",
    "<": "<",
    "lt": "<",
    "==": "==",
    "=": "==",
    "eq": "==",
    "!=": "!=",
    "ne": "!=",
}


def compare_value(op: str, value: Optional[float], threshold: Optional[float]) -> Optional[bool]:
    """Evaluate one declared comparison, returning None when unknown."""
    normalised = COMPARISON_OPS.get(str(op).strip().lower()) or COMPARISON_OPS.get(str(op).strip())
    if normalised is None or value is None or threshold is None:
        return None
    try:
        left = float(value)
        right = float(threshold)
    except (TypeError, ValueError):
        return None
    if normalised == ">=":
        return left >= right
    if normalised == "<=":
        return left <= right
    if normalised == ">":
        return left > right
    if normalised == "<":
        return left < right
    if normalised == "==":
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
    if normalised == "!=":
        return not math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
    return None


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return the sha256 of a file (deterministic, read-only)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_safe(value: Any) -> Any:
    """Recursively coerce a value into plain JSON types.

    * NaN / +/-Infinity become ``None`` (the view contract forbids them).
    * tuples / sets become lists (sets sorted for determinism).
    * Paths become their POSIX string form.
    * unknown objects fall back to ``str(value)``.
    """
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[str(key)] = json_safe(item)
        return out
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [json_safe(item) for item in sorted(value, key=str)]
    if isinstance(value, Path):
        return value.as_posix()
    return str(value)


def canonical_json(value: Any) -> str:
    """Canonical JSON used for fingerprints (sorted keys, no whitespace)."""
    return json.dumps(
        json_safe(value),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def json_fingerprint(value: Any) -> str:
    return sha256_text(canonical_json(value))


def repo_relative(path: str | Path | None, repo_root: str | Path | None) -> Optional[str]:
    """Return a repo-relative POSIX path (or the original relative path)."""
    if path is None:
        return None
    candidate = Path(path)
    root = Path(repo_root) if repo_root is not None else None
    if candidate.is_absolute():
        if root is None:
            return candidate.as_posix()
        try:
            return candidate.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            # outside the repository: never leak an absolute host path
            return candidate.name
    return candidate.as_posix()


def resolve_repo_path(
    path: str | Path | None,
    *,
    repo_root: str | Path,
    fallback_root: str | Path | None = None,
) -> Optional[Path]:
    """Resolve a possibly-relative path against ``repo_root``.

    Absolute paths are returned as-is.  Relative paths are first resolved
    against ``repo_root``; if that does not exist, ``fallback_root`` (the
    package checkout root) is tried so callers running from another cwd still
    find repository files.  The resolved path is returned regardless of
    existence so callers can report ``exists: false``.
    """
    if path is None:
        return None
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    primary = Path(repo_root) / candidate
    if primary.exists():
        return primary
    if fallback_root is not None:
        secondary = Path(fallback_root) / candidate
        if secondary.exists():
            return secondary
    return primary


def fmt_number(value: Optional[float], *, digits: int = 6) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(number) or math.isinf(number):
        return "—"
    if number == 0:
        return "0"
    text = f"{number:.{digits}g}"
    return text


def format_actual(value: Optional[float], unit: str | None) -> str:
    """Human display for a resolved effect value."""
    if value is None:
        return "—"
    unit_key = (unit or "").strip().lower()
    if unit_key in ("fraction", "percent_fraction"):
        return _signed_percent(value)
    if unit_key in ("percent", "%"):
        return _signed_percent(value / 100.0)
    if unit_key in ("seconds", "second", "s"):
        return f"{fmt_number(value)} 秒"
    if unit_key in ("stack", "stacks", "层"):
        return f"{fmt_number(value)} 层"
    if unit_key in ("multiplier", "x"):
        return f"×{fmt_number(value)}"
    return fmt_number(value)


def _signed_percent(fraction: float) -> str:
    if fraction == 0:
        return "0%"
    sign = "+" if fraction > 0 else "-"
    return f"{sign}{fmt_number(abs(fraction) * 100)}%"


def trim_text(value: Any, limit: int = 220) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def sorted_unique(values: Iterable[Any]) -> list:
    seen: list[Any] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def evidence_entries(items: Iterable[Any] | None) -> list[dict[str, Any]]:
    """Normalise registry/snapshot evidence records into JSON-safe dicts."""
    out: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            entry = {
                "kind": str(item.get("kind") or "derivation"),
                "title": EVIDENCE_KIND_LABELS.get(str(item.get("kind") or ""), str(item.get("kind") or "证据")),
                "detail": trim_text(item.get("detail"), 400),
                "locator": trim_text(item.get("locator"), 400) or None,
                "snippet": trim_text(item.get("snippet"), 400) or None,
            }
        elif isinstance(item, str):
            entry = {
                "kind": "derivation",
                "title": "推导计算",
                "detail": trim_text(item, 400),
                "locator": None,
                "snippet": None,
            }
        else:
            continue
        out.append(entry)
    return out


def enum_value(value: Any) -> str:
    return "" if value is None else str(value).strip()


__all__ = [
    "AUTO_STATE_ALIASES",
    "ACTIVATION_KINDS",
    "BASELINE_RELATIONS",
    "BASELINE_STATE_LABELS",
    "CATEGORY_ALIASES",
    "CATEGORY_LABELS",
    "CATEGORIES",
    "CHECKED_CATEGORIES",
    "CHECKED_DISPOSITIONS",
    "CHECKED_SCOPE_KEYS",
    "COMPARISON_OPS",
    "COMPUTATION_MODULE_IDS",
    "CONTROL_KINDS",
    "DEFAULT_BASELINE_PROFILE_ID",
    "DEFAULT_MODE_ID",
    "DEFAULT_REGISTRY_RELPATH",
    "DEFAULT_RULESET_RELPATH",
    "DEFAULT_SCENARIO_FILENAME",
    "DISPOSITION_LABELS",
    "EVIDENCE_KIND_LABELS",
    "MATCH_TYPES",
    "MODIFIER_OPERATIONS",
    "PAGE_CATEGORY_ORDER",
    "REGISTRY_SCHEMAS",
    "RELEVANCE_LABELS",
    "RELEVANCE_STATUSES",
    "RELEVANCE_TONES",
    "RESEARCH_STATUSES",
    "SCHEMA_CATALOG",
    "SCHEMA_CONTEXT",
    "SCHEMA_EFFECTS_ARTIFACT",
    "SCHEMA_EVALUATION",
    "SCHEMA_REGISTRY",
    "SCHEMA_RULES",
    "SCHEMA_SCENARIO",
    "SCHEMA_VIEW",
    "SORT_CATEGORY_ORDER",
    "SOURCE_KINDS",
    "STATE_KINDS",
    "STATE_SOURCES",
    "STATUS_LABELS",
    "STATUS_ORDER",
    "STATUS_TONES",
    "SUPPORT_LABELS",
    "SUPPORT_LEVELS",
    "SUPPORT_TONES",
    "canonical_json",
    "compare_value",
    "enum_value",
    "evidence_entries",
    "fmt_number",
    "format_actual",
    "json_fingerprint",
    "json_safe",
    "repo_relative",
    "resolve_repo_path",
    "sha256_file",
    "sha256_text",
    "sorted_unique",
    "trim_text",
]
