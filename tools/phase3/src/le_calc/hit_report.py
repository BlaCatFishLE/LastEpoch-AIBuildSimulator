"""Result assembly + offline report for the Flame Reave ``hit`` model extension.

Owner: fr-pipeline.  Consumers: ``le_calc.cli`` (internal ``hit-snapshot`` /
``hit-run`` steps) and ``scripts/le_commands.py`` (single entry point
``le.ps1 run --case examples/<case>/case.json``).

This is **not** a second pipeline: the hit model is a skill-model extension
inside the existing ``le_calc`` engine (next to the Black Hole modules).  It
reuses the existing contracts — ``case.json``, the BuildSpec revision,
``calculation-input``, :class:`le_calc.model.Snapshot` (``build_meta``
fingerprint chain), the source/rule ledgers, ``run-manifest.json``, the
offline single-file report convention and the conditional scenario structure.
What is new is the Flame Reave hit model itself (``hit_inputs`` /
``hit_damage`` / ``hit_simulate`` / ``hit_buffs``) plus this report layer.

The result schema is ``le-hit-result-v1`` (see DESIGN §5.3 — the field names
differ from the Black Hole ``result-v1`` model because the damage anatomy is
different, not because it is a separate pipeline).  Top level: ``run, input,
scope, scenario, snapshot, skill, single_hit, hits_per_cast, dps, buffs,
source_ledger, rule_ledger, issues, consistency``; one run directory holds
``result.json``, ``input-snapshot.json``, ``source-ledger.json``,
``rule-ledger.json``, ``buff-screening.json``, ``flame-reave-damage.html``,
``README.zh-CN.md`` and ``run-manifest.json`` (sha256 of every other file + the
consistency checks).

The HTML is a self-contained offline document: inline CSS/JS, no external
fonts, no ``http(s)`` references, and it never re-implements damage rules — it
only presents the data already inside ``result.json``.
"""

from __future__ import annotations

import hashlib
import json
import re
from html import escape as _escape
from pathlib import Path
from typing import Any, Iterable, Optional

from . import ENGINE_ID, __version__
from .report_ui import components as _ui_components
from .report_ui import document as _ui_document
from .report_ui import units as _ui_units
from .report_ui import shared_ui

#: result document schema id (DESIGN §5.3)
SCHEMA_VERSION = "le-hit-result-v1"
#: companion artefact schemas
SOURCE_LEDGER_SCHEMA = "le-hit-source-ledger-v1"
RULE_LEDGER_SCHEMA = "le-hit-rule-ledger-v1"
BUFF_SCREENING_SCHEMA = "le-hit-buff-screening-v1"
RUN_MANIFEST_SCHEMA = "le-hit-run-manifest-v1"
#: file names inside one run directory
RESULT_FILENAME = "result.json"
SNAPSHOT_FILENAME = "input-snapshot.json"
SOURCE_LEDGER_FILENAME = "source-ledger.json"
RULE_LEDGER_FILENAME = "rule-ledger.json"
BUFF_SCREENING_FILENAME = "buff-screening.json"
HTML_FILENAME = "flame-reave-damage.html"
README_FILENAME = "README.zh-CN.md"
MANIFEST_FILENAME = "run-manifest.json"

#: standard scope note (DESIGN §5.3 / task contract); shown verbatim in README+HTML
SCOPE_NOTE = (
    "限定来源：仅 Flame Reave（fr11mv）本体命中，含单次命中与 5/30/120s DPS 窗口；"
    "不是完整 BD 实战 DPS。"
)

#: windows required by the contract
REQUIRED_WINDOWS = (5.0, 30.0, 120.0)

#: statuses that make a run ``assumption`` instead of ``ok``
BLOCKING_STATUSES = ("assumed", "unresolved")

_ACTIVE_CONDITIONS = ("always", "satisfied")


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _pick(mapping: Any, *keys: str, default: Any = None) -> Any:
    """First present, non-``None`` value among ``keys`` of a mapping."""
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_text(payload: Any) -> str:
    """Deterministic JSON text (sorted keys, UTF-8, trailing newline)."""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_text(payload), encoding="utf-8", newline="\n")
    return path


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


# ---------------------------------------------------------------------------
# Snapshot / input binding
# ---------------------------------------------------------------------------


def snapshot_to_document(snapshot: Any) -> dict[str, Any]:
    """Accept a :class:`model.Snapshot` or a plain snapshot dict."""
    if snapshot is None:
        return {}
    if hasattr(snapshot, "to_dict"):
        try:
            return dict(snapshot.to_dict())
        except Exception:  # pragma: no cover - defensive against odd adapters
            pass
    return _as_dict(snapshot)


def _build_input_block(snapshot_doc: dict[str, Any]) -> dict[str, Any]:
    meta = _as_dict(snapshot_doc.get("build_meta"))
    return {
        "build": _as_dict(meta.get("build")),
        "dataset": _as_dict(meta.get("dataset")),
        "calc_input": _as_dict(meta.get("calc_input")),
        "fingerprints": {
            key: meta[key]
            for key in (
                "build_spec_sha256",
                "content_sha256",
                "calc_input_sha256",
                "database_sha256",
                "dataset_logical_content_sha256",
                "current_pointer_sha256",
                "source_raw_sha256",
            )
            if meta.get(key) is not None
        },
    }


def _snapshot_summary(
    snapshot_doc: dict[str, Any], ledger_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Lightweight snapshot view for result.json (the full copy is a separate file)."""
    by_source_kind: dict[str, int] = {}
    for row in ledger_rows:
        kind = _text(row.get("source_kind"), "unknown")
        by_source_kind[kind] = by_source_kind.get(kind, 0) + 1
    return {
        "build_meta": _as_dict(snapshot_doc.get("build_meta")),
        "character": _as_dict(snapshot_doc.get("character")),
        "attributes": _as_dict(snapshot_doc.get("attributes")),
        "resistances": _as_dict(snapshot_doc.get("resistances")),
        "derived": _as_dict(snapshot_doc.get("derived")),
        "contribution_summary": {
            "total": len(ledger_rows),
            "active": sum(1 for row in ledger_rows if row.get("active")),
            "included": sum(1 for row in ledger_rows if row.get("included")),
            "by_source_kind": dict(sorted(by_source_kind.items())),
        },
        "excluded_sources": list(snapshot_doc.get("excluded_sources") or []),
        "issues": list(snapshot_doc.get("issues") or []),
    }


# ---------------------------------------------------------------------------
# Source ledger (from snapshot.contributions)
# ---------------------------------------------------------------------------


def _source_location(source_path: str) -> Optional[str]:
    parts = source_path.split(".")
    if source_path.startswith("data.equipment."):
        return ".".join(parts[:3])
    if source_path.startswith("data.idols.entries."):
        return f"idol[{parts[3]}]" if len(parts) > 3 else source_path
    if source_path.startswith("data.blessings.entries."):
        return f"blessing[{parts[3]}]" if len(parts) > 3 else source_path
    if source_path.startswith("data.passive_tree.nodes."):
        return f"passive node mg-1:{parts[3]}" if len(parts) > 3 else source_path
    if source_path.startswith("data.specializations."):
        return ".".join(parts[:4])
    return source_path or None


def build_source_ledger_rows(snapshot_doc: dict[str, Any]) -> list[dict[str, Any]]:
    """One flat, display-ready row per snapshot contribution (deterministic order)."""
    contributions = [c for c in (snapshot_doc.get("contributions") or []) if isinstance(c, dict)]
    contributions.sort(
        key=lambda c: (
            _text(c.get("source_kind")),
            _text(c.get("source_path")),
            _text(c.get("contribution_id")),
        )
    )
    rows: list[dict[str, Any]] = []
    for number, contribution in enumerate(contributions, start=1):
        evidence = [e for e in (contribution.get("evidence") or []) if isinstance(e, dict)]
        condition_status = _text(contribution.get("condition_status"), "always")
        included = bool(contribution.get("included", True))
        source_path = _text(contribution.get("source_path"))
        rows.append(
            {
                "row_id": f"sl-{number:04d}",
                "contribution_id": _text(contribution.get("contribution_id"), f"contrib-{number:04d}"),
                "source_id": _text(contribution.get("contribution_id"), f"contrib-{number:04d}"),
                "source_kind": _text(contribution.get("source_kind"), "system"),
                "source_label": _text(contribution.get("source_label")),
                "source_path": source_path,
                "source_location": _source_location(source_path),
                "stat_key": _text(contribution.get("stat_key"), "unsupported"),
                "stat_name": _text(contribution.get("stat_name")),
                "operation": _text(contribution.get("operation"), "flag"),
                "value": _num(contribution.get("value")),
                "unit": _text(contribution.get("unit"), "flat"),
                "damage_tags": list(contribution.get("damage_tags") or []),
                "target": contribution.get("target"),
                "condition_code": contribution.get("condition_code"),
                "condition_status": condition_status,
                "included": included,
                "active": included and condition_status in _ACTIVE_CONDITIONS,
                "exclusion_reason": contribution.get("exclusion_reason"),
                "value_source": _text(contribution.get("value_source"), "source"),
                "status": _text(contribution.get("status"), "confirmed"),
                "evidence_refs": [
                    _text(_pick(e, "locator", "detail", default="")) for e in evidence
                ],
            }
        )
    return rows


def summarize_source_ledger(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    by_source_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_operation: dict[str, int] = {}
    by_condition: dict[str, int] = {}
    for row in rows:
        kind = _text(row.get("source_kind"), "unknown")
        by_source_kind[kind] = by_source_kind.get(kind, 0) + 1
        status = _text(row.get("status"), "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        operation = _text(row.get("operation"), "unknown")
        by_operation[operation] = by_operation.get(operation, 0) + 1
        condition = row.get("condition_code")
        if condition:
            key = f"{condition}={row.get('condition_status')}"
            by_condition[key] = by_condition.get(key, 0) + 1
    return {
        "total_rows": len(rows),
        "included_rows": sum(1 for row in rows if row.get("included")),
        "active_rows": sum(1 for row in rows if row.get("active")),
        "excluded_rows": sum(1 for row in rows if not row.get("included")),
        "by_source_kind": dict(sorted(by_source_kind.items())),
        "by_status": dict(sorted(by_status.items())),
        "by_operation": dict(sorted(by_operation.items())),
        "by_condition": dict(sorted(by_condition.items())),
        "unresolved_rows": sum(1 for row in rows if row.get("status") == "unresolved"),
    }


# ---------------------------------------------------------------------------
# Ruleset / rule ledger
# ---------------------------------------------------------------------------


def ruleset_to_document(ruleset: Any) -> dict[str, Any]:
    """Accept ``hit_ruleset.load_ruleset`` objects, ``{"data": ...}`` or dicts."""
    if ruleset is None:
        return {}
    if isinstance(ruleset, dict):
        data = dict(ruleset)
        inner = data.get("data")
        if isinstance(inner, dict) and not data.get("rules"):
            merged = dict(inner)
            merged.setdefault("path", data.get("path"))
            return merged
        return data
    if hasattr(ruleset, "to_dict"):
        try:
            return dict(ruleset.to_dict())
        except Exception:  # pragma: no cover - defensive
            pass
    if hasattr(ruleset, "data"):
        return _as_dict(getattr(ruleset, "data"))
    return {}


def ruleset_binding(ruleset: Any, *, ruleset_path: Optional[str] = None) -> dict[str, Any]:
    data = ruleset_to_document(ruleset)
    obj = ruleset
    return {
        "id": _text(_pick(data, "ruleset_id", "id", default=getattr(obj, "ruleset_id", None))),
        "path": _text(ruleset_path or _pick(data, "path", default=getattr(obj, "path", None))),
        "sha256": _pick(data, "sha256", default=getattr(obj, "sha256", None)),
        "game_version": _text(
            _pick(data, "game_version", "target_game_version", default=getattr(obj, "game_version", None))
        ),
        "schema": _text(_pick(data, "schema", default=getattr(obj, "schema", None))),
    }


def build_rule_ledger_rows(ruleset: Any) -> list[dict[str, Any]]:
    data = ruleset_to_document(ruleset)
    rows: list[dict[str, Any]] = []
    for number, rule in enumerate(data.get("rules") or [], start=1):
        if not isinstance(rule, dict):
            continue
        rows.append(
            {
                "rule_id": _text(_pick(rule, "rule_id", "id", default=f"rule-{number:03d}")),
                "title": _text(_pick(rule, "title", "name")),
                "statement": _text(_pick(rule, "statement", "detail", "description")),
                "status": _text(rule.get("status"), "unresolved"),
                "group": _pick(rule, "group", "scope"),
                "affects": list(rule.get("affects") or []),
                "evidence": list(rule.get("evidence") or []),
                "assumption": rule.get("assumption"),
            }
        )
    return rows


def summarize_rule_ledger(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    by_status: dict[str, int] = {}
    for row in rows:
        status = _text(row.get("status"), "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "total_rows": len(rows),
        "by_status": dict(sorted(by_status.items())),
        "blocking_rows": sum(1 for row in rows if row.get("status") in BLOCKING_STATUSES),
    }


def _blocking_effects(ruleset: Any) -> list[dict[str, Any]]:
    data = ruleset_to_document(ruleset)
    blocking: list[dict[str, Any]] = []
    for effect in data.get("effects") or []:
        if not isinstance(effect, dict):
            continue
        status = _text(_pick(effect, "status", "condition_status"))
        if status in BLOCKING_STATUSES:
            blocking.append(effect)
    return blocking


# ---------------------------------------------------------------------------
# Engine output normalisation
# ---------------------------------------------------------------------------


def _normalize_component(component: Any, index: int) -> dict[str, Any]:
    source = _as_dict(component)
    out: dict[str, Any] = {}
    for key, value in source.items():
        out[key] = value
    out["component_id"] = _text(
        _pick(source, "component_id", "id", "key", default=f"component-{index:02d}")
    )
    out["label"] = _text(_pick(source, "label", "name", "component_label", default=""))
    out["damage_type"] = _text(_pick(source, "damage_type", "type", default=""))
    out["damage_tags"] = list(source.get("damage_tags") or [])
    out["base"] = _num(_pick(source, "base", "base_damage"))
    out["added_scaled"] = _num(_pick(source, "added_scaled", "added", "added_damage_scaled"))
    out["increased"] = _num(_pick(source, "increased", "increased_total", "inc"))
    out["more_product"] = _num(_pick(source, "more_product", "more", "more_multiplier", default=1.0), 1.0)
    out["avg_crit"] = _num(_pick(source, "avg_crit", "crit_factor", default=1.0), 1.0)
    out["crit_chance"] = _num(_pick(source, "crit_chance"))
    out["crit_multiplier"] = _num(_pick(source, "crit_multiplier", "crit_multi", default=1.0), 1.0)
    out["penetration"] = _num(_pick(source, "penetration", "penetration_multiplier", default=1.0), 1.0)
    out["target_multiplier"] = _num(_pick(source, "target_multiplier", "target", default=1.0), 1.0)
    out["damage"] = _num(_pick(source, "damage", "hit_damage", "total"))
    return out


def _optional_number(mapping: Any, *keys: str) -> Optional[float]:
    """First non-``None`` value among ``keys`` as float; ``None`` stays unknown."""
    value = _pick(mapping, *keys)
    return None if value is None else _num(value)


def _normalize_single_hit(engine_output: Any, snapshot_doc: dict[str, Any]) -> dict[str, Any]:
    raw = _as_dict(engine_output)
    components_in = _pick(raw, "components", "parts", "hits", default=[]) or []
    components = [
        _normalize_component(component, index)
        for index, component in enumerate(components_in if isinstance(components_in, list) else [])
    ]
    totals_in = _as_dict(raw.get("totals"))
    damage_sum = sum(_num(component.get("damage")) for component in components)
    totals = {
        "base": _num(_pick(totals_in, "base", "base_damage")),
        "added_scaled": _num(_pick(totals_in, "added_scaled", "added", "added_damage_scaled")),
        "increased_total": _num(_pick(totals_in, "increased_total", "increased", "inc")),
        # multi-component totals legitimately leave these per-component multipliers
        # unknown (the engine returns None); keep None instead of faking 1.0.
        "more_product": _optional_number(totals_in, "more_product", "more"),
        "avg_crit": _num(_pick(totals_in, "avg_crit", "crit_factor", default=1.0), 1.0),
        "crit_chance": _optional_number(totals_in, "crit_chance"),
        "crit_multiplier": _optional_number(totals_in, "crit_multiplier", "crit_multi"),
        "penetration_multiplier": _optional_number(
            totals_in, "penetration_multiplier", "penetration"
        ),
        "target_multiplier": _num(_pick(totals_in, "target_multiplier", "target", default=1.0), 1.0),
        "damage": _num(
            _pick(totals_in, "damage", "single_hit_damage", "total", "hit_damage"),
            damage_sum,
        ),
    }
    for key, value in totals_in.items():
        totals.setdefault(key, value)
    derived = _as_dict(snapshot_doc.get("derived"))
    skill = _as_dict(derived.get("skill"))
    out = {
        "label": _text(_pick(raw, "label", "name", default=skill.get("name") or "single hit")),
        "components": components,
        "totals": totals,
        "assumptions": [str(item) for item in (raw.get("assumptions") or [])],
        "states": _as_dict(raw.get("states")),
        "rules_applied": list(raw.get("rules_applied") or []),
    }
    for key in (
        "excluded_components",
        "applied_effects",
        "not_modeled_effects",
        "unresolved_effects",
        "excluded_contributions",
    ):
        value = raw.get(key)
        if value is not None:
            out[key] = value
    return out


def _normalize_hits_per_cast(
    raw: Any,
    derived_hit_count: dict[str, Any],
    scenario: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        raw = {"per_cast": int(raw)}
    source = _as_dict(raw)
    per_cast = int(_num(_pick(source, "per_cast", "hits_per_cast", "hits", default=0)))
    returns = int(_num(_pick(source, "returns", "return_hits", default=0)))
    sources_value = _pick(source, "sources", "source_nodes", default=None)
    status = _text(_pick(source, "status", default=_pick(derived_hit_count, "status", default="assumed")))
    if per_cast <= 0:
        per_cast = int(_num(_pick(derived_hit_count, "per_cast", "hits_per_cast", default=0)))
    if returns <= 0:
        returns = int(_num(_pick(derived_hit_count, "returns", default=max(0, per_cast - 1))))
    if sources_value is None:
        sources_value = _pick(derived_hit_count, "sources", default=[])
    weights_raw = _pick(source, "damage_weights", default=None)
    if weights_raw is None:
        weights_raw = _pick(derived_hit_count, "damage_weights", default=None)
    weights = [float(value) for value in (weights_raw or [])]
    if len(weights) != per_cast:
        weights = [1.0] * per_cast
    effective = _pick(source, "effective_hits_per_cast", default=None)
    if effective is None:
        effective = _pick(derived_hit_count, "effective_hits_per_cast", default=sum(weights))
    # R2: the scenario that was actually computed wins over the static snapshot
    # block (serve puts the applied hit_override into the merged scenario).
    override = _as_dict(_as_dict(scenario).get("hit_override"))
    if override:
        override_hits = int(_num(override.get("hits_per_cast"), per_cast) or per_cast)
        override_weights = [float(value) for value in (override.get("damage_weights") or [])]
        if override_hits > 0:
            per_cast = override_hits
            returns = max(0, per_cast - 1)
            if len(override_weights) == per_cast:
                weights = override_weights
            elif override_weights:
                weights = override_weights[:per_cast] + [1.0] * max(0, per_cast - len(override_weights))
            else:
                weights = [1.0] * per_cast
            effective = sum(weights)
            status = "confirmed"
    return {
        "per_cast": per_cast,
        "returns": returns,
        "sources": list(sources_value or []),
        "status": status or "assumed",
        "label": _text(_pick(source, "label", default="每次施放对同一目标的命中次数")),
        "damage_weights": weights,
        "effective_hits_per_cast": _num(effective, float(sum(weights))),
    }


def _normalize_attack_rate(raw: Any) -> dict[str, Any]:
    source = _as_dict(raw)
    out = {
        "base_aps": _num(_pick(source, "base_aps", "base_attack_rate", "attack_rate")),
        "increased_attack_speed": _num(_pick(source, "increased_attack_speed", "inc_attack_speed")),
        "more_attack_speed": _num(_pick(source, "more_attack_speed", "more", default=1.0), 1.0),
        "final_aps": _num(_pick(source, "final_aps", "aps", "attacks_per_second", "final_attack_rate")),
        "source": _pick(source, "source", "sources", default=[]),
        "formula": _text(_pick(source, "formula", "note")),
    }
    for key, value in source.items():
        out.setdefault(key, value)
    return out


def _normalize_windows(raw_windows: Any) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for item in raw_windows or []:
        source = _as_dict(item)
        window_s = _num(_pick(source, "window_s", "seconds", "window"))
        total = _num(_pick(source, "total_damage", "damage", "total"))
        dps = _num(_pick(source, "dps"))
        if dps <= 0 and window_s > 0 and total > 0:
            dps = total / window_s
        entry = {
            "window_s": window_s,
            "window_id": _text(_pick(source, "window_id", "id", default=f"{window_s:g}s")),
            "hits": _num(_pick(source, "hits", "hit_count")),
            "total_damage": total,
            "dps": dps,
        }
        for key, value in source.items():
            entry.setdefault(key, value)
        windows.append(entry)
    windows.sort(key=lambda window: window["window_s"])
    return windows


def _normalize_dps(
    engine_output: Any,
    snapshot_doc: dict[str, Any],
    hits: dict[str, Any],
    scenario: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    raw = _as_dict(engine_output)
    attack_rate = _normalize_attack_rate(_pick(raw, "attack_rate", "aps", default={}))
    if attack_rate.get("final_aps") and hits.get("per_cast"):
        attack_rate["hits_per_second"] = attack_rate["final_aps"] * hits["per_cast"]
    policy = _pick(raw, "policy", default=None)
    if not isinstance(policy, dict):
        scenario_policy = _pick(_as_dict(_as_dict(scenario).get("dps")), "policy", default=None)
        if scenario_policy:
            policy = {
                "id": _text(scenario_policy),
                "label": "场景声明的输出策略（rotation 未由引擎展开）",
            }
        else:
            policy = {
                "id": _text(_pick(raw, "policy_id", default="single_target")),
                "label": "单一目标持续攻击（无移动/无中断）",
            }
    alternate = _pick(raw, "alternate_policy", default=None)
    scenario_doc = _as_dict(scenario)
    scenario_windows = list(_as_dict(scenario_doc.get("dps")).get("windows_s") or [])
    resource_note = _text(_pick(raw, "resource_note", "resource"))
    windows = _normalize_windows(_pick(raw, "windows", "dps_windows", default=[]))
    policy_detail = dict(policy)
    if not policy_detail.get("casts_per_second"):
        policy_detail["casts_per_second"] = _pick(raw, "effective_casts_per_second")
    if not policy_detail.get("hits_per_second"):
        policy_detail["hits_per_second"] = _pick(raw, "effective_hits_per_second")
    if not policy_detail.get("damage_per_second"):
        policy_detail["damage_per_second"] = _pick(raw, "damage_per_second")
    return {
        "attack_rate": attack_rate,
        "windows": windows,
        "scenario_windows_s": scenario_windows,
        "policy": policy_detail,
        "alternate_policy": dict(_as_dict(alternate)) if alternate else None,
        "damage_weights": list(hits.get("damage_weights") or []),
        "effective_hits_per_cast": _num(
            _pick(hits, "effective_hits_per_cast", default=0.0)
        ),
        "casts_per_second": _num(policy_detail.get("casts_per_second")),
        "hits_per_second": _num(policy_detail.get("hits_per_second")),
        "damage_per_second": _num(policy_detail.get("damage_per_second")),
        "damage_per_cast": _num(policy_detail.get("damage_per_cast")),
        "rotation_more_factor": _num(policy_detail.get("rotation_more_factor"), 1.0),
        "resource_note": resource_note,
        "assumptions": [str(item) for item in (raw.get("assumptions") or [])],
    }


def _dps_windows_map(dps: dict[str, Any]) -> dict[str, float]:
    return {
        f"{_num(window.get('window_s')):g}s": _num(window.get("dps"))
        for window in dps.get("windows") or []
    }


def _normalize_baseline_outputs(
    raw: Any,
    snapshot_doc: dict[str, Any],
    hits: dict[str, Any],
    scenario: Optional[dict[str, Any]],
) -> dict[str, Any]:
    profiles: dict[str, Any] = {}
    for profile_id, payload in _as_dict(raw).items():
        block = _as_dict(payload)
        single = _normalize_single_hit(block.get("single_hit"), snapshot_doc)
        dps = _normalize_dps(block.get("simulate"), snapshot_doc, hits, scenario)
        profiles[str(profile_id)] = {
            "single_hit_damage": _num(single["totals"].get("damage")),
            "more_product": _optional_number(single["totals"], "more_product"),
            "crit_chance": _optional_number(single["totals"], "crit_chance"),
            "avg_crit": _num(single["totals"].get("avg_crit"), 1.0),
            "dps_windows": _dps_windows_map(dps),
            "policy": _pick(dps["policy"], "id"),
            "casts_per_second": _num(dps.get("casts_per_second")),
            "hits_per_second": _num(dps.get("hits_per_second")),
            "damage_per_cast": _num(dps.get("damage_per_cast")),
            "damage_per_second": _num(dps.get("damage_per_second")),
        }
    return profiles


def _normalize_sensitivity(
    raw: Any,
    snapshot_doc: dict[str, Any],
    hits: dict[str, Any],
    scenario: Optional[dict[str, Any]],
    primary_single: float,
    primary_dps: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in raw or []:
        block = _as_dict(item)
        single = _normalize_single_hit(block.get("single_hit"), snapshot_doc)
        dps_block = _normalize_dps(block.get("simulate"), snapshot_doc, hits, scenario)
        windows = _dps_windows_map(dps_block)
        single_damage = _num(single["totals"].get("damage"))
        dps_value = _num(windows.get("5s"))
        rows.append(
            {
                "id": _text(block.get("id")),
                "label": _text(block.get("label")),
                "states": _as_dict(block.get("states")),
                "single_hit_damage": single_damage,
                "more_product": _optional_number(single["totals"], "more_product"),
                "crit_chance": _optional_number(single["totals"], "crit_chance"),
                "avg_crit": _num(single["totals"].get("avg_crit"), 1.0),
                "dps_windows": windows,
                "single_rel": (single_damage / primary_single - 1.0) if primary_single else None,
                "dps_rel": (dps_value / primary_dps - 1.0) if primary_dps else None,
            }
        )
    return rows


def _normalize_candidate(candidate: Any, index: int) -> dict[str, Any]:
    source = _as_dict(candidate)
    modeled = bool(_pick(source, "modeled", "included_in_baseline", default=False))
    out: dict[str, Any] = dict(source)
    out.update(
        {
            "buff_id": _text(_pick(source, "buff_id", "effect_id", "id", default=f"candidate-{index:02d}")),
            "label": _text(_pick(source, "label", "name")),
            "source_kind": _text(source.get("source_kind")),
            "source_path": _text(source.get("source_path")),
            "condition": source.get("condition"),
            "condition_status": _text(source.get("condition_status")),
            "status": _text(_pick(source, "status", "support_level", default="unresolved")),
            "modeled": modeled,
            "included_in_baseline": bool(
                _pick(source, "included_in_baseline", "modeled", default=modeled)
            ),
            "evidence": list(source.get("evidence") or []),
            "next_probe": source.get("next_probe"),
            "notes": source.get("notes"),
        }
    )
    return out


def _scenario_state_map(scenario: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Merged state switches for the lead-owned ``le-hit-scenario-v1`` shape.

    Supported layouts (later wins):
    ``profiles[baseline_profile].states`` → ``single_hit.states`` → ``dps.states``
    → top-level ``states``.  A plain ``{states: {...}}`` document keeps working.
    """
    scenario_doc = _as_dict(scenario)
    merged: dict[str, Any] = {}
    profile_id = _text(scenario_doc.get("baseline_profile"))
    profiles = _as_dict(scenario_doc.get("profiles"))
    if profile_id and isinstance(profiles.get(profile_id), dict):
        merged.update(_as_dict(profiles[profile_id].get("states")))
    merged.update(_as_dict(_as_dict(scenario_doc.get("single_hit")).get("states")))
    merged.update(_as_dict(_as_dict(scenario_doc.get("dps")).get("states")))
    merged.update(_as_dict(scenario_doc.get("states")))
    return merged


def _applied_states(snapshot_doc: dict[str, Any], scenario: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    derived = _as_dict(snapshot_doc.get("derived"))
    merged: dict[str, Any] = {}
    merged.update(_as_dict(derived.get("applied_states")))
    merged.update(_scenario_state_map(scenario))
    rows: list[dict[str, Any]] = []
    for key in sorted(merged):
        value = merged[key]
        if isinstance(value, bool):
            active = value
        elif isinstance(value, (int, float)):
            active = float(value) > 0
        else:
            active = bool(value)
        rows.append({"state": key, "value": value, "active": active})
    return rows


def _normalize_buffs(
    screening: Any,
    snapshot_doc: dict[str, Any],
    scenario: Optional[dict[str, Any]],
) -> dict[str, Any]:
    raw = _as_dict(screening)
    candidates_in = raw.get("candidates") or []
    candidates = [
        _normalize_candidate(candidate, index)
        for index, candidate in enumerate(candidates_in if isinstance(candidates_in, list) else [])
    ]
    summary = _as_dict(raw.get("summary"))
    if not summary:
        summary = {
            "total": len(candidates),
            "modeled": sum(1 for candidate in candidates if candidate["modeled"]),
            "included_in_baseline": sum(
                1 for candidate in candidates if candidate["included_in_baseline"]
            ),
            "unresolved": sum(1 for candidate in candidates if candidate["status"] == "unresolved"),
            "not_modeled": sum(1 for candidate in candidates if not candidate["modeled"]),
        }
    return {
        "applied": _applied_states(snapshot_doc, scenario),
        "screening": {
            "schema": _text(raw.get("schema"), BUFF_SCREENING_SCHEMA),
            "summary": summary,
            "candidates": candidates,
        },
    }


# ---------------------------------------------------------------------------
# Issues / status / consistency
# ---------------------------------------------------------------------------


def _issue(issue_id: str, severity: str, title: str, detail: str, scope: str) -> dict[str, Any]:
    return {
        "issue_id": issue_id,
        "severity": severity,
        "title": title,
        "detail": detail,
        "scope": scope,
    }


def _collect_issues(
    *,
    snapshot_doc: dict[str, Any],
    ruleset: Any,
    rule_rows: list[dict[str, Any]],
    single: dict[str, Any],
    dps: dict[str, Any],
    buffs: dict[str, Any],
    consistency: dict[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(issue_id: str, severity: str, title: str, detail: str, scope: str) -> None:
        base = issue_id or "issue-hit"
        candidate = base
        suffix = 2
        while candidate in seen:
            candidate = f"{base}-{suffix}"
            suffix += 1
        seen.add(candidate)
        issues.append(_issue(candidate, severity, title, detail, scope))

    for number, issue in enumerate(snapshot_doc.get("issues") or [], start=1):
        if not isinstance(issue, dict):
            continue
        add(
            _text(_pick(issue, "issue_id", "id", default=f"issue-snapshot-{number:03d}")),
            _text(_pick(issue, "severity", default="info")),
            _text(_pick(issue, "title", "name", default="快照问题")),
            _text(_pick(issue, "detail", "message")),
            _text(_pick(issue, "scope", default="snapshot")),
        )

    for status in BLOCKING_STATUSES:
        group = [row for row in rule_rows if row.get("status") == status]
        if not group:
            continue
        listing = "；".join(
            f"{row.get('rule_id')}: {row.get('statement')}" for row in group[:12]
        )
        if len(group) > 12:
            listing += f"；…（共 {len(group)} 条，见 rule-ledger.json）"
        add(
            f"issue-rules-{status}",
            "warning" if status == "assumed" else "error",
            f"规则账含 {len(group)} 条 {status} 规则",
            listing,
            "ruleset",
        )

    blocking_effects = _blocking_effects(ruleset)
    if blocking_effects:
        listing = ", ".join(
            _text(_pick(effect, "id", "effect_id", "label", default="?")) for effect in blocking_effects[:8]
        )
        add(
            "issue-effects-blocking",
            "warning",
            f"ruleset.effects 含 {len(blocking_effects)} 条 assumed/unresolved 条件效果",
            f"条件效果未全部确认（{listing}）；baseline 只计入已建模部分。",
            "buffs",
        )

    unresolved_candidates = [
        candidate for candidate in buffs["screening"]["candidates"] if candidate.get("status") == "unresolved"
    ]
    if unresolved_candidates:
        add(
            "issue-buff-screening-unresolved",
            "warning",
            f"条件 Buff 筛查有 {len(unresolved_candidates)} 条 unresolved 候选",
            ", ".join(candidate["buff_id"] for candidate in unresolved_candidates[:10]),
            "buffs",
        )

    # Engine assumptions: dedupe across single-hit/DPS (the DPS call re-runs the
    # single hit) and drop the "rule ..." lines already represented by the rule
    # ledger, so the issues list stays readable in result.json/HTML.
    engine_assumptions: list[str] = []
    seen_engine: set[str] = set()
    for assumption in list(single.get("assumptions") or []) + list(dps.get("assumptions") or []):
        text = str(assumption).strip()
        if not text or text in seen_engine:
            continue
        if text.startswith("rule ") and "(" in text:
            continue
        seen_engine.add(text)
        engine_assumptions.append(text)
    if engine_assumptions:
        detail = "；".join(engine_assumptions)
        if len(detail) > 2400:
            detail = detail[:2400] + "…（完整列表见 result.single_hit.assumptions / result.dps.assumptions）"
        add(
            "issue-engine-assumptions",
            "warning",
            f"引擎显式假设 {len(engine_assumptions)} 条",
            detail,
            "engine",
        )

    excluded_components = single.get("excluded_components") or []
    if excluded_components:
        add(
            "issue-excluded-components",
            "warning",
            f"技能基础伤害有 {len(excluded_components)} 个伤害类型未在 ruleset 中建模",
            json.dumps(excluded_components, ensure_ascii=False)[:800],
            "single_hit",
        )

    if not consistency.get("all_pass", True):
        failed = sorted(key for key, value in consistency.items() if value is False)
        add(
            "issue-consistency",
            "error",
            "结果一致性校验失败",
            json.dumps(failed, ensure_ascii=False),
            "result",
        )
    return issues


def _binding_matches(snapshot_doc: dict[str, Any], input_block: dict[str, Any]) -> bool:
    """``result.input`` must repeat the fingerprints recorded in ``snapshot.build_meta``."""
    meta = _as_dict(snapshot_doc.get("build_meta"))
    build = _as_dict(input_block.get("build"))
    dataset = _as_dict(input_block.get("dataset"))
    calc = _as_dict(input_block.get("calc_input"))
    checks: list[bool] = []
    content_sha = build.get("content_sha256")
    if content_sha is not None or meta.get("content_sha256") is not None:
        checks.append(content_sha == meta.get("content_sha256"))
    calc_sha = calc.get("sha256")
    if calc_sha is not None or meta.get("calc_input_sha256") is not None:
        checks.append(calc_sha == meta.get("calc_input_sha256"))
    dataset_id = dataset.get("dataset_id")
    if dataset_id is not None or _as_dict(meta.get("dataset")).get("dataset_id") is not None:
        checks.append(dataset_id == _as_dict(meta.get("dataset")).get("dataset_id"))
    return all(checks) if checks else True


def consistency_checks(
    *,
    snapshot_doc: dict[str, Any],
    input_block: dict[str, Any],
    single_hit: dict[str, Any],
    dps: dict[str, Any],
    hits_per_cast: dict[str, Any],
) -> dict[str, Any]:
    """The consistency gates copied into ``run-manifest.json`` (DESIGN §5.3)."""
    components = list(single_hit.get("components") or [])
    component_sum = sum(_num(component.get("damage")) for component in components)
    total = _num((single_hit.get("totals") or {}).get("damage"))
    tolerance = 1e-6 * max(1.0, abs(component_sum), abs(total))
    components_sum_ok = abs(component_sum - total) <= tolerance
    if components and total == 0 and component_sum > 0:  # inconsistent payload
        components_sum_ok = False

    windows = list(dps.get("windows") or [])
    ordered = all(
        windows[index]["window_s"] < windows[index + 1]["window_s"]
        and windows[index]["total_damage"] <= windows[index + 1]["total_damage"] + 1e-6
        for index in range(len(windows) - 1)
    )
    window_seconds = {round(float(window.get("window_s") or 0.0), 6) for window in windows}
    complete = all(round(required, 6) in window_seconds for required in REQUIRED_WINDOWS)
    dps_positive = bool(windows) and all(
        float(window.get("dps") or 0.0) >= 0.0 for window in windows
    )
    checks = {
        "single_hit_components_sum_equals_total": bool(components_sum_ok),
        "dps_windows_ordered": bool(ordered),
        "dps_windows_complete": bool(complete),
        "dps_windows_non_negative": bool(dps_positive),
        "snapshot_binding_matches_result_input": bool(_binding_matches(snapshot_doc, input_block)),
        "hits_per_cast_positive": int(hits_per_cast.get("per_cast") or 0) > 0,
    }
    checks["all_pass"] = all(checks.values())
    return checks


def _run_status(
    *,
    ruleset: Any,
    rule_rows: list[dict[str, Any]],
    snapshot_doc: dict[str, Any],
    single_hit: dict[str, Any],
    dps: dict[str, Any],
    buffs: dict[str, Any],
) -> tuple[str, str]:
    blocking_rules = [row for row in rule_rows if row.get("status") in BLOCKING_STATUSES]
    blocking_effects = _blocking_effects(ruleset)
    blocking_contributions = [
        contribution
        for contribution in (snapshot_doc.get("contributions") or [])
        if isinstance(contribution, dict)
        and contribution.get("status") in BLOCKING_STATUSES
        and contribution.get("included", True)
    ]
    unresolved_candidates = [
        candidate
        for candidate in buffs["screening"]["candidates"]
        if candidate.get("status") == "unresolved"
    ]
    engine_assumptions = len(single_hit.get("assumptions") or []) + len(dps.get("assumptions") or [])
    data = ruleset_to_document(ruleset)
    open_questions = list(data.get("open_questions") or [])

    reasons: list[str] = []
    if blocking_rules:
        reasons.append(f"rules 中 {len(blocking_rules)} 条 assumed/unresolved")
    if blocking_effects:
        reasons.append(f"effects 中 {len(blocking_effects)} 条 assumed/unresolved")
    if blocking_contributions:
        reasons.append(f"contributions 中 {len(blocking_contributions)} 条 assumed/unresolved")
    if unresolved_candidates:
        reasons.append(f"buff 筛查 {len(unresolved_candidates)} 条 unresolved")
    if engine_assumptions:
        reasons.append(f"引擎显式假设 {engine_assumptions} 条")
    if open_questions:
        reasons.append(f"ruleset.open_questions {len(open_questions)} 条")

    ruleset_id = ruleset_binding(ruleset).get("id") or "hit ruleset"
    if reasons:
        return (
            "assumption",
            f"{ruleset_id} 尚未全部确认（" + "；".join(reasons) + "）；结果不能标为精确值，"
            "也不代表完整 BD 实战 DPS。",
        )
    return "ok", f"{ruleset_id} 的规则全部为 confirmed/derived，且无未解决条件效果。"


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------


def build_hit_result(
    snapshot: Any,
    ruleset: Any,
    case: Any,
    *,
    run_id: str,
    generated_at: str,
    ruleset_path: Optional[str] = None,
    out_dir: Optional[str] = None,
    scenario: Optional[dict[str, Any]] = None,
    engine_outputs: Optional[dict[str, Any]] = None,
    repro_command: Optional[str] = None,
    scenario_path: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble the ``le-hit-result-v1`` document from engine outputs + snapshot.

    Parameters
    ----------
    snapshot:
        :class:`le_calc.model.Snapshot` or the snapshot JSON dict
        (``hit_inputs.build_snapshot``).
    ruleset:
        ``hit_ruleset.load_ruleset`` object or the raw ruleset JSON dict.
    case:
        The case configuration dict (``le-case-v1``); used for scope metadata.
    engine_outputs:
        ``{"single_hit": ..., "simulate": ..., "buff_screening": ...}`` — the
        return values of the engine functions.  Missing blocks degrade to empty
        structures instead of raising (the consistency gate then reports it).
    """

    snapshot_doc = snapshot_to_document(snapshot)
    ruleset_doc = ruleset_to_document(ruleset)
    case_doc = _as_dict(case)
    outputs = _as_dict(engine_outputs)

    source_rows = build_source_ledger_rows(snapshot_doc)
    rule_rows = build_rule_ledger_rows(ruleset)
    input_block = _build_input_block(snapshot_doc)
    binding = ruleset_binding(ruleset, ruleset_path=ruleset_path)
    derived = _as_dict(snapshot_doc.get("derived"))
    skill_raw = _as_dict(derived.get("skill"))
    main_skill = _as_dict(case_doc.get("main_skill"))

    single_hit = _normalize_single_hit(_pick(outputs, "single_hit", "compute_single_hit"), snapshot_doc)
    simulation_output = _pick(outputs, "simulate", "simulation")
    simulation_hits = _pick(_as_dict(simulation_output), "hits_per_cast", default=None)
    hits_per_cast = _normalize_hits_per_cast(
        simulation_hits if simulation_hits is not None else _pick(outputs, "hits_per_cast", default=None),
        _as_dict(derived.get("hit_count")),
        scenario,
    )
    dps = _normalize_dps(
        _pick(outputs, "simulate", "simulation"), snapshot_doc, hits_per_cast, scenario
    )
    buffs = _normalize_buffs(
        _pick(outputs, "buff_screening", "screening"),
        snapshot_doc,
        scenario,
    )
    primary_single = _num((single_hit.get("totals") or {}).get("damage"))
    primary_windows = _dps_windows_map(dps)
    primary_dps = _num(primary_windows.get("5s"))
    baseline_profiles = _normalize_baseline_outputs(
        _pick(outputs, "baselines", default={}),
        snapshot_doc,
        hits_per_cast,
        scenario,
    )
    sensitivity = _normalize_sensitivity(
        _pick(outputs, "buff_sensitivity", default=[]),
        snapshot_doc,
        hits_per_cast,
        scenario,
        primary_single,
        primary_dps,
    )
    buffs["sensitivity"] = sensitivity
    ruleset_data = _as_dict(ruleset_to_document(ruleset))
    open_questions = list(ruleset_data.get("open_questions") or [])
    baselines = {
        "profile": _text((_as_dict(scenario) or {}).get("baseline_profile")) or None,
        "profiles": baseline_profiles,
        "note": (
            "两个基线口径分别引用，不能相减当作“条件增益”。baseline_profile 决定报告主口径；"
            "legacy_single_skill 关闭常驻条件（Enchant Weapon active / Army of Skin），build_base 含常驻自动效果。"
        ),
    }
    consistency = consistency_checks(
        snapshot_doc=snapshot_doc,
        input_block=input_block,
        single_hit=single_hit,
        dps=dps,
        hits_per_cast=hits_per_cast,
    )
    status, status_reason = _run_status(
        ruleset=ruleset,
        rule_rows=rule_rows,
        snapshot_doc=snapshot_doc,
        single_hit=single_hit,
        dps=dps,
        buffs=buffs,
    )
    issues = _collect_issues(
        snapshot_doc=snapshot_doc,
        ruleset=ruleset,
        rule_rows=rule_rows,
        single=single_hit,
        dps=dps,
        buffs=buffs,
        consistency=consistency,
    )

    skill = {
        "ability_id": _text(_pick(skill_raw, "ability_id", default=main_skill.get("ability_id"))),
        "name": _text(
            _pick(skill_raw, "name", default=main_skill.get("name") or "Flame Reave")
        ),
        "class": _text(_pick(main_skill, "class", default=skill_raw.get("class"))),
        "mastery": _text(_pick(main_skill, "mastery", default=skill_raw.get("mastery"))),
        "tags": list(skill_raw.get("tags") or main_skill.get("damage_tags") or []),
        "base_damage": list(skill_raw.get("base_damage") or []),
        "added_damage_scaling": _num(skill_raw.get("added_damage_scaling")),
        "crit_chance": _num(skill_raw.get("crit_chance")),
        "crit_multiplier": _num(skill_raw.get("crit_multiplier"), 1.0),
        "mana_cost": _num(skill_raw.get("mana_cost")),
        "cast_time_s": _num(skill_raw.get("cast_time_s")),
        "use_duration_s": _num(_pick(skill_raw, "use_duration_s", "use_duration")),
        "attack_speed_base": skill_raw.get("attack_speed_base"),
        "cooldown_s": skill_raw.get("cooldown_s"),
        "hit": bool(skill_raw.get("hit", True)),
    }

    scenario_doc = _as_dict(scenario)
    profile_id = _text(scenario_doc.get("baseline_profile"))
    scenario_dps = _as_dict(scenario_doc.get("dps"))
    scenario_id = _text(_pick(scenario_doc, "scenario_id", "preset_id", default=""))
    if not scenario_id:
        scenario_id = f"{case_doc.get('case_id')}:{profile_id}" if profile_id else "baseline"
    scenario_name = _text(scenario_doc.get("name"))
    if not scenario_name:
        scenario_name = (
            f"{skill['name']} 单目标命中（{profile_id}）"
            if profile_id
            else "Flame Reave 单目标命中（baseline）"
        )
    scenario_block = {
        "case_id": case_doc.get("case_id"),
        "scenario_id": scenario_id,
        "name": scenario_name,
        "schema": _text(scenario_doc.get("schema")),
        "baseline_profile": profile_id or None,
        "source": "scenario" if scenario_doc else "baseline（ruleset 默认 applied_states）",
        "target": _as_dict(scenario_doc.get("target")),
        "states": buffs["applied"],
        "effects": list(scenario_doc.get("effects") or []),
        "policy": scenario_dps.get("policy"),
        "windows_s": list(scenario_dps.get("windows_s") or []),
        "rotation": _as_dict(scenario_dps.get("rotation")),
        "assumptions": list(scenario_doc.get("assumptions") or []),
        "binding": _as_dict(scenario_doc.get("binding")),
        "path": scenario_path,
        "notes": scenario_doc.get("notes"),
    }

    result: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "run": {
            "run_id": run_id,
            "generated_at": generated_at,
            "engine": {"id": ENGINE_ID, "version": __version__},
            "out_dir": out_dir,
            "ruleset": binding,
            "scope_note": SCOPE_NOTE,
            "status": status,
            "status_reason": status_reason,
            "repro_command": repro_command,
        },
        "input": input_block,
        "scope": {
            "model": "hit",
            "main_skill": main_skill or {
                "ability_id": skill["ability_id"],
                "name": skill["name"],
            },
            "pipeline": {
                "note": "现有 le_calc/le.ps1 管线的技能模型扩展（不是第二套管线）",
                "entrypoint": "le.ps1 run --case <case.json>（hit-snapshot/hit-run 只是内部步骤）",
                "reused": [
                    "case.json（le-case-v1）与单一入口 le.ps1",
                    "BuildSpec revision（data/builds/**）",
                    "calculation-input-v1",
                    "model.Snapshot + build_meta 指纹链（build/dataset/calc_input）",
                    "source/rule ledger 与 run-manifest 约定",
                    "离线单文件 HTML 报告约定",
                    "条件场景结构（le-hit-scenario-v1；由 ruleset.effects / 场景状态开关驱动）",
                ],
                "new": [
                    "Flame Reave hit 单次伤害模型（base/added/inc/more/crit/penetration/target）",
                    "攻速与命中次数模型（APS / hits_per_cast）",
                    "5/30/120s DPS 窗口与输出策略",
                    "条件 Buff 筛查（modeled/not_modeled/unresolved）",
                    "hit 报告渲染层与 le-hit-result-v1 结果契约",
                ],
            },
            "included": [
                f"主技能 {skill['name']}（{skill['ability_id']}）本体命中",
                "单次命中伤害构成（base/added/inc/more/crit/penetration/target）",
                "5/30/120s DPS 窗口与命中次数",
                "装备、神像、祝福、角色被动、技能树中进入 hit 模型的贡献",
                "条件 Buff 筛查（哪些效果已建模、哪些未建模/未解决）",
            ],
            "notes": [
                "不模拟其他技能、召唤物与持续伤害（除非规则包显式建模）",
                "不是完整 BD 实战 DPS：移动、走位、资源管理、目标行为均未建模",
                "assumed/unresolved 规则未确认前，结果只能作为假设级估算",
            ],
        },
        "scenario": scenario_block,
        "snapshot": _snapshot_summary(snapshot_doc, source_rows),
        "skill": skill,
        "single_hit": single_hit,
        "hits_per_cast": hits_per_cast,
        "dps": dps,
        "baselines": baselines,
        "buffs": buffs,
        "open_questions": open_questions,
        "user_confirmations": ruleset_data.get("user_confirmations"),
        "source_ledger": source_rows,
        "rule_ledger": rule_rows,
        "issues": issues,
        "consistency": consistency,
    }
    return result


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------


def _format_number(value: Any, digits: int = 2) -> str:
    number = _num(value, float("nan"))
    if number != number:  # NaN
        return ""
    if abs(number) >= 1000:
        return f"{number:,.{digits}f}"
    if number == int(number):
        return f"{int(number)}"
    return f"{number:,.{digits}f}"


def _multiplier_text(value: Any, digits: int = 4, *, unknown: str = "多组件（见明细）") -> str:
    """Display a multiplier; ``None`` means the engine kept it per-component."""
    if value is None:
        return unknown
    return f"×{_format_number(value, digits)}"


def render_readme(result: dict[str, Any]) -> str:
    run = _as_dict(result.get("run"))
    single = _as_dict(result.get("single_hit"))
    totals = _as_dict(single.get("totals"))
    dps = _as_dict(result.get("dps"))
    attack = _as_dict(dps.get("attack_rate"))
    hits = _as_dict(result.get("hits_per_cast"))
    skill = _as_dict(result.get("skill"))
    build = _as_dict(_as_dict(result.get("input")).get("build"))
    dataset = _as_dict(_as_dict(result.get("input")).get("dataset"))
    ruleset = _as_dict(run.get("ruleset"))
    buffs = _as_dict(result.get("buffs"))
    screening = _as_dict(buffs.get("screening"))
    summary = _as_dict(screening.get("summary"))
    lines: list[str] = []
    lines.append(f"# Flame Reave 命中伤害分析（{run.get('run_id')}）")
    lines.append("")
    lines.append(f"- 状态：**{run.get('status')}** — {run.get('status_reason', '')}")
    lines.append(
        f"- 主技能：`{skill.get('ability_id')}` {skill.get('name')} "
        f"（tags: {', '.join(skill.get('tags') or []) or 'n/a'}）"
    )
    lines.append(
        f"- 规则包：`{ruleset.get('id')}`（sha256 `{str(ruleset.get('sha256'))[:16]}…`，"
        f"游戏版本 {ruleset.get('game_version')}）"
    )
    lines.append(f"- 构筑：`{build.get('build_id')}` revision `{build.get('revision_id')}`")
    lines.append(f"- 数据集：`{dataset.get('dataset_id')}`（目标版本 {dataset.get('target_game_version')}）")
    lines.append(f"- 范围：{run.get('scope_note', '')}")
    if run.get("repro_command"):
        lines.append(f"- 复现：`{run['repro_command']}`")
    lines.append("")
    lines.append("## 与现有管线的关系")
    lines.append("")
    lines.append(
        "本 run 是**现有 le_calc / `le.ps1` 管线的技能模型扩展**（不是第二套管线）："
        "单一入口仍是 `le.ps1 run --case <case.json>`；`hit-snapshot` / `hit-run` 只是该入口内部的步骤。"
    )
    lines.append("")
    lines.append(
        "- 复用：`case.json`（`le-case-v1`）、BuildSpec revision、`calculation-input-v1`、"
        "`model.Snapshot` 的 `build_meta` 指纹链（build → dataset → calc_input）、"
        "source/rule ledger、`run-manifest.json`、离线单文件报告约定、条件场景结构、`le.ps1` 入口。"
    )
    lines.append(
        "- 新增：Flame Reave 单次命中伤害 / 攻速 / 命中次数 / DPS 窗口模型与条件 Buff 筛查，"
        "以及本报告渲染层；结果 schema 为 `le-hit-result-v1`，只覆盖该技能本体命中。"
    )
    confirmations = _as_dict(result.get("user_confirmations"))
    if confirmations.get("items"):
        lines.append("")
        lines.append("### 用户裁决（2026-11-07）")
        lines.append("")
        for item in confirmations.get("items") or []:
            lines.append(f"- {item}")
    lines.append("")
    lines.append("## 关键结果")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---:|")
    lines.append(f"| 单次命中伤害 | {_format_number(totals.get('damage'))} |")
    lines.append(f"| 命中次数 / 次施放 | {hits.get('per_cast')} |")
    lines.append(f"| 攻击速度（APS） | {_format_number(attack.get('final_aps'), 4)} |")
    for window in dps.get("windows") or []:
        lines.append(
            f"| {_format_number(window.get('window_s'), 0)}s 窗口 DPS | {_format_number(window.get('dps'))} |"
        )
    lines.append(f"| 平均暴击乘区 | {_multiplier_text(totals.get('avg_crit'))} |")
    lines.append(f"| 穿透乘区 | {_multiplier_text(totals.get('penetration_multiplier'))} |")
    lines.append(f"| 目标乘区 | {_multiplier_text(totals.get('target_multiplier'))} |")
    lines.append(f"| 施放速率（主策略） | {_format_number(dps.get('casts_per_second'), 4)} /s |")
    lines.append(f"| 实际命中速率 | {_format_number(dps.get('hits_per_second'), 4)} /s |")
    lines.append("")
    lines.append("> 单次命中与 DPS 都是限定来源、限定目标场景的**固定条件估算**；assumed/unresolved 未确认前只能作为假设级数值。")
    lines.append("")
    baselines = _as_dict(result.get("baselines"))
    baseline_profiles = _as_dict(baselines.get("profiles"))
    if baseline_profiles:
        lines.append("## 双基线（分别引用，不可相减当增益）")
        lines.append("")
        lines.append("| 基线口径 | 单次命中 | 5s DPS | 30s DPS | 120s DPS | 施放速率 /s |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for profile_id, block in baseline_profiles.items():
            data = _as_dict(block)
            windows = _as_dict(data.get("dps_windows"))
            lines.append(
                f"| {profile_id}{'（主口径）' if profile_id == baselines.get('profile') else ''} | "
                f"{_format_number(data.get('single_hit_damage'))} | "
                f"{_format_number(windows.get('5s'))} | {_format_number(windows.get('30s'))} | "
                f"{_format_number(windows.get('120s'))} | {_format_number(data.get('casts_per_second'), 4)} |"
            )
        lines.append("")
        lines.append(f"_{baselines.get('note')}_")
        lines.append("")
    lines.append("## 单次命中乘区链")
    lines.append("")
    lines.append(f"1. base = {_format_number(totals.get('base'), 4)}")
    lines.append(
        f"2. + added × scaling = {_format_number(totals.get('added'), 4)} × "
        f"{_format_number(_as_dict((single.get('components') or [{}])[0]).get('added_damage_scaling'), 4)} "
        f"= {_format_number(totals.get('added_scaled'), 4)}"
    )
    lines.append(f"3. increased 合计 = +{_format_number(totals.get('increased_total'), 4)}")
    lines.append(f"4. more 乘区 = {_multiplier_text(totals.get('more_product'))}")
    lines.append(
        f"5. avg crit = chance {_pct_or_number(totals.get('crit_chance'))} × multi "
        f"{_multiplier_text(totals.get('crit_multiplier'))} → {_multiplier_text(totals.get('avg_crit'))}"
    )
    lines.append(f"6. 穿透 = {_multiplier_text(totals.get('penetration_multiplier'))}；目标 = {_multiplier_text(totals.get('target_multiplier'))}")
    lines.append(f"7. **单次命中 = {_format_number(totals.get('damage'))}**")
    lines.append("")
    lines.append("## 伤害构成")
    lines.append("")
    lines.append("| 组件 | 类型 | base | added×scaling | inc | more | 伤害 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for component in single.get("components") or []:
        lines.append(
            f"| {component.get('label') or component.get('component_id')} | {component.get('damage_type')} | "
            f"{_format_number(component.get('base'), 4)} | {_format_number(component.get('added_scaled'), 4)} | "
            f"{_format_number(component.get('increased'), 4)} | ×{_format_number(component.get('more_product'), 4)} | "
            f"{_format_number(component.get('damage'))} |"
        )
    lines.append("")
    lines.append("## 命中与 DPS")
    lines.append("")
    if dps.get("windows"):
        lines.append("| 窗口 (s) | 命中次数 | 总伤害 | DPS |")
        lines.append("|---:|---:|---:|---:|")
        for window in dps["windows"]:
            lines.append(
                f"| {_format_number(window.get('window_s'), 0)} | {_format_number(window.get('hits'), 0)} | "
                f"{_format_number(window.get('total_damage'))} | {_format_number(window.get('dps'))} |"
            )
        lines.append("")
    policy = _as_dict(dps.get("policy"))
    lines.append(
        f"- 输出策略：`{policy.get('id')}` {policy.get('label', '')}；"
        f"每次施放 {hits.get('per_cast')} 次命中（status={hits.get('status')}）。"
    )
    lines.append(f"- 资源假设：{dps.get('resource_note') or '未模拟法力消耗/恢复（以规则包登记为准）'}")
    lines.append("")
    lines.append("## 条件 Buff 筛查")
    lines.append("")
    lines.append(
        "- 候选 {total} · 已建模 {modeled} · 未建模 {not_modeled} · unresolved {unresolved}".format(
            total=summary.get("total", 0),
            modeled=summary.get("modeled", 0),
            not_modeled=summary.get("not_modeled", 0),
            unresolved=summary.get("unresolved", 0),
        )
    )
    applied = buffs.get("applied") or []
    if applied:
        lines.append(
            "- 应用状态："
            + "，".join(
                f"{row.get('state')}={row.get('value')}" for row in applied
            )
        )
    lines.append("- 完整候选、证据与 next_probe 见 `buff-screening.json`；结果页只展示筛查摘要。")
    lines.append("")
    sensitivity = buffs.get("sensitivity") or []
    if sensitivity:
        lines.append("## 条件 Buff 敏感性（单状态切换，固定条件估算）")
        lines.append("")
        lines.append("| 状态切换 | 单次命中 | 相对主口径 | DPS 5s | 相对主口径 | more 乘区 | 暴击率 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for item in sensitivity:
            if not isinstance(item, dict):
                continue
            single_rel = item.get("single_rel")
            dps_rel = item.get("dps_rel")
            windows = _as_dict(item.get("dps_windows"))
            lines.append(
                f"| {item.get('label')} | {_format_number(item.get('single_hit_damage'))} | "
                f"{(f'{single_rel * 100:+.1f}%' if isinstance(single_rel, (int, float)) else '—')} | "
                f"{_format_number(windows.get('5s'))} | "
                f"{(f'{dps_rel * 100:+.1f}%' if isinstance(dps_rel, (int, float)) else '—')} | "
                f"{_multiplier_text(item.get('more_product'))} | {_pct_or_number(item.get('crit_chance'))} |"
            )
        lines.append("")
        lines.append("> 每个状态都是单独切换（不是叠加后的实战总量）；叠层/操作序列未做时间模拟。")
        lines.append("")
    open_questions = [item for item in result.get("open_questions") or [] if isinstance(item, dict)]
    if open_questions:
        lines.append("## 待确认问题（ruleset.open_questions）")
        lines.append("")
        for item in open_questions:
            lines.append(f"- `{item.get('id')}` {item.get('question')}")
        lines.append("")
    lines.append("## 文件")
    lines.append("")
    lines.append("- `result.json`：唯一业务数据源（HTML 由同一份数据生成）")
    lines.append("- `input-snapshot.json`：冻结的 hit 快照（build/dataset/calc_input 指纹 + contributions）")
    lines.append("- `source-ledger.json`：逐条属性来源账")
    lines.append("- `rule-ledger.json`：规则与假设账（confirmed/derived/assumed/unresolved）")
    lines.append("- `buff-screening.json`：条件 Buff 完整候选（含证据/next_probe）")
    lines.append(f"- `{HTML_FILENAME}`：离线单文件报告（双击打开，无网络请求）")
    lines.append("- `run-manifest.json`：输入指纹、文件 sha256、一致性校验结果")
    lines.append("")
    lines.append("## 假设 / 未解决")
    lines.append("")
    for status in BLOCKING_STATUSES:
        rules = [row for row in (result.get("rule_ledger") or []) if row.get("status") == status]
        effects = [
            candidate
            for candidate in screening.get("candidates") or []
            if candidate.get("status") == status
        ]
        blockers = len(rules) + len(effects)
        if blockers:
            lines.append(f"### {status}（{blockers} 条）")
            lines.append("")
            for rule in rules:
                lines.append(f"- `{rule.get('rule_id')}` {rule.get('title')}：{rule.get('statement')}")
            for candidate in effects:
                lines.append(
                    f"- `{candidate.get('buff_id')}` {candidate.get('label')}："
                    f"{candidate.get('condition') or '条件未建模'}"
                )
            lines.append("")
    warnings = [
        issue
        for issue in (result.get("issues") or [])
        if issue.get("severity") in ("warning", "error")
    ]
    if warnings:
        lines.append("### 需要知悉的 issues")
        lines.append("")
        for issue in warnings:
            lines.append(f"- [{issue.get('severity')}] {issue.get('title')}：{issue.get('detail')}")
        lines.append("")
    lines.append("## 重要限制")
    lines.append("")
    lines.append("- 本报告只覆盖 Flame Reave（fr11mv）本体命中，不是完整 BD 实战总 DPS。")
    lines.append("- `status=assumption` 表示规则包中仍有 assumed/unresolved 项，数值不能称为精确值。")
    lines.append("- 未模拟移动、走位、资源管理、目标行为与其他技能/召唤物/持续伤害。")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Offline single-file HTML
# ---------------------------------------------------------------------------


def _display_safe(value: Any) -> Any:
    """Strip URL schemes from a display copy (the HTML must contain no external refs)."""
    if isinstance(value, str):
        return value.replace("https://", "").replace("http://", "")
    if isinstance(value, dict):
        return {key: _display_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_display_safe(item) for item in value]
    return value


def _embed_json(payload: Any) -> str:
    """JSON safe for ``<script type="application/json">``; schemes stripped."""
    text = json.dumps(_display_safe(payload), ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _esc(value: Any) -> str:
    return _escape("" if value is None else str(value))


def _safe_text(value: Any) -> str:
    return _esc(_display_safe("" if value is None else str(value)))


def _pct_or_number(value: Any, digits: int = 2) -> str:
    number = _num(value, float("nan"))
    if number != number:
        return ""
    if 0.0 <= number <= 1.0:
        return f"{number * 100:.{digits}f}%"
    return _format_number(number, digits)


def _render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return _format_number(value, 4)
    if isinstance(value, str):
        return _safe_text(value)
    if value is None:
        return "—"
    return _safe_text(json.dumps(_display_safe(value), ensure_ascii=False))


def _evidence_text(evidence: Any) -> str:
    parts: list[str] = []
    for item in evidence or []:
        if isinstance(item, dict):
            label = _pick(item, "locator", "detail", "kind", default="")
            kind = _pick(item, "kind", default="")
            parts.append(f"[{kind}] {label}" if kind else str(label))
        else:
            parts.append(str(item))
    return "；".join(part for part in parts if part)


_CSS = """
:root{--bg:#0f1420;--panel:#171f2e;--line:#2a3547;--text:#e6ecf5;--muted:#93a2b8;
--accent:#e8734a;--fire:#ff9a5c;--ok:#3ecf8e;--warn:#f2c14e;--bad:#ef5b6b;--tab:#1e2939}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;font-size:14px;line-height:1.55}
header{padding:22px 28px 8px}
h1{margin:0 0 4px;font-size:22px}
h2{margin:26px 0 10px;font-size:17px;border-left:3px solid var(--accent);padding-left:9px}
.sub{color:var(--muted);font-size:12.5px}
.wrap{padding:0 28px 44px;max-width:1280px;margin:0 auto}
.banner{border-radius:8px;padding:11px 15px;margin:12px 0;border:1px solid var(--line)}
.banner-assumption{background:rgba(242,193,78,.12);border-color:var(--warn)}
.banner-ok{background:rgba(62,207,142,.10);border-color:var(--ok)}
.banner strong{letter-spacing:.4px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:16px 0 14px}
.tab{background:var(--tab);color:var(--text);border:1px solid var(--line);border-radius:7px;
padding:8px 14px;cursor:pointer;font-size:13.5px}
.tab.is-active{background:var(--accent);border-color:var(--accent);color:#141a24;font-weight:600}
.panel{display:none}
.panel.is-active{display:block}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:10px;margin:10px 0 4px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:11px 13px}
.card .k{color:var(--muted);font-size:12px}
.card .v{font-size:19px;font-weight:600;margin-top:3px;color:var(--fire);word-break:break-all}
table{border-collapse:collapse;width:100%;background:var(--panel);border:1px solid var(--line);
border-radius:8px;overflow:hidden;margin:8px 0 4px}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;font-size:12.5px}
th{background:#1c2535;color:#b9c6d8;font-weight:600;white-space:nowrap}
tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;margin:1px 3px 1px 0;
border:1px solid var(--line)}
.st-confirmed,.st-derived,.st-ok,.st-modeled{color:var(--ok);border-color:rgba(62,207,142,.45)}
.st-assumed,.st-warning{color:var(--warn);border-color:rgba(242,193,78,.45)}
.st-unresolved,.st-error,.st-not_modeled{color:var(--bad);border-color:rgba(239,91,107,.45)}
.muted{color:var(--muted)}
.kv{display:grid;grid-template-columns:170px 1fr;gap:4px 12px;margin:8px 0}
.kv .k{color:var(--muted)}
ul{margin:6px 0 6px 20px;padding:0}
li{margin:2px 0}
.bar{height:8px;background:#223047;border-radius:4px;overflow:hidden;min-width:60px}
.bar>span{display:block;height:100%;background:var(--fire)}
footer{color:var(--muted);font-size:11.5px;padding:22px 28px 40px;border-top:1px solid var(--line);margin-top:26px}

h3{margin:18px 0 8px;font-size:14.5px;color:#d9e6f7}
h4.grp-title{margin:14px 0 6px;font-weight:600;color:#cfe0f5}
.section-note{color:var(--muted);font-size:12.5px;margin:4px 0 10px}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin:8px 0 10px}
.chip{background:#202b3d;border:1px solid var(--line);border-radius:12px;padding:2px 9px;font-size:11.5px}
.chip-fire{color:var(--fire);border-color:rgba(255,154,92,.5)}
.steps{display:grid;gap:8px;margin:8px 0 12px}
.step{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:8px;padding:9px 12px;display:grid;grid-template-columns:28px 1fr;gap:10px}
.step-idx{color:var(--accent);font-weight:700}
.step-title{font-weight:600}
.step-formula{color:var(--muted);font-family:ui-monospace,Consolas,monospace;font-size:11.5px;margin-top:2px;word-break:break-all}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}
.warnbox{background:rgba(242,193,78,.10);border:1px solid rgba(242,193,78,.5);border-radius:8px;padding:9px 12px;margin:8px 0}
.timeline{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px;margin:8px 0}
.timeline svg{width:100%;height:130px;display:block}
.kv2{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:4px 18px;margin:8px 0}
.kv2 .row{display:flex;justify-content:space-between;gap:12px;border-bottom:1px dashed #24304a;padding:3px 0}
.kv2 .k{color:var(--muted)}
.weight-list{grid-template-columns:1fr}
.weight-list .row{display:grid;grid-template-columns:120px 1fr 70px;align-items:center}
.weight-bar{height:8px;background:#223047;border-radius:4px;overflow:hidden}
.weight-bar>span{display:block;height:100%;background:var(--fire)}
.fire{color:var(--fire)}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;word-break:break-all}
.num{font-variant-numeric:tabular-nums}
@media print{body{background:#fff;color:#111}.tab{display:none}.panel{display:block!important}}
"""

_JS = """
(function(){
  var tabs=Array.prototype.slice.call(document.querySelectorAll('[data-tab]'));
  var panels=Array.prototype.slice.call(document.querySelectorAll('[data-panel]'));
  function show(id){
    tabs.forEach(function(t){t.classList.toggle('is-active',t.getAttribute('data-tab')===id);
      t.setAttribute('aria-selected',t.getAttribute('data-tab')===id?'true':'false');});
    panels.forEach(function(p){p.classList.toggle('is-active',p.getAttribute('data-panel')===id);});
  }
  tabs.forEach(function(t){t.addEventListener('click',function(){show(t.getAttribute('data-tab'));});});
  if(tabs.length){show(tabs[0].getAttribute('data-tab'));}
})();
"""


_FR_CSS_OVERRIDES = r"""
/* ------------------------------------------------------------------ */
/* FR report additions on top of the BH report style.css              */
/* The FR panels were originally authored with a small dark theme;    */
/* these rules map their fragment classes onto the BH light theme.    */
/* ------------------------------------------------------------------ */
.app-main .page h2{margin:24px 0 12px;font-size:18px;font-weight:650;letter-spacing:0;border:0;padding:0;color:var(--ink)}
.page-head h2{margin:0 0 4px;font-size:22px;font-weight:650;letter-spacing:-.01em}
.page-head{margin-bottom:6px}
.app-main .page h3{margin:22px 0 10px;font-size:16px;font-weight:650;color:var(--ink)}
.app-main .page h4.grp-title{margin:16px 0 8px;font-size:14px;font-weight:650;color:var(--ink-soft)}
.page-question{margin:2px 0 0;color:var(--muted);font-size:14px}
.status-banner{display:flex;gap:14px;align-items:flex-start;background:var(--warn-soft);border:1px solid #f0d9a8;border-radius:10px;padding:12px 16px;margin:0 0 22px}
.status-banner-text p{margin:0;color:var(--ink-soft);font-size:13.5px}
.status-banner .badge{flex:none;margin-top:2px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(176px,1fr));gap:12px;margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:0}
.card .k{color:var(--muted);font-size:12.5px;margin:0 0 5px}
.card .v{font-size:22px;font-weight:650;color:var(--ink);margin:0;font-variant-numeric:tabular-nums;word-break:break-word}
table{width:100%;border-collapse:collapse;font-size:13.5px;background:var(--card);margin:0 0 16px}
thead th{background:#f8fafc;color:var(--muted);font-weight:650;font-size:12.5px}
th,td{padding:8px 12px;border-bottom:1px solid var(--line-soft);text-align:left;vertical-align:top}
tr:last-child td{border-bottom:0}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;margin:1px 3px 1px 0;border:1px solid transparent;background:var(--bg-soft);color:var(--ink-soft)}
.st-confirmed,.st-derived,.st-ok,.st-modeled{color:var(--ok);background:var(--ok-soft)}
.st-assumed,.st-warning{color:var(--warn);background:var(--warn-soft)}
.st-unresolved,.st-error,.st-not_modeled{color:var(--bad);background:var(--bad-soft)}
.muted{color:var(--muted)}
.section-note{color:var(--muted);font-size:12.5px;margin:2px 0 12px;line-height:1.6}
.chip{background:var(--bg-soft);border:1px solid transparent;color:var(--ink-soft);border-radius:999px;padding:2px 9px;font-size:12.5px}
.chip-fire{background:var(--fire-soft);color:var(--fire)}
.warnbox{background:var(--warn-soft);border:1px solid #f0d9a8;border-radius:10px;padding:12px 16px;margin:0 0 14px}
.warnbox p{margin:6px 0}
.steps{display:grid;gap:10px;margin:0 0 18px}
.step{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--cast);border-radius:10px;padding:12px 16px;display:grid;grid-template-columns:28px 1fr;gap:12px}
.step-idx{color:var(--cast);font-weight:700}
.step-title{font-weight:650}
.step-formula{color:var(--muted);font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;margin-top:3px;word-break:break-word}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:18px}
.kv2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:2px 22px;margin:0 0 16px}
.kv2 .row{display:flex;justify-content:space-between;gap:14px;border-bottom:1px solid var(--line-soft);padding:5px 0}
.kv2 .k{color:var(--muted)}
.weight-list{grid-template-columns:1fr}
.weight-list .row{display:grid;grid-template-columns:120px 1fr 80px;align-items:center}
.weight-bar{height:8px;background:var(--bg-soft);border-radius:999px;overflow:hidden}
.weight-bar>span{display:block;height:100%;background:var(--fire)}
.bar{height:8px;background:var(--bg-soft);border-radius:999px;overflow:hidden;min-width:60px}
.bar>span{display:block;height:100%;background:var(--cast)}
.timeline{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:0 0 16px}
.timeline svg{width:100%;height:150px;display:block}
.fire{color:var(--fire)}
.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;word-break:break-all}
.num{font-variant-numeric:tabular-nums}
ul{margin:6px 0 12px 20px;padding:0}
li{margin:2px 0}
@media (max-width:1080px){.kv2{grid-template-columns:1fr}}
@media print{.page-tabs{display:none}.page{display:block !important}.status-banner{break-inside:avoid}}
"""


def _report_css() -> str:
    """BH report stylesheet + FR light-theme remap (offline, no external refs)."""
    base = Path(__file__).with_name("report_ui") / "assets" / "style.css"
    try:
        bh_css = base.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - defensive fallback
        bh_css = ""
    return bh_css + _FR_CSS_OVERRIDES



def _html_table(headers: list[str], rows: list[list[str]], *, numeric: Iterable[int] = ()) -> str:
    numeric_set = set(numeric)
    head = "".join(
        f'<th class="{"num" if index in numeric_set else ""}">{_esc(title)}</th>'
        for index, title in enumerate(headers)
    )
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="{"num" if index in numeric_set else ""}">{cell}</td>'
            for index, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return (
        '<div class="table-wrap"><table class="data-table">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _status_tag(status: Any) -> str:
    text = _safe_text(status or "unknown")
    return f'<span class="tag st-{text}">{text}</span>'


def _cards(result: dict[str, Any]) -> str:
    single = _as_dict(result.get("single_hit"))
    totals = _as_dict(single.get("totals"))
    dps = _as_dict(result.get("dps"))
    attack = _as_dict(dps.get("attack_rate"))
    hits = _as_dict(result.get("hits_per_cast"))
    windows = {round(_num(window.get("window_s")), 6): window for window in dps.get("windows") or []}
    cards = [
        ("单次命中伤害", _format_number(totals.get("damage"))),
        ("命中次数 / 施放", _format_number(hits.get("per_cast"), 0)),
        ("攻击速度 APS", _format_number(attack.get("final_aps"), 4)),
        ("平均暴击乘区", _multiplier_text(totals.get("avg_crit"))),
        ("穿透乘区", _multiplier_text(totals.get("penetration_multiplier"))),
        ("目标乘区", _multiplier_text(totals.get("target_multiplier"))),
    ]
    for seconds in REQUIRED_WINDOWS:
        window = windows.get(round(seconds, 6))
        if window is not None:
            cards.append((f"{seconds:g}s 窗口 DPS", _format_number(window.get("dps"))))
    html = "".join(
        f'<div class="card"><div class="k">{_esc(key)}</div><div class="v">{_esc(value)}</div></div>'
        for key, value in cards
    )
    return f'<div class="cards">{html}</div>'


def _overview_panel(result: dict[str, Any]) -> str:
    run = _as_dict(result.get("run"))
    input_block = _as_dict(result.get("input"))
    build = _as_dict(input_block.get("build"))
    dataset = _as_dict(input_block.get("dataset"))
    calc = _as_dict(input_block.get("calc_input"))
    skill = _as_dict(result.get("skill"))
    scenario = _as_dict(result.get("scenario"))
    fingerprints = _as_dict(input_block.get("fingerprints"))
    rows = [
        ("运行 id", _safe_text(run.get("run_id"))),
        ("生成时间", _safe_text(run.get("generated_at"))),
        ("引擎", _safe_text(f"{_as_dict(run.get('engine')).get('id')} {_as_dict(run.get('engine')).get('version')}")),
        ("状态", f'{_status_tag(run.get("status"))}'),
        ("主技能", _safe_text(f"{skill.get('ability_id')} · {skill.get('name')}")),
        ("技能标签", _safe_text(", ".join(skill.get("tags") or []))),
        ("构筑", _safe_text(f"{build.get('build_id')} / {build.get('revision_id')}")),
        ("数据集", _safe_text(dataset.get("dataset_id"))),
        ("计算输入", _safe_text(calc.get("path"))),
        ("输出场景", _safe_text(f"{scenario.get('scenario_id')} · {scenario.get('name')}")),
        ("场景基线", _safe_text(scenario.get("baseline_profile") or "—")),
        ("输出策略", _safe_text(_as_dict(scenario.get("policy")).get("id") or scenario.get("policy") or "—")),
    ]
    kv = "".join(f'<div class="k">{_esc(key)}</div><div>{value}</div>' for key, value in rows)
    fp_rows = [[_esc(key), _safe_text(value)] for key, value in sorted(fingerprints.items())]
    fp_table = _html_table(["指纹", "值"], fp_rows) if fp_rows else '<p class="muted">快照未提供指纹。</p>'
    scope = _as_dict(result.get("scope"))
    included = "".join(f"<li>{_safe_text(item)}</li>" for item in scope.get("included") or [])
    notes = "".join(f"<li>{_safe_text(item)}</li>" for item in scope.get("notes") or [])
    assumptions = "".join(
        f"<li>{_safe_text(item)}</li>" for item in scenario.get("assumptions") or []
    )
    return f"""
<h2>关键结果</h2>
{_cards(result)}
<h2>运行与输入绑定</h2>
<div class="kv">{kv}</div>
<h3>输入指纹</h3>
{fp_table}
<h2>范围</h2>
<ul>{included}</ul>
<ul class="muted">{notes}</ul>
<h3>场景假设</h3>
<ul>{assumptions or '<li class="muted">无额外场景假设（baseline）。</li>'}</ul>
"""


def _composition_panel(result: dict[str, Any]) -> str:
    single = _as_dict(result.get("single_hit"))
    totals = _as_dict(single.get("totals"))
    rows: list[list[str]] = []
    for component in single.get("components") or []:
        rows.append(
            [
                _safe_text(component.get("label") or component.get("component_id")),
                _safe_text(component.get("damage_type")),
                _format_number(component.get("base"), 4),
                _format_number(component.get("added_scaled"), 4),
                _format_number(component.get("increased"), 4),
                f"×{_format_number(component.get('more_product'), 4)}",
                f"×{_format_number(component.get('avg_crit'), 4)}",
                f"×{_format_number(component.get('penetration'), 4)}",
                f"×{_format_number(component.get('target_multiplier'), 4)}",
                _format_number(component.get("damage")),
            ]
        )
    table = _html_table(
        ["组件", "类型", "基础", "点伤×倍率", "提高", "独立", "平均暴击", "穿透", "目标", "伤害"],
        rows,
        numeric=[2, 3, 4, 5, 6, 7, 8, 9],
    )
    totals_rows = [
        ["base", _format_number(totals.get("base"), 4)],
        ["added × added_damage_scaling", _format_number(totals.get("added_scaled"), 4)],
        ["increased 合计", _format_number(totals.get("increased_total"), 4)],
        ["more 乘积", _multiplier_text(totals.get("more_product"), 4, unknown="按组件分别计算（见明细）")],
        [
            "暴击",
            f"chance {_pct_or_number(totals.get('crit_chance'))} · "
            f"multi {_multiplier_text(totals.get('crit_multiplier'), 4, unknown='见明细')} · "
            f"avg {_multiplier_text(totals.get('avg_crit'))}",
        ],
        ["穿透乘区", _multiplier_text(totals.get("penetration_multiplier"))],
        ["目标乘区", _multiplier_text(totals.get("target_multiplier"))],
        ["单次命中伤害", _format_number(totals.get("damage"))],
    ]
    totals_table = _html_table(["合计项", "数值"], totals_rows)
    assumptions = "".join(
        f"<li>{_safe_text(item)}</li>" for item in single.get("assumptions") or []
    )
    return f"""
<h2>单次命中构成</h2>
{table}
<h3>乘区合计</h3>
{totals_table}
<h3>引擎假设</h3>
<ul>{assumptions or '<li class="muted">引擎未登记额外假设。</li>'}</ul>
"""


def _dps_panel(result: dict[str, Any]) -> str:
    dps = _as_dict(result.get("dps"))
    attack = _as_dict(dps.get("attack_rate"))
    hits = _as_dict(result.get("hits_per_cast"))
    policy = _as_dict(dps.get("policy"))
    windows = dps.get("windows") or []
    max_dps = max((_num(window.get("dps")) for window in windows), default=0.0) or 1.0
    rows = []
    for window in windows:
        dps_value = _num(window.get("dps"))
        width = max(2.0, 100.0 * dps_value / max_dps)
        rows.append(
            [
                _format_number(window.get("window_s"), 0),
                _format_number(window.get("hits"), 0),
                _format_number(window.get("total_damage")),
                f'<div class="bar"><span style="width:{width:.1f}%"></span></div>',
                _format_number(dps_value),
            ]
        )
    table = _html_table(
        ["窗口 (s)", "命中次数", "总伤害", "", "DPS"], rows, numeric=[0, 1, 2, 4]
    )
    attack_rows = [
        ["基础 APS（武器合成）", _format_number(attack.get("base_aps"), 4)],
        ["increased attack speed", _format_number(attack.get("increased_attack_speed"), 4)],
        ["more attack speed", f"×{_format_number(attack.get('more_attack_speed'), 4)}"],
        ["最终 APS", _format_number(attack.get("final_aps"), 4)],
        ["命中次数 / 施放", _format_number(hits.get("per_cast"), 0)],
        ["命中来源节点", _safe_text(", ".join(str(item) for item in hits.get("sources") or []))],
        ["命中次数状态", _status_tag(hits.get("status"))],
    ]
    if attack.get("hits_per_second"):
        attack_rows.append(["命中次数 / 秒", _format_number(attack.get("hits_per_second"), 3)])
    attack_table = _html_table(["攻速与命中", "数值"], attack_rows)
    return f"""
<h2>命中次数</h2>
{attack_table}
<h2>DPS 窗口</h2>
{table}
<h3>输出策略</h3>
<div class="kv">
  <div class="k">策略</div><div>{_safe_text(policy.get('id'))} · {_safe_text(policy.get('label'))}</div>
  <div class="k">资源假设</div><div>{_safe_text(dps.get('resource_note') or '未模拟法力消耗/恢复（以规则包登记为准）')}</div>
</div>
"""


def _buffs_panel(result: dict[str, Any]) -> str:
    buffs = _as_dict(result.get("buffs"))
    screening = _as_dict(buffs.get("screening"))
    summary = _as_dict(screening.get("summary"))
    cards = "".join(
        f'<div class="card"><div class="k">{_esc(key)}</div><div class="v">{_esc(_format_number(value, 0))}</div></div>'
        for key, value in (
            ("候选", summary.get("total")),
            ("已建模", summary.get("modeled")),
            ("进入基线", summary.get("included_in_baseline")),
            ("未建模", summary.get("not_modeled")),
            ("unresolved", summary.get("unresolved")),
        )
    )
    state_rows = [
        [
            _safe_text(row.get("state")),
            _render_value(row.get("value")),
            "是" if row.get("active") else "否",
        ]
        for row in buffs.get("applied") or []
    ]
    state_table = _html_table(["状态", "值", "生效"], state_rows) if state_rows else '<p class="muted">无状态开关。</p>'
    candidate_rows = []
    for candidate in screening.get("candidates") or []:
        candidate_rows.append(
            [
                _safe_text(candidate.get("buff_id")),
                _safe_text(candidate.get("label")),
                _safe_text(candidate.get("source_kind")),
                _safe_text(candidate.get("condition")),
                _status_tag(candidate.get("status")),
                "是" if candidate.get("modeled") else "否",
                _safe_text(_evidence_text(candidate.get("evidence"))),
                _safe_text(candidate.get("next_probe")),
            ]
        )
    candidate_table = (
        _html_table(
            ["候选", "名称", "来源", "条件", "状态", "已建模", "证据", "next_probe"],
            candidate_rows,
        )
        if candidate_rows
        else '<p class="muted">本次运行没有条件 Buff 候选。</p>'
    )
    return f"""
<h2>筛查摘要</h2>
<div class="cards">{cards}</div>
<h2>应用状态</h2>
{state_table}
<h2>候选明细</h2>
{candidate_table}
"""


def _sources_panel(result: dict[str, Any]) -> str:
    rows = list(result.get("source_ledger") or [])
    summary = summarize_source_ledger(rows)
    kind_cards = "".join(
        f'<div class="card"><div class="k">{_esc(kind)}</div><div class="v">{count}</div></div>'
        for kind, count in summary["by_source_kind"].items()
    )
    source_rows = [
        [
            _safe_text(row.get("row_id")),
            _safe_text(row.get("source_label")),
            _safe_text(row.get("stat_name")),
            _safe_text(row.get("operation")),
            _format_number(row.get("value"), 4),
            _safe_text(", ".join(row.get("damage_tags") or [])),
            _status_tag(row.get("status")),
            _safe_text(row.get("condition_status")),
            "是" if row.get("included") else "否",
        ]
        for row in rows
    ]
    source_table = _html_table(
        ["行", "来源", "属性", "运算", "数值", "tags", "状态", "条件", "纳入"],
        source_rows,
        numeric=[4],
    )
    rule_rows = [
        [
            _safe_text(row.get("rule_id")),
            _safe_text(row.get("title")),
            _status_tag(row.get("status")),
            _safe_text(row.get("statement")),
            _safe_text(_evidence_text(row.get("evidence"))),
        ]
        for row in result.get("rule_ledger") or []
    ]
    rule_table = _html_table(["规则", "标题", "状态", "陈述", "证据"], rule_rows)
    consistency = _as_dict(result.get("consistency"))
    check_rows = [
        [("PASS" if value is True else "FAIL"), _safe_text(key)]
        for key, value in consistency.items()
        if key != "all_pass"
    ]
    check_table = _html_table(["结果", "检查项"], check_rows)
    issue_rows = [
        [
            _status_tag(issue.get("severity")),
            _safe_text(issue.get("scope")),
            _safe_text(issue.get("title")),
            _safe_text(issue.get("detail")),
        ]
        for issue in result.get("issues") or []
    ]
    issue_table = (
        _html_table(["级别", "范围", "标题", "详情"], issue_rows)
        if issue_rows
        else '<p class="muted">无 issues。</p>'
    )
    return f"""
<h2>来源账摘要</h2>
<div class="cards">{kind_cards}</div>
<p class="muted">共 {summary['total_rows']} 行，其中 included {summary['included_rows']} 行、active {summary['active_rows']} 行。</p>
<h2>规则账</h2>
{rule_table}
<h2>一致性校验</h2>
{check_table}
<h2>Issues</h2>
{issue_table}
<h2>来源明细</h2>
{source_table}
"""


# ---------------------------------------------------------------------------
# Rich report panels (v2) — modeled after the Black Hole report depth
# ---------------------------------------------------------------------------
# Everything below renders static HTML from ``result.json`` only: the offline
# report never recomputes damage rules and never fetches external resources.


_SLOT_LABELS = {
    "weapon1": "主手", "weapon2": "副手", "head": "头盔", "chest": "胸甲",
    "hands": "手套", "waist": "腰带", "feet": "靴子", "ring1": "戒指 1",
    "ring2": "戒指 2", "amulet": "项链", "relic": "遗物",
}
_ATTR_LABELS = {"strength": "力量", "vitality": "活力", "intelligence": "智力", "dexterity": "敏捷", "attunement": "协调"}
_TAG_LABELS = {"Fire": "火焰", "Cold": "冰霜", "Lightning": "闪电", "Physical": "物理", "Void": "虚空", "Necrotic": "腐化", "Poison": "毒素", "Melee": "近战", "Spell": "法术", "Elemental": "元素", "Hit": "命中", "DoT": "持续伤害"}
_SOURCE_KIND_LABELS = {"equipment": "装备", "idol": "神像", "blessing": "祝福", "passive_node": "被动", "skill_node": "技能节点", "skill": "技能", "unique": "_unique", "derived": "派生", "base_character": "角色基础"}


def _strip_node_suffix(label: str) -> str:
    import re as _re
    return _re.sub(r"\s*\[[^\]]*\]\s*$", "", label or "").strip()


def _source_location_label(row: dict[str, Any]) -> str:
    kind = _text(row.get("source_kind"))
    location = _text(row.get("source_location") or row.get("source_path"))
    if kind == "base_character":
        return "角色基础"
    if kind == "passive_node":
        return "Mage 被动"
    if kind == "skill_node":
        return "Flame Reave 技能节点"
    if kind == "skill":
        return "技能"
    if kind == "blessing":
        return "祝福"
    if kind == "idol":
        import re as _re
        match = _re.search(r"idols\.(\d+)", location)
        return f"神像 {int(match.group(1)) + 1}" if match else "神像"
    if kind == "equipment":
        slot = location.rsplit(".", 1)[-1]
        return _SLOT_LABELS.get(slot, "装备")
    return _SOURCE_KIND_LABELS.get(kind, "来源")


def _source_instance(location: str) -> str:
    parts = [part for part in str(location or "").split(".") if part]
    if not parts:
        return ""
    # ledger locations use the dataset path prefix (``data.blessings.7.ir``);
    # grouping must key on the instance root, not the raw full location.
    while parts and parts[0] in ("data", "dataset", "snapshot"):
        parts = parts[1:]
    if not parts:
        return ""
    if parts[0] in ("equipment", "idols", "blessings") and len(parts) >= 2:
        return ".".join(parts[:2])
    if parts[:2] == ["passive_tree", "nodes"] and len(parts) >= 3:
        return ".".join(parts[:3])
    if len(parts) >= 2 and parts[0] == "specializations":
        for index, part in enumerate(parts):
            if part == "nodes" and index + 1 < len(parts):
                return ".".join(parts[: index + 2])
    return ""
def _ledger_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in (result.get("source_ledger") or []) if isinstance(row, dict)]


def _ledger_by_id(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row.get("contribution_id")): row for row in _ledger_rows(result)}


def _row_status(row: dict[str, Any]) -> str:
    if not row.get("included"):
        return "未采用"
    if row.get("active"):
        return "生效"
    condition = _text(row.get("condition_status"))
    return "条件未满足" if condition and condition not in ("always", "satisfied") else "未启用"


_data_source_row = None


def _display_source(result: dict[str, Any], contribution_id: str) -> str:
    row = _ledger_by_id(result).get(str(contribution_id))
    if not row:
        return "来源"
    return _strip_node_suffix(_text(row.get("source_label"))) or _source_location_label(row)


def _display_effect(result: dict[str, Any], contribution_id: str) -> str:
    row = _ledger_by_id(result).get(str(contribution_id))
    return _player_effect(row) if row else ""


def _report_cards(result: dict[str, Any]) -> str:
    single = _as_dict(result.get("single_hit"))
    totals = _as_dict(single.get("totals"))
    dps = _as_dict(result.get("dps"))
    attack = _as_dict(dps.get("attack_rate"))
    hits = _as_dict(result.get("hits_per_cast"))
    policy = _as_dict(dps.get("policy"))
    windows = {round(_num(window.get("window_s")), 6): window for window in dps.get("windows") or []}
    cards = [
        ("单次命中伤害", _format_number(totals.get("damage"))),
        ("命中次数 / 施放", _ui_units.fmt_compact(hits.get("per_cast"), "count")),
        ("首击当量 / 施放", _format_number(hits.get("effective_hits_per_cast"))),
        ("施放速率 / 秒", _format_number(dps.get("casts_per_second"), 4)),
        ("名义攻速 APS", _format_number(attack.get("final_aps"), 4)),
        ("暴击率", _pct_or_number(totals.get("crit_chance"))),
        ("暴击倍率", _multiplier_text(totals.get("crit_multiplier"))),
        ("平均暴击乘区", _multiplier_text(totals.get("avg_crit"))),
        ("穿透乘区", _multiplier_text(totals.get("penetration_multiplier"))),
        ("目标乘区", _multiplier_text(totals.get("target_multiplier"))),
    ]
    for seconds in REQUIRED_WINDOWS:
        window = windows.get(round(seconds, 6))
        if window is not None:
            cards.append((f"{seconds:g}s 窗口 DPS", _format_number(window.get("dps"))))
    if policy.get("binding_constraint"):
        cards.append(("DPS 主约束", _safe_text(_player_policy_label(policy.get("binding_constraint")))))
    html = "".join(
        f'<div class="card"><div class="k">{_esc(key)}</div><div class="v">{_esc(value)}</div></div>'
        for key, value in cards
    )
    return f'<div class="cards">{html}</div>'


def _chip_list(tags: Iterable[Any]) -> str:
    chips = "".join(f'<span class="chip">{_safe_text(tag)}</span>' for tag in tags if tag)
    return f'<div class="chips">{chips}</div>' if chips else '<span class="muted">—</span>'


def _mini_kv(rows: Iterable[tuple[Any, Any]]) -> str:
    body = "".join(
        f'<div class="row"><span class="k">{_esc(key)}</span><span>{value}</span></div>'
        for key, value in rows
    )
    return f'<div class="kv2">{body}</div>'


def _target_block(result: dict[str, Any]) -> str:
    single = _as_dict(result.get("single_hit"))
    totals = _as_dict(single.get("totals"))
    target = _as_dict(totals.get("target"))
    scenario = _as_dict(result.get("scenario"))
    scenario_target = _as_dict(scenario.get("target"))
    rows = [
        ("目标类型", "Boss / 稀有" if target.get("boss_or_rare") else "普通"),
        ("怪物等级", _format_number(target.get("monster_level"), 0)),
        ("怪物减伤 DR", _pct_or_number(target.get("monster_damage_reduction"))),
        ("DR 乘区", _multiplier_text(target.get("monster_dr_factor"))),
        ("目标火抗", _pct_or_number(target.get("fire_resistance"))),
        ("抗性乘区", _multiplier_text(target.get("resistance_factor"))),
        ("穿透乘区", _multiplier_text(totals.get("penetration_multiplier"))),
        ("场景 target 覆盖", _safe_text(json.dumps(scenario_target, ensure_ascii=False)) if scenario_target else "—"),
    ]
    return _mini_kv(rows)


def _issues_summary_block(result: dict[str, Any]) -> str:
    issues = list(result.get("issues") or [])
    counts: dict[str, int] = {}
    for issue in issues:
        severity = _text(issue.get("severity"), "info")
        counts[severity] = counts.get(severity, 0) + 1
    cards = "".join(
        f'<div class="card"><div class="k">{_esc(severity)}</div><div class="v">{_esc(str(count))}</div></div>'
        for severity, count in sorted(counts.items())
    )
    rows = [
        [
            _status_tag(issue.get("severity")),
            _safe_text(issue.get("scope")),
            _safe_text(issue.get("title")),
            _safe_text(issue.get("detail")),
        ]
        for issue in issues
    ]
    table = _html_table(["级别", "范围", "标题", "详情"], rows) if rows else '<p class="muted">无 issues。</p>'
    return f'<div class="cards">{cards or "<div class=\"card\"><div class=\"k\">issues</div><div class=\"v\">0</div></div>"}</div>{table}'


def _chain_block(result: dict[str, Any]) -> str:
    single = _as_dict(result.get("single_hit"))
    totals = _as_dict(single.get("totals"))
    components = single.get("components") or []
    total_damage = _num(totals.get("damage"))
    steps: list[tuple[str, str, str]] = [
        ("1", "基础伤害 base", _format_number(totals.get("base"), 4)),
        (
            "2",
            "附加伤害 added × added_damage_scaling",
            f"{_format_number(totals.get('added'), 4)} × scaling = {_format_number(totals.get('added_scaled'), 4)}",
        ),
        (
            "3",
            "小计 subtotal",
            _format_number(_num(totals.get("base")) + _num(totals.get("added_scaled")), 4),
        ),
        (
            "4",
            "increased 加算池",
            f"+{_format_number(totals.get('increased_total'), 4)} → ×{_format_number(1.0 + _num(totals.get('increased_total')), 4)}",
        ),
        (
            "5",
            "more 独立乘区（按组相乘）",
            _multiplier_text(totals.get("more_product")),
        ),
        (
            "6",
            "暴击期望 avg crit",
            f"chance {_pct_or_number(totals.get('crit_chance'))} × multi {_multiplier_text(totals.get('crit_multiplier'))} → {_multiplier_text(totals.get('avg_crit'))}",
        ),
        ("7", "穿透 ×(1+Σpen)", _multiplier_text(totals.get("penetration_multiplier"))),
        ("8", "目标抗性 / 怪物 DR", _multiplier_text(totals.get("target_multiplier"))),
        ("9", "单次命中伤害", f'<strong class="fire">{_format_number(total_damage)}</strong>'),
    ]
    body = "".join(
        f'<div class="step"><div class="step-idx">{index}</div><div>'
        f'<div class="step-title">{_safe_text(title)}</div><div class="step-formula">{value}</div></div></div>'
        for index, title, value in steps
    )
    per_component = ""
    if len(components) > 1:
        rows = [
            [
                _safe_text(component.get("label") or component.get("component_id")),
                _safe_text(component.get("damage_type")),
                _format_number(component.get("base"), 4),
                _format_number(component.get("added_scaled"), 4),
                _format_number(component.get("increased"), 4),
                _multiplier_text(component.get("more_product")),
                _multiplier_text(component.get("avg_crit")),
                _format_number(component.get("damage")),
            ]
            for component in components
        ]
        per_component = f"""
<h3>分组件乘区</h3>
{_html_table(["组件", "类型", "base", "added×scaling", "inc", "more", "avg crit", "伤害"], rows, numeric=[2, 3, 4, 5, 6, 7])}
"""
    return f'<div class="steps">{body}</div>{per_component}'


def _component_detail_blocks(result: dict[str, Any]) -> str:
    single = _as_dict(result.get("single_hit"))
    blocks: list[str] = []
    for component in single.get("components") or []:
        label = _safe_text(component.get("label") or component.get("component_id"))
        damage_type = _safe_text(component.get("damage_type"))
        tags = _chip_list(component.get("damage_tags") or [])
        base_sources = component.get("base_sources") or []
        base_rows = [
            [_safe_text(item.get("source")), _safe_text(item.get("damage_type")), _format_number(item.get("value"), 4)]
            for item in base_sources
            if isinstance(item, dict)
        ]
        base_table = (
            _html_table(["来源", "类型", "数值"], base_rows, numeric=[2])
            if base_rows
            else '<p class="muted">无 base 来源记录。</p>'
        )
        added_rows = [
            [
                _safe_text(_display_source(result, str(item.get("contribution_id")))),
                _safe_text(_display_effect(result, str(item.get("contribution_id")))),
                _status_tag(_row_status(_ledger_by_id(result).get(str(item.get("contribution_id"))) or {})),
            ]
            for item in component.get("added_rows") or []
            if isinstance(item, dict)
        ]
        added_table = (
            _html_table(["来源", "效果", "状态"], added_rows)
            if added_rows
            else '<p class="muted">无 added 明细。</p>'
        )
        excluded_added = [
            [
                _safe_text(_display_source(result, str(item.get("contribution_id")))),
                _safe_text(_display_effect(result, str(item.get("contribution_id")))),
                "不适用于本组件",
            ]
            for item in component.get("excluded_added_rows") or []
            if isinstance(item, dict)
        ]
        excluded_table = (
            f'<h3>不适用于本组件的附加伤害</h3>'
            f'{_html_table(["来源", "效果", "原因"], excluded_added)}'
            if excluded_added
            else ""
        )
        inc_rows = sorted(
            [
                item
                for item in component.get("increased_rows") or []
                if isinstance(item, dict)
            ],
            key=lambda item: -abs(_num(item.get("value"))),
        )
        inc_table = _html_table(
            ["来源", "效果", "状态"],
            [
                [
                    _safe_text(_display_source(result, str(item.get("contribution_id")))),
                    _safe_text(_display_effect(result, str(item.get("contribution_id")))),
                    _status_tag(_row_status(_ledger_by_id(result).get(str(item.get("contribution_id"))) or {})),
                ]
                for item in inc_rows
            ],
        ) if inc_rows else '<p class="muted">无 increased 明细。</p>'
        more_html = ""
        for group in component.get("more_groups") or []:
            if not isinstance(group, dict):
                continue
            rows = [
                [
                    _safe_text(_display_source(result, str(row.get("contribution_id")))),
                    _safe_text(_display_effect(result, str(row.get("contribution_id")))),
                    _status_tag(_row_status(_ledger_by_id(result).get(str(row.get("contribution_id"))) or {})),
                ]
                for row in group.get("rows") or []
                if isinstance(row, dict)
            ]
            more_html += (
                f'<h4 class="grp-title">独立乘区 · ×{_format_number(group.get("multiplier"), 4)}</h4>'
                f'{_html_table(["来源", "效果", "状态"], rows)}'
            )
        if not more_html:
            more_html = '<p class="muted">无 more 组。</p>'
        crit = _as_dict(component.get("crit_detail"))
        crit_rows = [
            ["基础暴击率", _pct_or_number(crit.get("base_chance"))],
            ["added 暴击率（flat）", _pct_or_number(crit.get("added_chance"))],
            ["increased 暴击率", _pct_or_number(crit.get("increased_chance"))],
            ["more 暴击率乘区", _multiplier_text(crit.get("more_chance_product"))],
            ["最终暴击率（cap 后）", _pct_or_number(crit.get("crit_chance"))],
            ["基础暴击倍率", _multiplier_text(crit.get("base_multiplier"))],
            ["added 暴击倍率", _multiplier_text(crit.get("added_multiplier"))],
            ["increased 暴击倍率", _pct_or_number(crit.get("increased_multiplier"))],
            ["more 暴击倍率乘区", _multiplier_text(crit.get("more_multiplier_product"))],
            ["最终暴击倍率", _multiplier_text(crit.get("crit_multiplier"))],
            ["期望乘区 avg crit", _multiplier_text(crit.get("avg_crit"))],
        ]
        crit_rows_table = _html_table(["暴击项", "数值"], crit_rows)
        crit_source_rows = [
            [
                _safe_text(_display_source(result, str(item.get("contribution_id")))),
                _safe_text(_display_effect(result, str(item.get("contribution_id")))),
                _status_tag(_row_status(_ledger_by_id(result).get(str(item.get("contribution_id"))) or {})),
            ]
            for key in ("chance_rows", "multiplier_rows")
            for item in crit.get(key) or []
            if isinstance(item, dict)
        ]
        crit_sources_table = (
            f'<h4 class="grp-title">暴击来源明细</h4>'
            f'{_html_table(["来源", "效果", "状态"], crit_source_rows)}'
            if crit_source_rows
            else ""
        )
        pen_rows = [
            [
                _safe_text(_display_source(result, str(item.get("contribution_id")))),
                _safe_text(_display_effect(result, str(item.get("contribution_id")))),
            ]
            for item in component.get("penetration_rows") or []
            if isinstance(item, dict)
        ]
        pen_table = (
            _html_table(["来源", "效果"], pen_rows)
            if pen_rows
            else '<p class="muted">无穿透词条（乘区 ×1）。</p>'
        )
        blocks.append(
            f"""
<h2>组件 · {label}（{damage_type}）</h2>
{tags}
{_mini_kv([
    ('base', _format_number(component.get('base'), 4)),
    ('added（未乘 scaling）', _format_number(component.get('added'), 4)),
    ('added × scaling', _format_number(component.get('added_damage_scaling'), 4) + ' → ' + _format_number(component.get('added_scaled'), 4)),
    ('subtotal', _format_number(component.get('subtotal'), 4)),
    ('increased 合计', _format_number(component.get('increased'), 4)),
    ('more 乘积', _multiplier_text(component.get('more_product'))),
    ('avg crit', _multiplier_text(component.get('avg_crit'))),
    ('穿透', _multiplier_text(component.get('penetration'))),
    ('目标乘区', _multiplier_text(component.get('target_multiplier'))),
    ('组件伤害', _format_number(component.get('damage'))),
])}
<h3>公式</h3>
<div class="mono">{_safe_text(component.get('formula') or '')}</div>
<h3>base 来源</h3>
{base_table}
<h3>added 明细（进入本组件）</h3>
{added_table}
{excluded_table}
<h3>increased 明细</h3>
{inc_table}
<h3>more 组</h3>
{more_html}
<h3>暴击</h3>
{crit_rows_table}
{crit_sources_table}
<h3>穿透</h3>
{pen_table}
"""
        )
    return "".join(blocks)


def _skill_level_explainer(result: dict[str, Any]) -> str:
    """Plain-language explanation: skill level 27 only gates node points."""
    snapshot = _as_dict(result.get("snapshot"))
    skill = _as_dict(result.get("skill"))
    tree = _as_dict((snapshot.get("derived") or {}).get("structure_counts"))
    rows = [
        row
        for row in (result.get("source_ledger") or [])
        if isinstance(row, dict) and "Level of Skills" in _text(row.get("stat_name"))
    ]
    level_rows = [
        [
            _safe_text(row.get("source_label")),
            _safe_text(row.get("stat_name")),
            _format_number(row.get("value"), 0),
            _status_tag(row.get("status")),
            "是" if row.get("included") else "否",
        ]
        for row in rows
    ]
    table = (
        _html_table(["来源", "词条", "数值", "状态", "是否计入伤害"], level_rows, numeric=[2])
        if level_rows
        else '<p class="muted">来源账中没有 Level of Skills 行。</p>'
    )
    return f"""
<h2>技能等级 27：只影响节点数量，不影响伤害</h2>
<div class="warnbox">
<p><strong>结论</strong>：离线存档把技能等级显示成 20 级是存档读取/显示 bug；实际 FR 是 <strong>27 级</strong>。
等级只决定可分配的技能节点数量，<strong>不会缩放技能伤害</strong>。本 BD 的 FR 技能树已按
<strong>27 点</strong>建树（snapshot: <code>fr11mv_points_total = {_esc(tree.get('fr11mv_points_total') or '27')}</code>），
所以单次命中/DPS 不需要任何“等级缩放”修正。</p>
<p><strong>装备上的 +Level of Skills</strong> 只用于确定这 27 点（它们不再额外换算成伤害）；
报告把它们保留在来源账里是为了可审计，不是漏算。</p>
</div>
{table}
"""


def _weights_note(weights: list[float]) -> str:
    if not weights:
        return "—"
    first = float(weights[0])
    rest = [float(value) for value in weights[1:]]
    if rest and all(abs(value - rest[0]) < 1e-9 for value in rest):
        fraction = rest[0]
        if abs(first - 1.0) < 1e-9 and abs(fraction - 1.0) > 1e-9:
            return f"第 1 击按完整权重，后续命中按 {fraction * 100:g}% 权重计入"
    return "自定义权重：" + " / ".join(f"{value:g}" for value in weights)


def _hit_composition_meter(result: dict[str, Any]) -> str:
    """Per-cast hit composition bar (weights come from the applied hit policy)."""
    totals = _as_dict(_as_dict(result.get("single_hit")).get("totals"))
    hits = _as_dict(result.get("hits_per_cast"))
    dps = _as_dict(result.get("dps"))
    policy = _as_dict(dps.get("policy"))
    weights = [float(value) for value in (hits.get("damage_weights") or [])]
    single = _num(totals.get("damage"))
    rotation = _num(policy.get("rotation_more_factor"), 1.0)
    per_cast = _num(policy.get("damage_per_cast")) or single * sum(weights) * rotation
    if not weights or per_cast <= 0:
        return ""
    _weight_text = " / ".join(f"{weight:g}" for weight in weights)
    colors = ("fire", "body", "cold", "state")
    segments: list[str] = []
    legend: list[str] = []
    for index, weight in enumerate(weights):
        value = single * weight * rotation
        share = value / per_cast
        color = colors[index % len(colors)]
        segments.append(
            f'<span class="meter-seg meter-{color}" style="width:{share * 100:.2f}%" '
            f'title="第 {index + 1} 击 ×{weight:g}"></span>'
        )
        legend.append(
            f'<span class="meter-key"><i class="meter-dot meter-{color}"></i>'
            f"第 {index + 1} 击（权重 ×{weight:g}） "
            f"<b>{_format_number(value)}</b> "
            f'<span class="meter-share">{share * 100:.1f}%</span></span>'
        )
    return f"""
<h2>每次施放的伤害组成</h2>
<p class="section-note">每次施放 = 单次命中 × 伤害权重求和；本次为 {len(weights)} 击（权重 {_weight_text}），
再乘 Rhythm 平均系数 {_multiplier_text(rotation)}。合计 {_format_number(per_cast)}。</p>
<div class="meter">{"".join(segments)}</div>
<div class="meter-legend">{"".join(legend)}</div>
"""


def _report_overview(result: dict[str, Any]) -> str:
    run = _as_dict(result.get("run"))
    skill = _as_dict(result.get("skill"))
    scenario = _as_dict(result.get("scenario"))
    target = _as_dict(_as_dict(_as_dict(result.get("single_hit")).get("totals")).get("target"))
    scope = _as_dict(result.get("scope"))
    return f"""
<h2>关键结果</h2>
{_report_cards(result)}
<p class="section-note">预计伤害：这是限定来源、限定目标场景的模型值，不是游戏内实测 DPS。</p>
{_hit_composition_meter(result)}
<h2>两种基线口径</h2>
{_baseline_block(result)}
<h2>单次命中乘区链</h2>
{_chain_block(result)}
<h2>当前范围</h2>
{_mini_kv([
    ('技能', _safe_text(skill.get('name') or 'Flame Reave')),
    ('职业 / 专精', f"{skill.get('class') or '—'} / {skill.get('mastery') or '—'}"),
    ('目标', f"Lv.{_format_number(target.get('monster_level'), 0)} · DR {_pct_or_number(target.get('monster_damage_reduction'))} · 火抗 {_pct_or_number(target.get('fire_resistance'))}"),
    ('模型范围', '本体命中（不含其它技能/召唤物；Ignite DoT 暂不计入）'),
])}
"""


def _report_composition(result: dict[str, Any]) -> str:
    single = _as_dict(result.get("single_hit"))
    totals = _as_dict(single.get("totals"))
    components = single.get("components") or []
    if not components:
        return '<p class="muted">本次运行没有命中组件。</p>'
    rows = [
        [
            _safe_text(component.get("label") or component.get("component_id")),
            _safe_text(component.get("damage_type")),
            _format_number(component.get("base"), 4),
            _format_number(component.get("added_scaled"), 4),
            _format_number(component.get("increased"), 4),
            _multiplier_text(component.get("more_product")),
            _multiplier_text(component.get("avg_crit")),
            _multiplier_text(component.get("penetration")),
            _multiplier_text(component.get("target_multiplier")),
            _format_number(component.get("damage")),
        ]
        for component in components
    ]
    table = _html_table(
        ["组件", "类型", "base", "added×scaling", "inc", "more", "avg crit", "穿透", "目标", "伤害"],
        rows,
        numeric=[2, 3, 4, 5, 6, 7, 8, 9],
    )
    totals_rows = [
        ["基础伤害", _format_number(totals.get("base"), 2)],
        ["点伤 × 倍率", _format_number(totals.get("added_scaled"), 2)],
        ["提高合计", _format_number(totals.get("increased_total"), 2)],
        ["独立乘区乘积", _multiplier_text(totals.get("more_product"))],
        [
            "暴击",
            f"暴击率 {_pct_or_number(totals.get('crit_chance'))} · "
            f"暴击倍率 {_multiplier_text(totals.get('crit_multiplier'))} · "
            f"平均乘区 {_multiplier_text(totals.get('avg_crit'))}",
        ],
        ["穿透乘区", _multiplier_text(totals.get("penetration_multiplier"))],
        ["目标乘区", _multiplier_text(totals.get("target_multiplier"))],
        ["单次命中伤害", _format_number(totals.get("damage"))],
    ]
    totals_table = _html_table(["合计项", "数值"], totals_rows)
    excluded_sections = ""
    excluded_components = single.get("excluded_components") or []
    if excluded_components:
        excluded_sections = (
            '<h3>未进入命中的组件</h3>'
            + _html_table(
                ["组件", "类型", "原因"],
                [
                    [
                        _safe_text(item.get("label") or item.get("component_id")),
                        _safe_text(item.get("damage_type")),
                        _safe_text(item.get("reason")),
                    ]
                    for item in excluded_components
                    if isinstance(item, dict)
                ],
            )
        )
    return f"""
<h2>单次命中构成</h2>
{table}
<h3>乘区合计</h3>
{totals_table}
<h2>逐组件明细</h2>
{_component_detail_blocks(result)}
<h2>未计入的组件</h2>
{excluded_sections or '<p class="muted">无排除组件。</p>'}
"""


def _weights_html(weights: list[float]) -> str:
    if not weights:
        return '<p class="muted">未提供命中权重。</p>'
    max_weight = max(weights) or 1.0
    bars = "".join(
        f'<div class="row"><span class="k">第 {index + 1} 击</span>'
        f'<span class="weight-bar"><span style="width:{max(4.0, 100.0 * weight / max_weight):.1f}%"></span></span>'
        f'<span class="num">×{_format_number(weight, 2)}</span></div>'
        for index, weight in enumerate(weights)
    )
    total = sum(weights)
    return f'<div class="kv2 weight-list">{bars}<div class="row"><span class="k">合计首击当量</span><span>×{_format_number(total, 4)}</span></div></div>'


def _timeline_svg(result: dict[str, Any]) -> str:
    """Static 5s cast/hit timeline (display only; intra-cast offsets are assumed)."""
    dps = _as_dict(result.get("dps"))
    policy = _as_dict(dps.get("policy"))
    casts_per_second = _num(dps.get("casts_per_second")) or _num(policy.get("casts_per_second"))
    weights = [float(value) for value in (dps.get("damage_weights") or [])]
    if casts_per_second <= 0 or not weights:
        return '<p class="muted">无时间轴数据（策略或权重缺失）。</p>'
    span = 5.0
    cast_count = 0
    while cast_count < 12 and cast_count / casts_per_second <= span:
        cast_count += 1
    click_offsets = [0.0, 0.12, 0.24, 0.36][: len(weights)]
    left, width = 44.0, 930.0
    axis_y = 96.0
    parts: list[str] = [
        f'<line x1="{left:.1f}" y1="{axis_y:.1f}" x2="{left + width:.1f}" y2="{axis_y:.1f}" stroke="#cbd5e1" stroke-width="2"/>'
    ]
    for tick in range(6):
        x = left + tick / span * width
        parts.append(
            f'<line x1="{x:.1f}" y1="{axis_y - 6:.1f}" x2="{x:.1f}" y2="{axis_y + 8:.1f}" stroke="#cbd5e1"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{axis_y + 24:.1f}" fill="#5a6675" font-size="11" text-anchor="middle">{tick}s</text>'
        )
    for index in range(cast_count):
        cast_time = index / casts_per_second
        x = left + cast_time / span * width
        parts.append(
            f'<rect x="{x - 4:.1f}" y="{axis_y - 26:.1f}" width="8" height="20" rx="2" fill="#c2410c"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{axis_y - 32:.1f}" fill="#c2410c" font-size="10" text-anchor="middle">#{index + 1}</text>'
        )
        for weight, offset in zip(weights, click_offsets):
            hit_time = cast_time + offset
            if hit_time > span:
                continue
            hx = left + hit_time / span * width
            radius = 2.5 + 5.5 * min(weight, 1.0)
            parts.append(
                f'<circle cx="{hx:.1f}" cy="{axis_y - 8:.1f}" r="{radius:.1f}" fill="#ea580c" opacity="0.9"/>'
            )
    return (
        '<div class="timeline">'
        '<svg viewBox="0 0 1000 130" role="img" aria-label="5 秒施放与命中时间轴">'
        + "".join(parts)
        + "</svg>"
        '<p class="section-note">橙色方块 = 施放时刻；圆点 = 命中间（大小按伤害权重）。'
        "圆圈内部的 hit 间偏移为显示假设（0/0.15/0.30s），不是游戏实测动作时间；冷却限制下施放间隔见下。</p>"
        "</div>"
    )


def _player_resource_note(note: Any) -> str:
    """Player-facing resource assumption text (raw engine notes stay in JSON)."""

    text = _text(note)
    if not text:
        return "未模拟法力消耗/恢复（以规则包登记为准）"
    if re.search(r"[A-Za-z]{3,}", text):
        return "未模拟法力消耗/恢复（按规则包假设持续输出）"
    return text


def _report_dps(result: dict[str, Any]) -> str:
    dps = _as_dict(result.get("dps"))
    attack = _as_dict(dps.get("attack_rate"))
    hits = _as_dict(result.get("hits_per_cast"))
    policy = _as_dict(dps.get("policy"))
    alternate = _as_dict(dps.get("alternate_policy"))
    windows = dps.get("windows") or []
    max_dps = max((_num(window.get("dps")) for window in windows), default=0.0) or 1.0
    rows = []
    for window in windows:
        dps_value = _num(window.get("dps"))
        width = max(2.0, 100.0 * dps_value / max_dps)
        rows.append(
            [
                _format_number(window.get("window_s"), 0),
                _format_number(window.get("casts"), 0),
                _format_number(window.get("complete_casts"), 0),
                _format_number(window.get("hits"), 0),
                _format_number(window.get("damage_per_cast")),
                _format_number(window.get("total_damage")),
                f'<div class="bar"><span style="width:{width:.1f}%"></span></div>',
                _format_number(dps_value),
            ]
        )
    windows_table = _html_table(
        ["窗口 (s)", "施放次数", "完整施放", "命中次数", "每次施放伤害", "总伤害", "", "DPS"],
        rows,
        numeric=[0, 1, 2, 3, 4, 5, 7],
    )
    hit_rows = [
        ["命中次数 / 施放", _ui_units.fmt_compact(hits.get("per_cast"), "count")],
        ["额外命中（returns）", _ui_units.fmt_compact(hits.get("returns"), "count")],
        ["命中来源", f"技能树 {len(hits.get('sources') or [])} 处"],
        ["首击当量 / 施放", _format_number(hits.get("effective_hits_per_cast"), 3)],
        ["状态", _player_status_label(hits.get("status"))],
        ["说明", _weights_note(list(hits.get("damage_weights") or []))],
    ]
    hit_kv = _mini_kv((key, value) for key, value in hit_rows)
    inc_attack = [
        [
            _safe_text(_display_source(result, str(row.get("contribution_id")))),
            _safe_text(_display_effect(result, str(row.get("contribution_id")))),
        ]
        for row in attack.get("increased_rows") or []
        if isinstance(row, dict)
    ]
    attack_inc_table = (
        _html_table(["来源", "效果"], inc_attack)
        if inc_attack
        else '<p class="muted">无提高攻击速度来源。</p>'
    )
    more_groups = attack.get("more_groups") or []
    attack_more_table = (
        _html_table(
            ["more 组", "加成", "乘区"],
            [
                [
                    _safe_text(item.get("group")),
                    _format_number(item.get("value"), 4),
                    _multiplier_text(item.get("multiplier")),
                ]
                for item in more_groups
                if isinstance(item, dict)
            ],
            numeric=[1, 2],
        )
        if more_groups
        else '<p class="muted">无独立攻速来源。</p>'
    )
    cooldown_rows = [
        [
            _safe_text(_display_source(result, str(row.get("contribution_id")))),
            _safe_text(_display_effect(result, str(row.get("contribution_id")))),
        ]
        for row in policy.get("cooldown_rows") or []
        if isinstance(row, dict)
    ]
    cooldown_table = (
        _html_table(["来源", "效果"], cooldown_rows)
        if cooldown_rows
        else '<p class="muted">无 ICR 来源。</p>'
    )
    policy_table_rows = [
        ["主口径", _safe_text(_player_policy_label(policy.get("id"))), _format_number(policy.get("casts_per_second"), 4), _format_number(policy.get("hits_per_second"), 4)],
        ["主口径 · 每次施放伤害", "—", _format_number(policy.get("damage_per_cast")), _format_number(policy.get("damage_per_second"))],
    ]
    if alternate:
        policy_table_rows.append(
            ["备选口径", _safe_text(_player_policy_label(alternate.get("id"))), _format_number(alternate.get("casts_per_second"), 4), _format_number(alternate.get("hits_per_second"), 4)]
        )
        policy_table_rows.append(
            ["备选口径 · 每次施放伤害", "—", _format_number(alternate.get("damage_per_cast")), _format_number(alternate.get("damage_per_second"))]
        )
    policy_table = _html_table(["口径", "策略", "每秒施放 / 每秒命中", "每次施放伤害 / 秒伤害"], policy_table_rows, numeric=[2, 3])
    rotation = policy.get("scenario_rotation") or {}
    resource_note = _player_resource_note(dps.get("resource_note"))
    return f"""
<h2>命中次数与权重</h2>
<div class="cond-toolbar print-hide">
  <span class="cond-field">每次施放命中数：{_format_number(hits.get("per_cast"), 0)}（在 Buff 设置页修改）</span>
</div>
{hit_kv}
{_weights_html([float(value) for value in (hits.get("damage_weights") or [])])}
<h2>攻速</h2>
{_mini_kv([
    ('基础攻速', _format_number(attack.get('base_aps'), 3)),
    ('提高攻击速度合计', f"+{_format_number(_num(attack.get('increased_attack_speed')) * 100.0, 1)}%"),
    ('独立攻速乘区', _multiplier_text(attack.get('more_attack_speed'))),
    ('最终攻速（名义）', _format_number(attack.get('final_aps'), 3)),
    ('施放间隔', f"{_format_number(attack.get('attack_interval_s'), 2)} 秒"),
])}
<h3>提高攻击速度来源</h3>
{attack_inc_table}
<h3>独立攻速来源</h3>
{attack_more_table}
<h2>冷却与施放速率</h2>
{_mini_kv([
    ('施放策略', _player_policy_label(policy.get('id'))),
    ('基础冷却', f"{_format_number(policy.get('cooldown_s'), 2)} 秒"),
    ('有效冷却', f"{_format_number(policy.get('effective_cooldown_s'), 3)} 秒"),
    ('冷却恢复速度合计', f"+{_format_number(_num(policy.get('increased_cooldown_recovery_speed')) * 100.0, 1)}%"),
    ('实际施放速率', f"{_format_number(policy.get('casts_per_second'), 3)} 次/秒"),
    ('实际命中速率', f"{_format_number(policy.get('hits_per_second'), 3)} 次/秒"),
    ('每次施放伤害', _format_number(policy.get('damage_per_cast'))),
    ('每秒伤害', _format_number(policy.get('damage_per_second'))),
    ('循环平均乘区', _multiplier_text(policy.get('rotation_more_factor'))),
])}
<h3>ICR 来源</h3>
{cooldown_table}
<h2>策略对比</h2>
{policy_table}
<p class="section-note">主口径由冷却与攻速共同约束；备选口径同时计算（例如忽略冷却只看攻速）以便对比约束来源，不能混用或相加。</p>
<h2>DPS 窗口</h2>
{windows_table}
<h2>资源与旋转</h2>
{_mini_kv([
    ('资源假设', resource_note),
    ('循环说明', _safe_text(rotation.get('rotation_notes') or policy.get('rotation_notes') or '按技能冷却与攻速连续施放。')),
    ('其他技能施放 / 秒', _format_number(rotation.get('other_skills_per_second') or 0.0, 2)),
])}
"""


def _report_timeline(result: dict[str, Any]) -> str:
    """Dedicated timeline page (same static SVG as the BH report style)."""
    dps = _as_dict(result.get("dps"))
    hits = _as_dict(result.get("hits_per_cast"))
    policy = _as_dict(dps.get("policy"))
    windows = dps.get("windows") or []
    cps = _num(policy.get("casts_per_second"))
    cast_rows: list[list[str]] = []
    if cps > 0:
        weights = [float(value) for value in (hits.get("damage_weights") or [])]
        damage_per_cast = _num(policy.get("damage_per_cast")) or _num(dps.get("damage_per_cast"))
        cumulative = 0.0
        for index in range(8):
            cast_time = index / cps
            if cast_time > _num(windows[0].get("window_s") if windows else 5.0):
                break
            cumulative += damage_per_cast
            hit_times = ", ".join(
                f"{cast_time + offset:.2f}s" for offset in [0.0, 0.12, 0.24, 0.36][: len(weights)]
            )
            cast_rows.append(
                [
                    str(index + 1),
                    f"{cast_time:.2f}s",
                    _safe_text(hit_times),
                    _format_number(damage_per_cast),
                    _format_number(cumulative),
                ]
            )
    cast_table = (
        _html_table(
            ["施放 #", "施放时刻", "命中间（示意）", "每次施放伤害", "累计伤害"],
            cast_rows,
            numeric=[3, 4],
        )
        if cast_rows
        else '<p class="muted">无施放数据。</p>'
    )
    return f"""
<h2>时间轴与施放序列</h2>
{_timeline_svg(result)}
<p class="section-note">橙色方块 = 施放时刻；圆点 = 命中间（大小按伤害权重）。命中内部偏移是显示假设
（0 / 0.12 / 0.24 / 0.36s），不是游戏内动作时间；实际施放间隔由冷却限制决定（见上一页）。</p>
{cast_table}
<h2>窗口折算</h2>
{_html_table(
    ["窗口 (s)", "施放次数", "命中次数", "总伤害", "DPS"],
    [
        [
            _format_number(window.get("window_s"), 0),
            _format_number(window.get("casts"), 0),
            _format_number(window.get("hits"), 0),
            _format_number(window.get("total_damage")),
            _format_number(window.get("dps")),
        ]
        for window in windows
        if isinstance(window, dict)
    ],
    numeric=[0, 1, 2, 3, 4],
)}
"""


def _candidate_table(candidates: list[dict[str, Any]]) -> str:
    rows = []
    for candidate in candidates:
        modifiers = candidate.get("modifiers") or candidate.get("effects") or []
        if isinstance(modifiers, list):
            modifier_text = "；".join(
                "{stat}:{op}={value}{unit}".format(
                    stat=_text(item.get("stat_key")),
                    op=_text(item.get("operation")),
                    value=_format_number(item.get("value"), 4),
                    unit=(" " + _text(item.get("unit")) if item.get("unit") else ""),
                )
                for item in modifiers
                if isinstance(item, dict)
            )
        else:
            modifier_text = _safe_text(modifiers)
        condition = candidate.get("condition")
        condition_text = (
            json.dumps(condition, ensure_ascii=False) if condition else "—"
        )
        rows.append(
            [
                _safe_text(candidate.get("buff_id")),
                _safe_text(candidate.get("label")),
                _safe_text(candidate.get("source_kind")),
                _safe_text(candidate.get("source_path")),
                _safe_text(condition_text),
                _safe_text(candidate.get("condition_status") or ""),
                _safe_text(modifier_text),
                _status_tag(candidate.get("status")),
                "是" if candidate.get("included_in_baseline") else "否",
                _safe_text(candidate.get("next_probe") or ""),
                _safe_text(candidate.get("notes") or ""),
            ]
        )
    return _html_table(
        ["候选 id", "名称", "来源类型", "来源路径", "条件", "条件状态", "效果/修正", "状态", "进入基线", "next_probe", "备注"],
        rows,
    )


def _sensitivity_table(result: dict[str, Any]) -> str:
    sensitivity = _as_dict(result.get("buffs")).get("sensitivity") or []
    if not sensitivity:
        return '<p class="muted">本次运行未生成敏感性数据（需要 scenarios profiles/状态）。</p>'
    rows = [
        [
            _safe_text(_player_variant_label(item.get("label"))),
            _format_number(item.get("single_hit_damage")),
            _format_number(item.get("single_rel") * 100.0, 1) + "%" if item.get("single_rel") is not None else "—",
            _format_number((item.get("dps_windows") or {}).get("5s")),
            _format_number(item.get("dps_rel") * 100.0, 1) + "%" if item.get("dps_rel") is not None else "—",
            _multiplier_text(item.get("more_product")),
            _pct_or_number(item.get("crit_chance")),
        ]
        for item in sensitivity
        if isinstance(item, dict)
    ]
    return _html_table(
        ["状态切换", "单次命中", "相对主口径", "DPS 5s", "相对主口径", "more 乘区", "暴击率"],
        rows,
        numeric=[1, 2, 3, 4, 5, 6],
    )


def _offhand_detail_block(result: dict[str, Any]) -> str:
    """Show the offhand item values so they can be reviewed (user request)."""
    snapshot = _as_dict(result.get("snapshot"))
    weapon = _as_dict((snapshot.get("derived") or {}).get("weapon"))
    slots = [slot for slot in weapon.get("slots") or [] if isinstance(slot, dict)]
    if not slots:
        return '<p class="muted">快照未提供武器信息。</p>'
    blocks: list[str] = []
    for slot in slots:
        slot_id = _text(slot.get("slot"))
        if slot_id not in ("weapon1", "weapon2"):
            continue
        affix_rows = [
            [
                _safe_text(item.get("name")),
                _safe_text(item.get("slot")),
                _format_number(item.get("tier"), 0),
                _format_number(item.get("tier0"), 0),
                _safe_text(item.get("channel")),
                _safe_text(item.get("text")),
                _format_number(item.get("value"), 4),
            ]
            for item in slot.get("affixes") or []
            if isinstance(item, dict)
        ]
        implicit_rows = [
            [
                _safe_text(item.get("text")),
                _safe_text(", ".join(item.get("tags") or [])),
                _format_number(item.get("value"), 4),
            ]
            for item in slot.get("implicit_rows") or []
            if isinstance(item, dict)
        ]
        blocks.append(
            f"""
<h3>{_safe_text(slot_id)} · {_safe_text(slot.get('name') or slot.get('base_name'))}（{_safe_text(slot.get('item_type'))}）</h3>
{_mini_kv([
    ('显示稀有度', _safe_text(slot.get('rarity'))),
    ('存档/输入 rarity code', _safe_text(slot.get('raw_rarity'))),
    ('base', _safe_text(slot.get('base_name'))),
    ('attackRate（仅显示，不参与 FR 攻速）', _format_number(slot.get('attack_rate'), 3)),
    ('added weapon range', _format_number(slot.get('added_weapon_range'), 3)),
])}
<h4 class="grp-title">implicit</h4>
{_html_table(["词条", "tags", "数值"], implicit_rows, numeric=[2]) if implicit_rows else '<p class="muted">无 implicit。</p>'}
<h4 class="grp-title">affixes（tier0≥5 即 T6+，决定显示稀有度为 Exalted）</h4>
{_html_table(["词条", "槽位", "tier", "tier0", "channel", "文本", "数值"], affix_rows, numeric=[2, 3, 6]) if affix_rows else '<p class="muted">无 affix。</p>'}
"""
        )
    note = (
        '<div class="warnbox"><p><strong>Whetstone Gavel 的 offhand-Exalted 效果</strong>：'
        "条件（副手显示稀有度 Exalted）已成立；但“哪些副手 stats 被 +97% effectiveness 放大”"
        "（implicit / affix / 其中哪些条件词条）尚未确认，因此本轮不把它计入 FR 伤害，状态 "
        "<code>unresolved</code>。上表即为副手当前用于建模的数值，可据此确认作用范围。</p></div>"
    )
    return note + "".join(blocks)


_REPORT_JS = r"""
(function () {
  var tabs = Array.prototype.slice.call(document.querySelectorAll('[data-tab]'));
  var pages = Array.prototype.slice.call(document.querySelectorAll('[data-panel]'));
  function show(id) {
    tabs.forEach(function (tab) {
      var active = tab.getAttribute('data-tab') === id;
      tab.classList.toggle('is-active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    pages.forEach(function (page) {
      page.classList.toggle('is-active', page.getAttribute('data-panel') === id);
    });
  }
  tabs.forEach(function (tab) {
    tab.addEventListener('click', function () { show(tab.getAttribute('data-tab')); });
  });
  var initial = (location.hash || '').replace('#page-', '');
  if (initial && document.getElementById('page-' + initial)) { show(initial); }
  else if (tabs.length) { show(tabs[0].getAttribute('data-tab')); }
  var printButton = document.querySelector('[data-print]');
  if (printButton) { printButton.addEventListener('click', function () { window.print(); }); }

  /* source browser filters */
  var sourceRoot = document.getElementById('page-sources');
  if (sourceRoot) {
    var search = sourceRoot.querySelector('[data-source-search]');
    var category = sourceRoot.querySelector('[data-source-category]');
    var status = sourceRoot.querySelector('[data-source-status]');
    var count = sourceRoot.querySelector('[data-source-count]');
    var expand = sourceRoot.querySelector('[data-source-expand]');
    function filterSources() {
      var q = (search && search.value || '').toLowerCase();
      var cat = category && category.value || 'all';
      var state = status && status.value || 'relevant';
      var visible = 0;
      Array.prototype.forEach.call(sourceRoot.querySelectorAll('[data-source-group]'), function (group) {
        var text = (group.getAttribute('data-text') || '').toLowerCase();
        var groupState = group.getAttribute('data-status') || '';
        var matchesQ = !q || text.indexOf(q) >= 0;
        var matchesCat = cat === 'all' || group.getAttribute('data-category') === cat;
        var matchesState = state === 'all'
          || (state === 'relevant' && groupState !== '未采用')
          || groupState === state;
        var ok = matchesQ && matchesCat && matchesState;
        group.style.display = ok ? '' : 'none';
        if (ok) { visible += 1; }
      });
      if (count) { count.textContent = visible + ' 个来源'; }
    }
    if (search) { search.addEventListener('input', filterSources); }
    if (category) { category.addEventListener('change', filterSources); }
    if (status) { status.addEventListener('change', filterSources); }
    if (expand) { expand.addEventListener('click', function () {
      Array.prototype.forEach.call(sourceRoot.querySelectorAll('[data-source-group]'), function (group) {
        group.open = true;
      });
    }); }
    filterSources();
  }

  /* buff page controls */
  var root = document.getElementById('page-buffs');
  if (!root) { return; }
  var statusEl = root.querySelector('[data-cond-status]');
  function status(message) { if (statusEl) { statusEl.textContent = message; } }
  var buffSearch = root.querySelector('[data-buff-search]');
  var buffCategory = root.querySelector('[data-buff-category]');
  function filterBuffs() {
    var q = (buffSearch && buffSearch.value || '').toLowerCase();
    var cat = buffCategory && buffCategory.value || 'all';
    Array.prototype.forEach.call(root.querySelectorAll('[data-buff-card]'), function (card) {
      var text = (card.getAttribute('data-text') || '').toLowerCase();
      var ok = (!q || text.indexOf(q) >= 0) && (cat === 'all' || card.getAttribute('data-category') === cat);
      card.style.display = ok ? '' : 'none';
    });
  }
  if (buffSearch) { buffSearch.addEventListener('input', filterBuffs); }
  if (buffCategory) { buffCategory.addEventListener('change', filterBuffs); }
  filterBuffs();

  function hitWeights(hits) {
    var weights = [];
    for (var i = 0; i < hits; i += 1) { weights.push(i === 0 ? 1.0 : 0.5); }
    return weights;
  }
  function readScenario() {
    var states = {};
    Array.prototype.forEach.call(root.querySelectorAll('[data-cond][data-state]'), function (el) {
      var state = el.getAttribute('data-state');
      if (el.type === 'checkbox') { states[state] = el.checked; }
    });
    var blade = parseInt((root.querySelector('[data-cond-stacks="blade_weaving_stacks"]') || {}).value, 10) || 0;
    var ambition = parseInt((root.querySelector('[data-cond-stacks="ambition_stacks"]') || {}).value, 10) || 0;
    states['blade_weaving_stacks'] = blade;
    states['ambition_stacks'] = ambition;
    states['target_in_melee_range'] = true;
    var hitsInput = document.querySelector('[data-cond-hits]');
    var hits = parseInt(hitsInput && hitsInput.value, 10) || 4;
    var levelInput = root.querySelector('[data-cond-level]');
    var level = parseInt(levelInput && levelInput.value, 10) || 100;
    var targetSelect = root.querySelector('[data-cond-target]');
    return {
      baseline_profile: 'build_base',
      states: states,
      hit_override: { hits_per_cast: hits, damage_weights: hitWeights(hits) },
      target: { boss_or_rare: !targetSelect || targetSelect.value !== 'normal', monster_level: level, fire_resistance: 0.0 }
    };
  }
  function applyScenario(scenario) {
    if (!scenario || typeof scenario !== 'object') { return; }
    var states = scenario.states || {};
    Array.prototype.forEach.call(root.querySelectorAll('[data-cond][data-state]'), function (el) {
      var state = el.getAttribute('data-state');
      if (el.type === 'checkbox' && Object.prototype.hasOwnProperty.call(states, state)) {
        el.checked = !!states[state];
      }
    });
    var blade = root.querySelector('[data-cond-stacks="blade_weaving_stacks"]');
    if (blade && states.blade_weaving_stacks !== undefined) { blade.value = String(states.blade_weaving_stacks); }
    var ambition = root.querySelector('[data-cond-stacks="ambition_stacks"]');
    if (ambition && states.ambition_stacks !== undefined) { ambition.value = String(states.ambition_stacks); }
    var hitsInput = document.querySelector('[data-cond-hits]');
    if (hitsInput && scenario.hit_override && scenario.hit_override.hits_per_cast) { hitsInput.value = String(scenario.hit_override.hits_per_cast); }
    var level = root.querySelector('[data-cond-level]');
    if (level && scenario.target && scenario.target.monster_level) { level.value = String(scenario.target.monster_level); }
    var targetSelect = root.querySelector('[data-cond-target]');
    if (targetSelect && scenario.target) { targetSelect.value = scenario.target.boss_or_rare ? 'boss' : 'normal'; }
    status('设置已更改，点击“计算 DPS”应用。');
  }
  var initialScenario = readScenario();
  root.addEventListener('change', function () { status('设置已更改，点击“计算 DPS”应用。'); });
  root.addEventListener('input', function () { status('设置已更改，点击“计算 DPS”应用。'); });

  var compute = root.querySelector('[data-cond-compute]');
  if (compute) {
    compute.addEventListener('click', function () {
      status('计算中……');
      fetch('http://127.0.0.1:8765/recompute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scenario: readScenario() })
      }).then(function (response) {
        if (!response.ok) {
          return response.json().then(function (data) {
            throw new Error((data && data.error) || ('HTTP ' + response.status));
          });
        }
        var caseId = response.headers.get('X-Recompute-Case');
        if (caseId && caseId !== 'flame-reave') { throw new Error('案例不匹配：' + caseId); }
        return response.text().then(function (html) {
          document.open();
          document.write(html);
          document.close();
        });
      }).catch(function (error) {
        status('计算失败：' + error.message + '（请确认已启动本机服务，旧结果保持不变）');
      });
    });
  }
  var save = root.querySelector('[data-cond-save]');
  if (save) {
    save.addEventListener('click', function () {
      try {
        var payload = JSON.stringify(readScenario(), null, 2);
        var link = document.createElement('a');
        link.href = 'data:application/json;charset=utf-8,' + encodeURIComponent(payload);
        link.download = 'flame-reave-scenario.json';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        status('配置已下载。');
      } catch (error) { status('保存失败：' + error.message); }
    });
  }
  var load = root.querySelector('[data-cond-load]');
  if (load) {
    load.addEventListener('change', function () {
      var file = load.files && load.files[0];
      if (!file) { return; }
      var reader = new FileReader();
      reader.onload = function () {
        try { applyScenario(JSON.parse(String(reader.result || '{}'))); }
        catch (error) { status('载入失败：' + error.message); }
      };
      reader.readAsText(file);
    });
  }
  var reset = root.querySelector('[data-cond-reset]');
  if (reset) {
    reset.addEventListener('click', function () {
      applyScenario(initialScenario);
      status('Buff 已重置为生成时的配置。');
    });
  }
  var preset = root.querySelector('[data-cond-preset]');
  if (preset) {
    preset.addEventListener('change', function () {
      if (preset.value === 'legacy_single_skill') {
        var high = root.querySelector('[data-state="enemy_high_health"]');
        if (high) { high.checked = false; }
        status('已选择 legacy_single_skill 预设；点击计算应用。');
      } else {
        var highBase = root.querySelector('[data-state="enemy_high_health"]');
        if (highBase) { highBase.checked = true; }
        status('已选择 build_base 预设；点击计算应用。');
      }
    });
  }
})();
"""


# ---------------------------------------------------------------------------
# Run directory writer
# ---------------------------------------------------------------------------


def _manifest_files(out: Path, names: Iterable[str]) -> dict[str, str]:
    files: dict[str, str] = {}
    for name in names:
        path = out / name
        if path.is_file():
            files[name] = _sha256_file(path)
    return files


def write_hit_run(
    result: dict[str, Any],
    out_dir: str | Path,
    *,
    snapshot_document: Optional[dict[str, Any]] = None,
    ruleset_document: Optional[dict[str, Any]] = None,
    screening_document: Optional[dict[str, Any]] = None,
    render_html: bool = True,
) -> dict[str, str]:
    """Write the complete hit run directory and return ``{filename: path}``.

    The manifest is written **last** so its ``files`` block can hash every other
    artifact; ``run-manifest.json`` itself is intentionally not self-hashed.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}

    def write_json_artifact(name: str, payload: Any) -> None:
        path = _write_json(out / name, payload)
        written[name] = str(path)

    write_json_artifact(RESULT_FILENAME, result)

    snapshot_doc = _as_dict(snapshot_document) or _as_dict(result.get("snapshot"))
    write_json_artifact(SNAPSHOT_FILENAME, snapshot_doc)

    source_rows = list(result.get("source_ledger") or [])
    write_json_artifact(
        SOURCE_LEDGER_FILENAME,
        {
            "schema": SOURCE_LEDGER_SCHEMA,
            "run_id": _as_dict(result.get("run")).get("run_id"),
            "build_meta": _as_dict(_as_dict(snapshot_doc).get("build_meta")),
            "summary": summarize_source_ledger(source_rows),
            "rows": source_rows,
        },
    )

    rule_rows = list(result.get("rule_ledger") or [])
    ruleset_binding_block = _as_dict(_as_dict(result.get("run")).get("ruleset"))
    ruleset_doc = _as_dict(ruleset_document)
    effect_rows = [
        {
            "effect_id": _text(_pick(effect, "id", "effect_id", "buff_id")),
            "label": _text(_pick(effect, "label", "name")),
            "status": _text(effect.get("status")),
            "condition": effect.get("condition"),
        }
        for effect in ruleset_doc.get("effects") or []
        if isinstance(effect, dict)
    ]
    write_json_artifact(
        RULE_LEDGER_FILENAME,
        {
            "schema": RULE_LEDGER_SCHEMA,
            "run_id": _as_dict(result.get("run")).get("run_id"),
            "ruleset": {
                **ruleset_binding_block,
                "schema": ruleset_binding_block.get("schema") or ruleset_doc.get("schema"),
            },
            "summary": summarize_rule_ledger(rule_rows),
            "open_questions": list(ruleset_doc.get("open_questions") or []),
            "effects": effect_rows,
            "rows": rule_rows,
        },
    )

    screening_doc = _as_dict(screening_document)
    if not screening_doc:
        screening_doc = {
            "schema": BUFF_SCREENING_SCHEMA,
            "run_id": _as_dict(result.get("run")).get("run_id"),
            "summary": _as_dict(_as_dict(result.get("buffs")).get("screening")).get("summary") or {},
            "candidates": _as_dict(_as_dict(result.get("buffs")).get("screening")).get("candidates") or [],
        }
    written_screening = _write_json(out / BUFF_SCREENING_FILENAME, screening_doc)
    written[BUFF_SCREENING_FILENAME] = str(written_screening)

    readme_path = _write_text(out / README_FILENAME, render_readme(result))
    written[README_FILENAME] = str(readme_path)

    if render_html:
        html_path = _write_text(out / HTML_FILENAME, render_hit_html(result))
        written[HTML_FILENAME] = str(html_path)

    ordered_names = [
        RESULT_FILENAME,
        SNAPSHOT_FILENAME,
        SOURCE_LEDGER_FILENAME,
        RULE_LEDGER_FILENAME,
        BUFF_SCREENING_FILENAME,
        README_FILENAME,
    ]
    if render_html:
        ordered_names.append(HTML_FILENAME)
    consistency = _as_dict(result.get("consistency"))
    manifest = {
        "schema": RUN_MANIFEST_SCHEMA,
        "run": _as_dict(result.get("run")),
        "input": _as_dict(result.get("input")),
        "scope": _as_dict(result.get("scope")),
        "scenario": _as_dict(result.get("scenario")),
        "ruleset": ruleset_binding_block,
        "consistency": consistency,
        "files": _manifest_files(out, ordered_names),
    }
    manifest_path = _write_json(out / MANIFEST_FILENAME, manifest)
    written[MANIFEST_FILENAME] = str(manifest_path)
    return written


def result_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Compact CLI summary (printed by ``python -m le_calc hit-run``)."""
    run = _as_dict(result.get("run"))
    single = _as_dict(result.get("single_hit"))
    totals = _as_dict(single.get("totals"))
    dps = _as_dict(result.get("dps"))
    hits = _as_dict(result.get("hits_per_cast"))
    screening = _as_dict(_as_dict(result.get("buffs")).get("screening"))
    return {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "ruleset": _as_dict(run.get("ruleset")).get("id"),
        "single_hit": _num(totals.get("damage")),
        "avg_crit": _num(totals.get("avg_crit"), 1.0),
        "hits_per_cast": hits.get("per_cast"),
        "aps": _num(_as_dict(dps.get("attack_rate")).get("final_aps")),
        "windows": [
            {
                "window_s": window.get("window_s"),
                "total_damage": window.get("total_damage"),
                "dps": window.get("dps"),
            }
            for window in dps.get("windows") or []
        ],
        "buff_screening": _as_dict(screening.get("summary")),
        "consistency": _as_dict(result.get("consistency")),
        "issues": [
            {
                "id": issue.get("issue_id"),
                "severity": issue.get("severity"),
                "title": issue.get("title"),
            }
            for issue in result.get("issues") or []
        ],
    }


__all__ = [
    "BUFF_SCREENING_FILENAME",
    "BUFF_SCREENING_SCHEMA",
    "HTML_FILENAME",
    "MANIFEST_FILENAME",
    "README_FILENAME",
    "RESULT_FILENAME",
    "REQUIRED_WINDOWS",
    "RULE_LEDGER_FILENAME",
    "RULE_LEDGER_SCHEMA",
    "RUN_MANIFEST_SCHEMA",
    "SCOPE_NOTE",
    "SCHEMA_VERSION",
    "SNAPSHOT_FILENAME",
    "SOURCE_LEDGER_FILENAME",
    "SOURCE_LEDGER_SCHEMA",
    "build_hit_result",
    "build_rule_ledger_rows",
    "build_source_ledger_rows",
    "consistency_checks",
    "render_hit_html",
    "render_readme",
    "result_summary",
    "ruleset_binding",
    "ruleset_to_document",
    "snapshot_to_document",
    "summarize_rule_ledger",
    "summarize_source_ledger",
    "write_hit_run",
]


# ===========================================================================
# P3R shared report layer (player-report-ui contract HTML-01/04/09/10/11/12)
# The hit model now renders through the shared report_ui document: shared
# header/nav/footer/source panel/drawers/embedded JSON/metadata, with only the
# model-specific result pages supplied here.  Player text below is projected
# from the current result data; engineering fields stay in the result JSON.
# ===========================================================================

_PLAYER_STATUS_LABELS = {
    "confirmed": "已计入",
    "derived": "推导值",
    "assumed": "按假设生效",
    "included": "已计入",
    "modeled": "已建模",
    "not_modeled": "不适用",
    "unresolved": "待确认",
    "unsupported": "不适用",
}

_POLICY_LABELS = {
    "cooldown_limited": "冷却与攻速共同约束",
    "attack_speed_limited": "攻速约束",
    "cooldown": "冷却约束",
    "hybrid": "冷却与攻速共同约束",
}

_BASELINE_LABELS = {
    "build_base": "完整构筑基线（常驻效果生效）",
    "legacy_single_skill": "仅单技能基线（关闭常驻效果）",
}

_SOURCE_CATEGORY_LABELS = {
    "equipment": "装备",
    "idol": "神像",
    "blessing": "祝福",
    "passive_node": "被动天赋",
    "skill_node": "技能节点",
    "skill": "技能",
    "base_character": "角色基础",
    "unique": "传奇装备",
    "unique_tooltip": "传奇效果",
}

_STATE_LABELS = {
    "enemy_high_health": "目标高血量",
    "ignited": "目标被点燃",
    "frenzy_active": "Frenzy 生效",
    "consume_blade_weaving": "消耗 Blade Weaving",
    "blade_weaving_stacks": "Blade Weaving 层数",
    "ambition_stacks": "Ambition 层数",
    "consume_rhythm_of_fire": "消耗 Rhythm of Fire",
    "firebrand_stacks": "Firebrand 层数",
    "rhythm_of_fire_stacks": "Rhythm of Fire 层数",
    "mana_ge_300": "法力 ≥ 300",
    "enchant_weapon_active": "Enchant Weapon 生效",
    "dual_wield": "双持",
    "boss_or_rare": "Boss / 稀有目标",
    "target_in_melee_range": "目标在近战范围内",
    "melee_ability_mana_cost": "近战技能消耗法力",
}

_STAT_LABELS = {
    "attack_speed": "攻击速度",
    "cast_speed": "施法速度",
    "damage": "伤害",
    "fire_damage": "火焰伤害",
    "elemental_damage": "元素伤害",
    "melee_damage": "近战伤害",
    "critical_strike_chance": "暴击率",
    "critical_strike_multiplier": "暴击伤害加成",
    "crit_chance": "暴击率",
    "crit_multiplier": "暴击伤害加成",
    "added_damage": "附加伤害",
    "intelligence": "智力",
    "dexterity": "敏捷",
    "strength": "力量",
    "vitality": "活力",
    "attunement": "协调",
    "mana": "法力",
    "health": "生命",
    "all_attributes": "全属性",
}

_TAG_CN = {
    "fire": "火焰", "fire_damage": "火焰", "cold": "冰霜", "lightning": "闪电",
    "physical": "物理", "void": "虚空", "necrotic": "腐化", "poison": "毒素",
    "melee": "近战", "spell": "法术", "elemental": "元素",
}


def _signed_number(value: float, *, percent: bool = False, digits: int = 1) -> str:
    sign = "+" if value >= 0 else "−"
    amount = abs(value) * (100.0 if percent else 1.0)
    suffix = "%" if percent else ""
    return f"{sign}{_format_number(amount, digits)}{suffix}"


def _player_status_label(value: Any) -> str:
    text = _text(value)
    return _PLAYER_STATUS_LABELS.get(text, "已计入" if not text else _STATE_LABELS.get(text, "已计入"))


def _player_policy_label(value: Any) -> str:
    text = _text(value)
    if text in _POLICY_LABELS:
        return _POLICY_LABELS[text]
    lowered = text.lower().replace("_", " ")
    if "attack" in lowered and "cooldown" in lowered:
        return _POLICY_LABELS["cooldown_limited"]
    if "attack" in lowered:
        return _POLICY_LABELS["attack_speed_limited"]
    if "cooldown" in lowered:
        return _POLICY_LABELS["cooldown"]
    return "冷却与攻速共同约束"


def _stat_label(value: Any) -> str:
    text = _text(value)
    if not text:
        return "属性"
    if text in _STAT_LABELS:
        return _STAT_LABELS[text]
    key = text.lower().strip().replace(" ", "_")
    if key in _STAT_LABELS:
        return _STAT_LABELS[key]
    if "property" in key or "stat_" in key:
        return "属性"
    return "属性"


_STAT_NAME_CN = {
    "increased experience": "经验获取",
    "ailment chance": "异常几率",
    "increased stun chance": "眩晕几率",
    "armor": "护甲",
    "damage taken": "受到伤害",
    "health": "生命",
    "mana": "法力",
    "increased mana": "法力上限",
    "increased health": "生命上限",
    "movement speed": "移动速度",
    "increased movement speed": "移动速度",
    "critical strike avoidance": "暴击规避",
    "endurance": "耐力",
    "increased armor": "护甲",
    "increased damage": "伤害加成",
    "chance to shred armor": "护甲削减几率",
    "minion damage": "召唤物伤害",
    "increased minion damage": "召唤物伤害",
    "increased attack speed": "攻击速度",
    "increased cast speed": "施法速度",
    "increased cooldown recovery speed": "冷却恢复速度",
    "increased fire damage": "火焰伤害加成",
    "increased cold damage": "冰霜伤害加成",
    "increased lightning damage": "闪电伤害加成",
    "increased elemental damage": "元素伤害加成",
    "increased physical damage": "物理伤害加成",
    "increased void damage": "虚空伤害加成",
    "increased necrotic damage": "腐化伤害加成",
    "increased poison damage": "毒素伤害加成",
}


def _player_unsupported_effect(row: dict[str, Any]) -> str:
    """Readable label for stats the model does not use (never ``属性 +N``)."""

    stat_name = _text(row.get("stat_name") or row.get("target"))
    key = stat_name.strip().lower()
    label = _STAT_NAME_CN.get(key)
    unit = _text(row.get("unit"))
    value = _num(row.get("value"))
    if label:
        return f"{label} {_signed_number(value, percent=unit == 'percent')}"
    if len(stat_name) <= 24 and not re.search(r"[A-Za-z]{4,}", stat_name):
        return stat_name
    return "未建模效果（不参与本次伤害计算）"


def _player_effect(row: dict[str, Any]) -> str:
    """Human-readable effect with semantic units (percent/flat/stacks/seconds)."""

    sk = _text(row.get("stat_key"))
    op = _text(row.get("operation"))
    unit = _text(row.get("unit"))
    value = _num(row.get("value"))
    tags = [_TAG_CN.get(str(tag).lower(), "") for tag in row.get("damage_tags") or []]
    tag_text = "/".join(tag for tag in tags if tag)
    target = _text(row.get("target"))
    label = _stat_label(sk or row.get("stat_name") or target)
    if sk in ("unsupported", "not_modeled"):
        return _player_unsupported_effect(row)
    if sk == "added_damage":
        return f"附加{tag_text or '自适应'}伤害 {_signed_number(value, digits=1)}"
    if sk in ("damage", "fire_damage", "elemental_damage", "melee_damage") or "damage" in sk:
        kind_label = "独立伤害加成" if op in ("more", "less") else "伤害加成"
        tag_prefix = f"{tag_text}" if tag_text else ""
        return f"{tag_prefix}{kind_label} {_signed_number(value, percent=unit != 'flat')}"
    if sk in ("attack_speed", "cast_speed"):
        return f"{label} {_signed_number(value, percent=unit != 'flat')}"
    if sk in ("crit_chance", "critical_strike_chance"):
        return f"暴击率 {_signed_number(value, percent=True)}"
    if sk in ("crit_multiplier", "critical_strike_multiplier"):
        return f"暴击伤害加成 {_signed_number(value, percent=True)}"
    if sk in ("attribute", "all_attributes"):
        return f"{_STAT_LABELS.get(target, '全属性' if sk == 'all_attributes' else '属性')} {_signed_number(value, digits=0)}"
    if sk == "resistance":
        tag = _TAG_CN.get(str(target or "").lower(), "")
        label = f"{tag}抗性" if tag else "抗性"
        return f"{label} {_signed_number(value, percent=True)}"
    if sk in ("penetration", "cooldown_recovery_speed", "mana_cost", "skill_duration", "duration"):
        label_text = {
            "penetration": "穿透",
            "cooldown_recovery_speed": "冷却恢复速度",
            "mana_cost": "法力消耗",
            "skill_duration": "持续时间",
            "duration": "持续时间",
        }[sk]
        return f"{label_text} {_signed_number(value, percent=True)}"
    if sk == "cooldown_flat":
        return f"冷却时间 {_signed_number(value, digits=2)} 秒"
    if sk in ("mana", "health"):
        return f"{label} {_signed_number(value, digits=0)}"
    if unit == "percent":
        return f"{label} {_signed_number(value, percent=True)}"
    return f"{label} {_signed_number(value, digits=1)}"


def _baseline_block(result: dict[str, Any]) -> str:
    """Both baseline profiles in readable player language (no internal names)."""

    baselines = _as_dict(result.get("baselines"))
    profiles = _as_dict(baselines.get("profiles"))
    if not profiles:
        return '<p class="muted">本次运行未提供基线对比。</p>'
    current = _text(baselines.get("profile")) or _text(_as_dict(result.get("scenario")).get("baseline_profile"))
    rows: list[list[str]] = []
    for profile_id, block in profiles.items():
        # ``result.baselines.profiles`` is the normalized display shape:
        # single_hit_damage / dps_windows{5s,30s,120s} / casts_per_second.
        data = _as_dict(block)
        windows = _as_dict(data.get("dps_windows"))
        row = [
            _BASELINE_LABELS.get(profile_id, "基线口径"),
            _format_number(data.get("single_hit_damage")),
        ]
        for seconds in ("5s", "30s", "120s"):
            value = windows.get(seconds)
            row.append(_format_number(value) if value is not None else "—")
        rows.append(row)
    if all(_format_number(_as_dict(profile).get("single_hit_damage")) == "" for profile in profiles.values()):
        # Last-resort readability: never render a table that is all em-dashes.
        return '<p class="muted">本次运行的基线数据不可用（缺少归一化基线字段）。</p>'
    table = _html_table(
        ["基线口径", "单次命中", "5 秒 DPS", "30 秒 DPS", "120 秒 DPS"],
        rows,
        numeric=[1, 2, 3, 4],
    )
    return (
        '<p class="section-note">下表两套口径只作对照，不能相减当作“条件增益”；'
        '当前页面主口径见总览顶部说明。</p>'
        + table
    )


def _group_effect_pairs(group: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for row in group["rows"]:
        pairs.append((_safe_text(_player_effect(row)), _status_tag(_row_status(row))))
    return pairs


def _report_sources(result: dict[str, Any]) -> str:
    """Three-column source browser (HTML-09): 来源 / 效果 / 状态, grouped by instance."""

    rows = _ledger_rows(result)
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        kind = _text(row.get("source_kind"))
        label = _strip_node_suffix(_text(row.get("source_label"))) or _source_location_label(row)
        location = _text(row.get("source_location") or row.get("source_path"))
        instance = _source_instance(location) or location or label
        key = instance
        group = groups.setdefault(
            key,
            {
                "kind": kind,
                "label": label,
                "location": location,
                "rows": [],
                "labels": set(),
            },
        )
        group["rows"].append(row)
        group["labels"].add(label)
    ordered = sorted(
        groups.values(),
        key=lambda group: (
            {"base_character": 0, "equipment": 1, "idol": 2, "blessing": 3, "passive_node": 4, "skill_node": 5, "skill": 6}.get(group["kind"], 9),
            group["label"],
            group["location"],
        ),
    )
    bodies: list[str] = []
    for index, group in enumerate(ordered):
        statuses = {_row_status(row) for row in group["rows"]}
        main_status = "生效" if "生效" in statuses else sorted(statuses)[0]
        category = _SOURCE_CATEGORY_LABELS.get(group["kind"], "来源")
        caption = _source_location_label({"source_kind": group["kind"], "source_location": group["location"]})
        source_cell = (
            f'<span class="source-name">{_safe_text(group["label"])}</span>'
            f'<span class="source-caption">{_safe_text(caption)}</span>'
        )
        effect_rows: list[str] = []
        for row_index, row in enumerate(group["rows"]):
            status = _row_status(row)
            effect = _safe_text(_player_effect(row))
            if row_index == 0:
                cells = (
                    f'<td rowspan="{len(group["rows"])}" class="source-main">{source_cell}</td>'
                    f"<td>{effect}</td><td>{_status_tag(status)}</td>"
                )
            else:
                cells = f"<td>{effect}</td><td>{_status_tag(status)}</td>"
            effect_rows.append(f"<tr>{cells}</tr>")
        group_attrs = (
            f'data-category="{_esc(group["kind"])}" '
            f'data-status="{_esc(main_status)}" '
            f'data-text="{_esc(group["label"] + " " + caption + " " + category)}"'
        )
        bodies.append(
            shared_ui.source_group(
                name=group["label"],
                caption=caption,
                source_id=group.get("location") or group["label"],
                group_attrs=group_attrs,
                effects=_group_effect_pairs(group),
            )
        )
    categories = [
        ("all", "全部"), ("equipment", "装备"), ("idol", "神像"), ("blessing", "祝福"),
        ("passive_node", "被动天赋"), ("skill_node", "技能节点"), ("skill", "技能"),
        ("base_character", "角色基础"),
    ]
    category_options = "".join(f'<option value="{key}">{label}</option>' for key, label in categories)
    table = shared_ui.source_table(
        body_html="".join(bodies),
        table_class="source-table",
        caption="增伤来源：来源、效果与当前状态；可搜索和筛选。",
        headers=("来源", "效果", "状态"),
    )
    return f"""
<div class="cond-toolbar print-hide">
  <label class="cond-field">搜索 <input type="search" data-source-search placeholder="装备/技能/属性名"></label>
  <label class="cond-field">类别 <select data-source-category>{category_options}</select></label>
  <label class="cond-field">状态 <select data-source-status>
    <option value="relevant" selected>生效/未启用</option>
    <option value="all">全部（含未采用）</option>
    <option value="生效">仅生效</option>
    <option value="未启用">仅未启用</option>
    <option value="条件未满足">仅条件未满足</option>
  </select></label>
  <button type="button" class="btn btn-small" data-source-expand>展开全部</button>
  <span class="cond-status" data-source-count role="status"></span>
</div>
{table}
<p class="section-note">同一来源的多项效果按同一实例归组；不同实例即使同名也分行保留。</p>
"""


def _candidate_label(candidate: dict[str, Any]) -> tuple[str, str]:
    text = _text(candidate.get("label")) or "来源效果"
    if " — " in text:
        name, _, description = text.partition(" — ")
        return name.strip(), description.strip()
    if " - " in text:
        name, _, description = text.partition(" - ")
        return name.strip(), description.strip()
    return text, ""


def _candidate_condition_text(condition: Any) -> str:
    condition = _as_dict(condition)
    state = _text(condition.get("state"))
    if not state:
        return "常驻效果"
    label = _STATE_LABELS.get(state, "配置条件")
    value = condition.get("value")
    if isinstance(value, bool):
        return f"需要：{label}"
    if isinstance(value, (int, float)):
        return f"需要：{label} = {_format_number(value, 0)}"
    return f"需要：{label}"


_VARIANT_TEXT = (
    ("increased attack speed", "提高攻击速度"),
    ("added melee fire", "附加近战火焰伤害"),
    ("more Fire", "火焰独立增伤"),
    ("more Elemental", "元素独立增伤"),
    ("more damage", "独立增伤"),
    ("crit ", "暴击 "),
    ("ignited", "点燃"),
    ("Frenzy (active)", "Frenzy（生效）"),
)


def _player_variant_label(value: Any) -> str:
    """Readable player label for a sensitivity variant (data labels are engine text)."""

    text = _text(value) or "状态切换"
    for english, chinese in _VARIANT_TEXT:
        text = text.replace(english, chinese)
    text = text.replace("(", "（").replace(")", "）")
    return text


_GENERIC_NAME_WORDS = {
    "affix",
    "skill",
    "passive",
    "node",
    "equipment",
    "unique",
    "idol",
    "blessing",
    "effect",
}


def _readonly_candidate_label(candidate: dict[str, Any], category: str) -> str:
    """Readable label for an unsupported candidate.

    Unsupported entries often carry raw game-text dumps with internal locators
    (``idols.2: ...``) and English engine phrases.  Only a short proper-noun
    leading word is kept; everything else becomes a readable category label.
    """

    name, _description = _candidate_label(candidate)
    first = name.split(" (")[0].split("（")[0].split(" — ")[0].strip()
    token = first.split(" ")[0].strip(" :：,，")
    if (
        token
        and token[0].isupper()
        and len(token) <= 24
        and token.lower() not in _GENERIC_NAME_WORDS
        and all(ord(character) < 128 for character in token)
    ):
        return token
    return category if category.endswith("效果") else f"{category}效果"


def _player_candidate_name(candidate: dict[str, Any], category: str) -> tuple[str, str]:
    """Name/description pair safe for player text (no raw game-text dumps)."""

    name, description = _candidate_label(candidate)
    lowered = name.lower()
    raw_markers = (
        "affix",
        "increased ",
        "more ",
        "added ",
        "immune",
        "doubled",
        "per stack",
        "idols.",
        "equipment.",
        "blessings.",
    )
    if not description and any(marker in lowered for marker in raw_markers):
        return f"{category}效果", ""
    return name, description


def _candidate_value_lines(candidate: dict[str, Any]) -> list[str]:
    modifiers = candidate.get("modifiers") or []
    lines: list[str] = []
    for modifier in modifiers:
        if not isinstance(modifier, dict):
            continue
        operation = _text(modifier.get("operation"))
        value = modifier.get("value", modifier.get("value_per_stack"))
        unit = _text(modifier.get("unit")) or ("percent" if "damage" in _text(modifier.get("stat_key")) else "flat")
        stat_key = _text(modifier.get("stat_key"))
        target = _text(modifier.get("target"))
        label = _STAT_LABELS.get(stat_key, _STATE_LABELS.get(stats, "") if (stats := "") else _stat_label(stat_key))
        per_stack = modifier.get("value_per_stack") is not None
        amount = _signed_number(
            _num(value), percent=unit == "percent" and abs(_num(value)) <= 1.0
        )
        suffix = " / 层" if per_stack else ""
        if operation in ("more", "less"):
            lines.append(f"{label}独立乘区 {amount}{suffix}")
        elif operation in ("increased", "reduced", "add"):
            lines.append(f"{label}加成 {amount}{suffix}")
        elif operation == "added":
            lines.append(f"附加{_TAG_CN.get(target, '')}伤害 {amount}{suffix}")
        else:
            lines.append(f"{label} {amount}{suffix}")
    return lines


def _condition_parameters_block(
    result: dict[str, Any], capabilities: Optional[dict[str, Any]] = None
) -> str:
    """Buff cards from the current screening data (no hardcoded values/states)."""

    scenario = _as_dict(result.get("scenario"))
    states = _as_dict(scenario.get("states"))
    screening = _as_dict(_as_dict(result.get("buffs")).get("screening"))
    candidates = [item for item in screening.get("candidates") or [] if isinstance(item, dict)]
    baseline_profile = _text(scenario.get("baseline_profile")) or "build_base"
    control_declarations: dict[str, dict[str, Any]] = {}
    for control in _as_list(_as_dict(capabilities).get("buff_controls")):
        if not isinstance(control, dict):
            continue
        key = _text(control.get("state_key") or control.get("state_id"))
        if key:
            control_declarations[key] = dict(control)
    interactive: list[str] = []
    readonly: list[str] = []
    for candidate in candidates:
        name, description = _candidate_label(candidate)
        if not name:
            continue
        category_for_name = _SOURCE_CATEGORY_LABELS.get(_text(candidate.get("source_kind")), "来源")
        name, description = _player_candidate_name(candidate, category_for_name)
        coverage = _text(candidate.get("coverage")) or _text(candidate.get("status"))
        condition = _as_dict(candidate.get("condition"))
        state = _text(condition.get("state"))
        category = _SOURCE_CATEGORY_LABELS.get(_text(candidate.get("source_kind")), "来源")
        condition_text = _candidate_condition_text(condition)
        value_lines = _candidate_value_lines(candidate)
        applied = bool(candidate.get("applied"))
        included_baseline = bool(candidate.get("included_in_baseline"))
        max_stacks = int(_num(candidate.get("max_stacks")) or 0)
        if coverage in ("not_modeled", "unresolved", "unsupported"):
            safe_name = _readonly_candidate_label(candidate, category)
            readonly.append(
                f'<li><span class="source-name">{_safe_text(safe_name)}</span>'
                f'<span class="source-caption">{_safe_text(category)} · {_safe_text(condition_text)}</span>'
                f'<span class="badge badge-muted">{_PLAYER_STATUS_LABELS.get(coverage, "不适用")}</span></li>'
            )
            continue
        if not state:
            status = "常驻（已计入基线）" if included_baseline else ("已计入" if applied else "未启用")
            control = '<span class="badge badge-muted">只读</span>'
        else:
            declaration = control_declarations.get(state) or {}
            declared_kind = _text(declaration.get("kind")).lower()
            is_number = declared_kind in ("stacks", "int", "integer", "number", "float")
            if not declared_kind:
                is_number = state.endswith("_stacks") or max_stacks > 1
            if is_number:
                current = int(_num(states.get(state)) or 0)
                minimum = int(_num(declaration.get("min"), 0) or 0)
                maximum = int(
                    _num(declaration.get("max"), max_stacks or 20) or (max_stacks or 20)
                )
                control = (
                    f'<label class="cond-field">层数 <input type="number" data-cond data-state="{_esc(state)}" '
                    f'min="{minimum}" max="{maximum}" value="{current}"></label>'
                )
                status = "生效" if applied else "未启用"
            else:
                checked = bool(states.get(state)) or applied
                control = (
                    f'<label class="toggle"><input type="checkbox" data-cond data-state="{_esc(state)}"'
                    f'{" checked" if checked else ""}> 启用</label>'
                )
                status = "生效" if checked else "未启用"
        if candidate.get("assumption"):
            status += " · 按假设"
        effect_html = "".join(
            f'<div class="cond-effect-line">{_safe_text(line)}</div>' for line in value_lines
        )
        interactive.append(
            shared_ui.effect_card(
                title=name,
                subtitle=f"{category} · {condition_text}" if condition_text else category,
                status_label=status,
                status_tone="ok" if "生效" in status else "muted",
                control_html=f'<div class="cond-control">{control}</div>',
                value_html=effect_html,
                attrs=(
                    f'data-buff-card data-category="{_esc(_text(candidate.get("source_kind")))}" '
                    f'data-text="{_esc(name + " " + description + " " + category + " " + condition_text)}"'
                ),
                card_class="cond-effect-card cond-effect",
                variant="hit",
            )
        )
    preset_options = "".join(
        f'<option value="{key}"{" selected" if key == baseline_profile else ""}>'
        f'{_BASELINE_LABELS[key]}</option>'
        for key in ("build_base", "legacy_single_skill")
    )
    hits = _as_dict(result.get("hits_per_cast"))
    toolbar = shared_ui.action_bar(
        (
            f'<label class="cond-field">基线预设 <select data-cond-preset>{preset_options}</select></label>'
            '<button type="button" class="btn btn-primary" data-cond-compute>计算 DPS</button>'
            '<button type="button" class="btn" data-cond-save>保存配置</button>'
            '<label class="btn btn-file">载入配置<input type="file" accept="application/json" data-cond-load hidden></label>'
            '<button type="button" class="btn" data-cond-reset>重置 Buff</button>'
            '<span class="cond-status" data-cond-status role="status"></span>'
        ),
        aria_label="预设与配置",
    )
    toolbar += (
        '<p class="section-note" data-recalc-offline-note hidden></p>'
        '<div class="cond-toolbar print-hide">'
        '<label class="cond-field">搜索 <input type="search" data-buff-search placeholder="Buff / 装备 / 属性名"></label>'
        '<label class="cond-field">来源 <select data-buff-category><option value="all">全部</option>'
        '<option value="buff">Buff</option><option value="skill">技能</option><option value="gear">装备</option></select></label>'
        "</div>"
    )
    hit_parameter = {}
    for declaration in _as_list(_as_dict(capabilities).get("model_parameters")):
        if isinstance(declaration, dict) and _text(declaration.get("key")) == "hits_per_cast":
            hit_parameter = declaration
            break
    hit_min = int(_num(hit_parameter.get("min"), 1) or 1)
    hit_max = int(_num(hit_parameter.get("max"), 8) or 8)
    skill_settings = (
        '<h3>技能设置</h3><div class="cond-toolbar">'
        f'<label class="cond-field">每次施放命中数 <input type="number" data-cond-hits min="{hit_min}" max="{hit_max}" value="{int(_num(hits.get("per_cast")) or 4)}"></label>'
        '<span class="section-note">目标等级与类型在「目标设置」中修改。</span></div>'
    )
    readonly_block = (
        '<details class="readonly-effects"><summary>不适用 / 只读效果</summary><ul>'
        + "".join(readonly)
        + "</ul></details>"
        if readonly
        else ""
    )
    return (
        f"{toolbar}{skill_settings}"
        '<div class="cond-card-grid">' + "".join(interactive) + "</div>"
        f"{readonly_block}"
        '<p class="section-note">修改后点击“计算 DPS”（需本机服务）；离线打开时只查看当前生成结果，'
        "不会伪造“生效”。</p>"
    )


def _report_buffs(
    result: dict[str, Any], capabilities: Optional[dict[str, Any]] = None
) -> str:
    return (
        f"{_condition_parameters_block(result, capabilities)}"
        '<details class="compare-block"><summary>效果对比（当前结果，按需展开）</summary>'
        f"{_sensitivity_table(result)}"
        '<p class="section-note">每个状态单独切换，不是叠加后的实战总量。</p></details>'
    )


def _hit_target_drawer(result: dict[str, Any]) -> str:
    scenario = _as_dict(result.get("scenario"))
    target = _as_dict(scenario.get("target"))
    single_target = _as_dict(_as_dict(_as_dict(result.get("single_hit")).get("totals")).get("target"))
    level = target.get("monster_level") or single_target.get("monster_level") or 100
    fire_resistance = target.get("fire_resistance")
    if fire_resistance is None:
        fire_resistance = single_target.get("fire_resistance", 0.0)
    try:
        fire_percent = round(float(fire_resistance) * 100.0, 2)
    except (TypeError, ValueError):
        fire_percent = 0.0
    boss = target.get("boss_or_rare")
    if boss is None:
        boss = single_target.get("boss_or_rare", True)
    level_text = _format_number(level, 0)
    fire_text = _format_number(fire_percent, 0)
    return f"""
<div class="drawer target-drawer" id="drawer-scenario" role="dialog" aria-modal="true"
     aria-labelledby="drawer-scenario-title" hidden>
  <div class="drawer-backdrop" data-close-drawer></div>
  <div class="drawer-panel">
    <header class="drawer-head"><div><h2 id="drawer-scenario-title">目标设置</h2>
    <p class="small muted">修改目标后点「应用」；Buff 在「Buff 设置」页配置。</p></div>
    <button type="button" class="icon-btn" data-close-drawer aria-label="关闭">关闭 ×</button></header>
    <div class="drawer-body">
      <form class="target-form" data-scenario-form novalidate>
        <div class="target-field-grid">
          <label class="target-field"><span class="target-field-label">怪物等级</span>
            <input type="number" inputmode="numeric" data-cond-level min="1" max="100" step="1"
                   value="{_esc(level_text)}" aria-label="怪物等级"></label>
          <label class="target-field"><span class="target-field-label">目标类型</span>
            <select data-cond-target aria-label="目标类型">
              <option value="boss"{" selected" if boss else ""}>Boss / 稀有敌人</option>
              <option value="normal"{" selected" if not boss else ""}>普通敌人</option>
            </select></label>
          <label class="target-field"><span class="target-field-label">火焰抗性 (%)</span>
            <input type="number" inputmode="numeric" data-cond-fire-res min="-100" max="100" step="1"
                   value="{_esc(fire_text)}" aria-label="目标火焰抗性百分比"></label>
        </div>
        <div class="target-actions">
          <button type="button" class="btn" data-reset-scenario data-reset-target>恢复默认目标</button>
          <button type="button" class="btn btn-primary" data-apply-scenario>应用</button>
        </div>
      </form>
    </div>
  </div>
</div>
"""


def _hit_report_js() -> str:
    return r"""
(function () {
  "use strict";
  var page = document.getElementById("page-buffs");
  var statusEl = page ? page.querySelector("[data-cond-status]") : null;
  function setStatus(message, state) {
    if (window.ReportActions) {
      window.ReportActions.setStatus(statusEl, message, state);
      return;
    }
    if (!statusEl) { return; }
    statusEl.textContent = message;
    if (statusEl.setAttribute) { statusEl.setAttribute("data-status-state", state || "changed"); }
  }
  function embedded(id) {
    var node = document.getElementById(id);
    if (!node) { return {}; }
    try { return JSON.parse(node.textContent || "{}"); } catch (error) { return {}; }
  }
  var result = embedded("result-data");
  var capabilities = (function () {
    var payload = embedded("report-capabilities");
    return payload.capabilities || payload || {};
  })();
  var STATE_KINDS = {};
  var STATE_BOUNDS = {};
  (capabilities.buff_controls || []).forEach(function (control) {
    var key = control.state_key || control.state_id;
    if (!key) { return; }
    STATE_KINDS[key] = String(control.kind || "toggle").toLowerCase();
    STATE_BOUNDS[key] = control;
  });
  var PROFILE_STATES = capabilities.profile_states || {};
  var TARGET_KINDS = {};
  var TARGET_BOUNDS = {};
  (capabilities.target_fields || []).forEach(function (field) {
    var key = field.field || field.state_key;
    if (!key) { return; }
    TARGET_KINDS[key] = String(field.kind || "object").toLowerCase();
    TARGET_BOUNDS[key] = field;
  });
  var SCENARIO_ALLOWED = (capabilities.scenario_fields && capabilities.scenario_fields.allowed) || [
    "baseline_profile", "target", "states", "mode_policy", "model_parameters",
    "hit_override", "single_cast", "single_hit", "dps", "profiles", "mode",
    "assumptions", "schema", "case_id", "binding"
  ];
  var SCENARIO_REQUIRED = (capabilities.scenario_fields && capabilities.scenario_fields.required) || [
    "baseline_profile", "target", "states"
  ];
  var HIT_PARAMETER = (capabilities.model_parameters || []).filter(function (item) {
    return (item.key || item.parameter) === "hits_per_cast";
  })[0] || {};
  var BINDINGS = (function () {
    var controllerBindings = null;
    try { controllerBindings = window.LeRecalc && window.LeRecalc.readBindings ? window.LeRecalc.readBindings() : null; } catch (error) { controllerBindings = null; }
    return controllerBindings;
  })();

  function isToggleKind(kind) { return kind === "toggle" || kind === "bool" || kind === "boolean"; }
  function isNumberKind(kind) {
    return kind === "stacks" || kind === "int" || kind === "integer" || kind === "number" || kind === "float";
  }
  function isObject(value) { return !!value && typeof value === "object" && !Array.isArray(value); }
  function clone(value) {
    try { return JSON.parse(JSON.stringify(value)); } catch (error) { return value; }
  }

  function deepMerge(base, patch) {
    var out = clone(isObject(base) ? base : {});
    if (!isObject(patch)) { return out; }
    Object.keys(patch).forEach(function (key) {
      if (isObject(patch[key]) && isObject(out[key]) && !Array.isArray(patch[key])) {
        out[key] = deepMerge(out[key], patch[key]);
      } else {
        out[key] = clone(patch[key]);
      }
    });
    return out;
  }

  /* ---- F1: skill hit policy vs explicit custom weights ---------------- */
  var HIT_POLICY_RAW = isObject(capabilities.hit_policy) ? capabilities.hit_policy : {};
  function policyNumber(keys, fallback) {
    for (var index = 0; index < keys.length; index += 1) {
      var value = HIT_POLICY_RAW[keys[index]];
      if (typeof value === "number" && isFinite(value)) { return value; }
    }
    return fallback;
  }
  var HIT_POLICY_HITS = isObject(HIT_POLICY_RAW.hits) ? HIT_POLICY_RAW.hits : {};
  var HIT_POLICY = {
    defaultHits: Math.round(
      policyNumber(["hits_per_cast", "default_hits", "default"],
        Number(HIT_POLICY_HITS.default) || 4)
    ),
    minHits: Math.round(
      policyNumber(["min_hits", "min"], Number(HIT_POLICY_HITS.min) || Number(HIT_PARAMETER.min) || 1)
    ),
    maxHits: Math.round(
      policyNumber(["max_hits", "max"], Number(HIT_POLICY_HITS.max) || Number(HIT_PARAMETER.max) || 8)
    ),
    firstWeight: policyNumber(["first_weight", "full_weight", "first_hit_weight"], 1.0),
    additionalWeight: policyNumber(
      ["subsequent_weight", "additional_weight", "fraction", "per_hit_fraction",
        "hit_fraction", "return_weight"], 0.5
    ),
    explicitWeights: Array.isArray(HIT_POLICY_RAW.weights) ? HIT_POLICY_RAW.weights.map(Number) : null
  };
  function policyWeights(hits) {
    if (HIT_POLICY.explicitWeights && HIT_POLICY.explicitWeights.length) {
      var base = HIT_POLICY.explicitWeights.slice(0, hits);
      while (base.length < hits) { base.push(HIT_POLICY.explicitWeights[HIT_POLICY.explicitWeights.length - 1]); }
      return base;
    }
    var weights = [];
    for (var index = 0; index < hits; index += 1) {
      weights.push(index === 0 ? HIT_POLICY.firstWeight : HIT_POLICY.additionalWeight);
    }
    return weights;
  }
  function weightsEqual(left, right) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) { return false; }
    for (var index = 0; index < left.length; index += 1) {
      if (Math.abs(Number(left[index]) - Number(right[index])) > 1e-9) { return false; }
    }
    return true;
  }

  function normalizeStateMap(value) {
    if (Array.isArray(value)) {
      var map = {};
      value.forEach(function (item) {
        if (isObject(item) && typeof item.state === "string") {
          map[item.state] = clone(item.value);
        }
      });
      return map;
    }
    return isObject(value) ? clone(value) : {};
  }
  function filterScenario(value) {
    var out = {};
    if (!isObject(value)) { return out; }
    SCENARIO_ALLOWED.forEach(function (key) {
      if (Object.prototype.hasOwnProperty.call(value, key)) { out[key] = clone(value[key]); }
    });
    if (Object.prototype.hasOwnProperty.call(out, "states")) {
      var normalizedStates = normalizeStateMap(out.states);
      var declaredStates = {};
      Object.keys(normalizedStates).forEach(function (state) {
        if (STATE_ALLOWED[state]) { declaredStates[state] = normalizedStates[state]; }
      });
      out.states = declaredStates;
    }
    if (Object.prototype.hasOwnProperty.call(out, "target")) {
      out.target = filterTarget(out.target);
    }
    return out;
  }

  var STATE_ALLOWED = {};
  Array.prototype.forEach.call(capabilities.buff_controls || [], function (control) {
    if (isObject(control) && control.state_key) { STATE_ALLOWED[control.state_key] = true; }
  });
  Array.prototype.forEach.call(capabilities.target_fields || [], function (field) {
    if (isObject(field) && field.field) { STATE_ALLOWED[field.field] = true; }
  });
  Object.keys(capabilities.profile_states || {}).forEach(function (profile) {
    Object.keys(capabilities.profile_states[profile] || {}).forEach(function (state) {
      STATE_ALLOWED[state] = true;
    });
  });

  var initialScenario = filterScenario(result.scenario);
  var initialCanonical = clone(initialScenario);
  var canonicalScenario = clone(initialScenario);
  var initialState = isObject(initialScenario.states) ? clone(initialScenario.states) : {};
  var initialBaseline = initialScenario.baseline_profile || "build_base";
  var initialTarget = isObject(initialScenario.target) ? clone(initialScenario.target) : {};
  var initialHits = (function () {
    var block = isObject(result.hits_per_cast) ? result.hits_per_cast : {};
    return parseInt(block.per_cast, 10) || HIT_POLICY.defaultHits;
  })();
  var initialResultWeights = (function () {
    var block = isObject(result.hits_per_cast) ? result.hits_per_cast : {};
    var weights = block.damage_weights;
    if (isObject(initialScenario.hit_override) && Array.isArray(initialScenario.hit_override.damage_weights)) {
      weights = initialScenario.hit_override.damage_weights;
    }
    return Array.isArray(weights) ? weights.map(Number) : [];
  })();
  var initialCustomWeights = initialResultWeights.length === initialHits &&
    !weightsEqual(initialResultWeights, policyWeights(initialHits))
    ? initialResultWeights.slice()
    : null;
  if (!initialCustomWeights && Array.isArray(initialScenario.hit_override && initialScenario.hit_override.damage_weights)) {
    var initialOverrideWeights = initialScenario.hit_override.damage_weights.map(Number);
    if (initialOverrideWeights.length === initialHits &&
        !weightsEqual(initialOverrideWeights, policyWeights(initialHits))) {
      initialCustomWeights = initialOverrideWeights.slice();
    }
  }
  var customBaseWeights = initialCustomWeights ? initialCustomWeights.slice() : null;
  var GENERATION_KEY = "le-hit-generation-state";
  try {
    var generationRaw = window.sessionStorage.getItem(GENERATION_KEY);
    if (generationRaw) {
      var generation = JSON.parse(generationRaw);
      if (isObject(generation) && isObject(generation.scenario)) {
        initialCanonical = filterScenario(generation.scenario);
        initialScenario = clone(initialCanonical);
        canonicalScenario = clone(initialCanonical);
        initialState = normalizeStateMap(initialScenario.states || {});
        initialBaseline = initialScenario.baseline_profile || "build_base";
        initialTarget = isObject(initialScenario.target) ? clone(initialScenario.target) : {};
        initialHits = parseInt(generation.hits, 10) || initialHits;
        initialCustomWeights = Array.isArray(generation.custom_weights) && generation.custom_weights.length
          ? generation.custom_weights.map(Number)
          : null;
        customBaseWeights = initialCustomWeights ? initialCustomWeights.slice() : null;
      }
    } else {
      window.sessionStorage.setItem(GENERATION_KEY, JSON.stringify({
        scenario: initialCanonical,
        custom_weights: initialCustomWeights,
        hits: initialHits
      }));
    }
    var CURRENT_CANONICAL_KEY = "le-hit-canonical-scenario";
    var storedCanonicalRaw = window.sessionStorage.getItem(CURRENT_CANONICAL_KEY);
    if (storedCanonicalRaw) {
      var storedCanonical = JSON.parse(storedCanonicalRaw);
      if (isObject(storedCanonical)) {
        // The pre-compute canonical already carries the exact user/applied
        // scenario (including non-control states and hidden sections); keep it
        // authoritative over the service result's normalized echo.
        canonicalScenario = deepMerge(canonicalScenario, filterScenario(storedCanonical));
      }
    }
    var persistCanonical = function () {
      try {
        window.sessionStorage.setItem(CURRENT_CANONICAL_KEY, JSON.stringify(canonicalScenario));
      } catch (error) { /* optional */ }
    };
    var storedCustom = window.sessionStorage.getItem("le-hit-custom-weights");
    if (storedCustom && !initialCustomWeights) {
      var parsedCustom = JSON.parse(storedCustom);
      if (Array.isArray(parsedCustom) && parsedCustom.length) {
        customBaseWeights = parsedCustom.map(Number);
      }
    }
  } catch (error) { /* optional */ }

  var initialLevel = Number(initialTarget.monster_level || 100);
  var initialFireResistance = typeof initialTarget.fire_resistance === "number" ? initialTarget.fire_resistance : 0.0;
  var initialBoss = initialTarget.boss_or_rare !== false;
  var initialFireResistance = typeof initialTarget.fire_resistance === "number" ? initialTarget.fire_resistance : 0.0;
  var initialBoss = initialTarget.boss_or_rare !== false;

  function stateElement(state) {
    var controls = page ? page.querySelectorAll("[data-cond][data-state]") : [];
    for (var index = 0; index < controls.length; index += 1) {
      if (controls[index].getAttribute("data-state") === state) { return controls[index]; }
    }
    return null;
  }
  function readControlState(el) {
    var state = el.getAttribute("data-state");
    var kind = STATE_KINDS[state] || (el.type === "checkbox" ? "toggle" : "stacks");
    if (isToggleKind(kind)) {
      return el.type === "checkbox" ? !!el.checked : !!parseInt(el.value, 10);
    }
    if (isNumberKind(kind)) {
      var number = parseInt(el.value, 10);
      return isNaN(number) ? 0 : number;
    }
    return el.type === "checkbox" ? !!el.checked : (parseInt(el.value, 10) || 0);
  }
  function writeControlState(el, value) {
    var state = el.getAttribute("data-state");
    var kind = STATE_KINDS[state] || (el.type === "checkbox" ? "toggle" : "stacks");
    if (el.type === "checkbox") { el.checked = isToggleKind(kind) ? !!value : !!Number(value); return; }
    if (el.type === "number") {
      var number = isNumberKind(kind) ? parseInt(value, 10) : Number(value);
      el.value = String(isNaN(number) ? 0 : number);
    }
  }
  function targetValue(selector, fallback) {
    var el = document.querySelector(selector);
    if (!el) { return fallback; }
    if (el.type === "checkbox") { return !!el.checked; }
    if (selector === "[data-cond-fire-res]" || selector === "[data-cond-level]") {
      var number = parseFloat(el.value);
      return isNaN(number) ? fallback : number;
    }
    return el.value;
  }
  function readTarget() {
    var bossSelect = document.querySelector("[data-cond-target]");
    var level = targetValue("[data-cond-level]", initialLevel);
    var firePercent = targetValue("[data-cond-fire-res]", initialFireResistance * 100.0);
    return {
      boss_or_rare: bossSelect ? bossSelect.value !== "normal" : initialBoss,
      monster_level: Math.round(level),
      fire_resistance: Math.round(firePercent) / 100.0
    };
  }

  function weightsForHits(hits) {
    if (customBaseWeights && customBaseWeights.length) {
      var weights = customBaseWeights.slice();
      if (hits < weights.length) { return weights.slice(0, hits); }
      while (weights.length < hits) { weights.push(HIT_POLICY.additionalWeight); }
      return weights;
    }
    return policyWeights(hits);
  }
  function persistCustomWeights() {
    try {
      if (customBaseWeights && customBaseWeights.length) {
        window.sessionStorage.setItem("le-hit-custom-weights", JSON.stringify(customBaseWeights));
      } else {
        window.sessionStorage.removeItem("le-hit-custom-weights");
      }
    } catch (error) { /* optional */ }
  }
  function updateWeightsFromScenario(scenario) {
    var override = isObject(scenario && scenario.hit_override) ? scenario.hit_override : {};
    var hits = parseInt(override.hits_per_cast, 10);
    if (!hits || hits <= 0) { return; }
    if (Array.isArray(override.damage_weights) && override.damage_weights.length === hits) {
      var weights = override.damage_weights.map(Number);
      customBaseWeights = weightsEqual(weights, policyWeights(hits)) ? null : weights.slice();
      persistCustomWeights();
    }
  }
  function filterTarget(target) {
    var out = {};
    if (!isObject(target)) { return out; }
    Object.keys(target).forEach(function (key) {
      if (TARGET_BOUNDS[key] || STATE_BOUNDS[key] || key === "boss_or_rare" ||
          key === "monster_level" || key === "fire_resistance") {
        out[key] = clone(target[key]);
      }
    });
    return out;
  }

  function readScenario() {
    // F2: start from the complete canonical scenario, then overlay only the
    // fields this editor owns; hidden/declared sections stay intact.
    var scenario = clone(canonicalScenario);
    var states = isObject(scenario.states) ? clone(scenario.states) : {};
    Array.prototype.forEach.call(page.querySelectorAll("[data-cond][data-state]"), function (el) {
      states[el.getAttribute("data-state")] = readControlState(el);
    });
    scenario.states = states;
    var preset = page.querySelector("[data-cond-preset]");
    scenario.baseline_profile = preset ? preset.value : initialBaseline;
    scenario.target = filterTarget(deepMerge(isObject(scenario.target) ? scenario.target : {}, readTarget()));
    var hitsInput = page.querySelector("[data-cond-hits]") || document.querySelector("[data-cond-hits]");
    var hits = parseInt(hitsInput && hitsInput.value, 10);
    if (!hits || hits <= 0) { hits = initialHits; }
    scenario.hit_override = { hits_per_cast: hits, damage_weights: weightsForHits(hits) };
    scenario.schema = scenario.schema || "le-scenario-v1";
    canonicalScenario = deepMerge(canonicalScenario, scenario);
    try {
      window.sessionStorage.setItem("le-hit-canonical-scenario", JSON.stringify(canonicalScenario));
    } catch (error) { /* optional */ }
    persistCustomWeights();
    return scenario;
  }

  function applyScenario(scenario) {
    if (!isObject(scenario)) { return false; }
    canonicalScenario = filterScenario(deepMerge(canonicalScenario, scenario));
    try {
      window.sessionStorage.setItem("le-hit-canonical-scenario", JSON.stringify(canonicalScenario));
    } catch (error) { /* optional */ }
    var merged = canonicalScenario;
    var states = isObject(merged.states) ? merged.states : {};
    Array.prototype.forEach.call(page.querySelectorAll("[data-cond][data-state]"), function (el) {
      var state = el.getAttribute("data-state");
      if (Object.prototype.hasOwnProperty.call(states, state)) { writeControlState(el, states[state]); }
    });
    var preset = page.querySelector("[data-cond-preset]");
    if (preset && merged.baseline_profile) { preset.value = merged.baseline_profile; }
    var hitsInput = page.querySelector("[data-cond-hits]") || document.querySelector("[data-cond-hits]");
    var override = isObject(merged.hit_override) ? merged.hit_override : {};
    if (hitsInput && override.hits_per_cast) { hitsInput.value = String(override.hits_per_cast); }
    updateWeightsFromScenario(merged);
    var target = isObject(merged.target) ? merged.target : {};
    var levelInput = document.querySelector("[data-cond-level]");
    var targetSelect = document.querySelector("[data-cond-target]");
    var fireInput = document.querySelector("[data-cond-fire-res]");
    if (levelInput && target.monster_level !== undefined) { levelInput.value = String(target.monster_level); }
    if (targetSelect && target.boss_or_rare !== undefined) {
      targetSelect.value = target.boss_or_rare ? "boss" : "normal";
    }
    if (fireInput && typeof target.fire_resistance === "number") {
      fireInput.value = String(Math.round(target.fire_resistance * 100.0));
    }
    return true;
  }

  var controller = null;
  if (window.LeRecalc && page) {
    controller = window.LeRecalc.createController({
      getScenario: readScenario,
      onStatus: function (message, tone, stateHint) {
        var state = stateHint || "changed";
        if (!stateHint) {
          if (String(message).indexOf("计算中") === 0) { state = "busy"; }
          else if (String(message).indexOf("已应用") === 0) { state = "applied"; }
          else if (tone === "error") { state = "error"; }
        }
        setStatus(message, state);
      },
      onBusy: function (busy) {
        var button = page.querySelector("[data-cond-compute]");
        if (button) { button.disabled = !!busy; }
        if (busy) { setStatus("计算中…", "busy"); }
      },
      onAccepted: function (payload) {
        try { window.sessionStorage.setItem("le-recalc-applied", "1"); } catch (error) { /* optional */ }
        if (payload && payload.html) {
          document.open();
          document.write(payload.html);
          document.close();
        }
      },
      onPrepared: function () { setStatus("已应用（配置已准备，未变更当前结果）。", "applied"); }
    });
  }

  function markEdit(message) {
    if (controller) { controller.markEdit(); }
    setStatus(message || "已更改，点击“计算 DPS”应用。", "changed");
  }

  // Load-time zero requests: the shared client restores the applied view state.
  if (window.__leRecalcRestoredApplied) {
    setStatus("已应用（本次计算结果）。", "applied");
  } else {
    setStatus("已应用（生成时场景）。", "applied");
  }

  /* ------------------------------------------------------------------ */
  /* Report config (save/load) — le-report-config-v1, atomic validation  */
  /* ------------------------------------------------------------------ */
  function bindingSnapshot() {
    return controller && controller.bindings ? controller.bindings() : BINDINGS;
  }
  function configError(message, code) { return { ok: false, error: message, code: code || "config_invalid" }; }
  function validateScenario(scenario) {
    if (!isObject(scenario)) { return configError("缺少场景对象"); }
    for (var key in scenario) {
      if (Object.prototype.hasOwnProperty.call(scenario, key) && SCENARIO_ALLOWED.indexOf(key) === -1) {
        return configError("未知场景字段 " + key);
      }
    }
    for (var requiredIndex = 0; requiredIndex < SCENARIO_REQUIRED.length; requiredIndex += 1) {
      var required = SCENARIO_REQUIRED[requiredIndex];
      if (!Object.prototype.hasOwnProperty.call(scenario, required)) { return configError("缺少场景字段 " + required); }
    }
    if (typeof scenario.baseline_profile !== "string" ||
        (capabilities.baseline_profiles || []).indexOf(scenario.baseline_profile) === -1) {
      return configError("基线口径不在能力声明内");
    }
    var states = scenario.states;
    if (!isObject(states)) { return configError("states 必须是对象"); }
    for (var state in states) {
      if (!Object.prototype.hasOwnProperty.call(states, state)) { continue; }
      if (state === "baseline_profile") { continue; }
      var declaration = STATE_BOUNDS[state];
      if (!declaration && TARGET_BOUNDS[state]) { declaration = TARGET_BOUNDS[state]; }
      if (!declaration) { return configError("未知状态 " + state); }
      var kind = String(declaration.kind || "toggle").toLowerCase();
      var value = states[state];
      if (isToggleKind(kind)) {
        if (typeof value !== "boolean") { return configError("状态 " + state + " 必须是布尔值"); }
      } else if (isNumberKind(kind)) {
        var number = Number(value);
        if (!isFinite(number) || number !== Math.floor(number)) { return configError("状态 " + state + " 必须是整数"); }
        if (declaration.min !== undefined && number < Number(declaration.min)) { return configError("状态 " + state + " 低于下限"); }
        if (declaration.max !== undefined && number > Number(declaration.max)) { return configError("状态 " + state + " 超出上限"); }
      }
    }
    var target = scenario.target;
    if (!isObject(target)) { return configError("target 必须是对象"); }
    for (var field in target) {
      if (!Object.prototype.hasOwnProperty.call(target, field)) { continue; }
      var targetDeclaration = TARGET_BOUNDS[field] || STATE_BOUNDS[field];
      if (!targetDeclaration) { return configError("未知目标字段 " + field); }
      var targetKind = String(targetDeclaration.kind || "object").toLowerCase();
      if (isToggleKind(targetKind) && typeof target[field] !== "boolean") {
        return configError("目标字段 " + field + " 必须是布尔值");
      }
      if (isNumberKind(targetKind) && !isFinite(Number(target[field]))) {
        return configError("目标字段 " + field + " 必须是数值");
      }
    }
    var override = scenario.hit_override;
    if (override !== undefined) {
      if (!isObject(override)) { return configError("hit_override 必须是对象"); }
      for (var overrideKey in override) {
        if (["hits_per_cast", "damage_weights"].indexOf(overrideKey) === -1) {
          return configError("未知 hit_override 字段 " + overrideKey);
        }
      }
      var hits = Number(override.hits_per_cast);
      if (!isFinite(hits) || hits !== Math.floor(hits) || hits < 1) { return configError("命中次数必须是正整数"); }
      if (HIT_PARAMETER.min !== undefined && hits < Number(HIT_PARAMETER.min)) { return configError("命中次数低于能力下限"); }
      if (HIT_PARAMETER.max !== undefined && hits > Number(HIT_PARAMETER.max)) { return configError("命中次数超出能力上限"); }
      if (!Array.isArray(override.damage_weights) || override.damage_weights.length !== hits) {
        return configError("命中权重数量必须与命中次数一致");
      }
      for (var weightIndex = 0; weightIndex < override.damage_weights.length; weightIndex += 1) {
        var weight = Number(override.damage_weights[weightIndex]);
        if (!isFinite(weight) || weight < 0) { return configError("命中权重必须是非负数值"); }
      }
    }
    return { ok: true, scenario: scenario };
  }
  function validateConfig(parsed) {
    if (!isObject(parsed)) { return configError("配置必须是 JSON 对象"); }
    if (!(capabilities.scenario_fields || (capabilities.buff_controls || []).length || (capabilities.target_fields || []).length)) {
      return configError("页面缺少能力描述，无法校验配置");
    }
    if (window.ReportActions) {
      var envelope = window.ReportActions.validateEnvelope(parsed, {
        currentBinding: bindingSnapshot(),
        capabilities: capabilities
      });
      if (!envelope.ok) {
        return configError(envelope.error, envelope.code);
      }
      return { ok: true, scenario: envelope.scenario, warnings: envelope.warnings || [] };
    }
    var known = ["schema_version", "schema", "binding", "scenario"];
    for (var key in parsed) {
      if (Object.prototype.hasOwnProperty.call(parsed, key) && known.indexOf(key) === -1) {
        return configError("未知配置字段 " + key);
      }
    }
    var schemaVersion = parsed.schema_version;
    if (schemaVersion === undefined && parsed.schema !== undefined) {
      schemaVersion = parsed.schema; // legacy alias accepted with a warning
    }
    if (schemaVersion !== "le-report-config-v1") {
      return configError("schema_version 必须是 le-report-config-v1");
    }
    var bindingProblem = validateBinding(parsed.binding);
    if (bindingProblem) { return configError(bindingProblem); }
    return validateScenario(parsed.scenario);
  }
  function validateBinding(binding) {
    var current = bindingSnapshot();
    if (!isObject(current) || current.error) { return "页面绑定不可用"; }
    if (!isObject(binding)) { return "配置缺少绑定信息（旧格式不支持）"; }
    var known = ["schema_version", "case_id", "model_id", "capabilities_version", "ui_contract_version", "renderer_fingerprint", "report_ref", "input_fingerprints", "recompute_endpoint"];
    for (var key in binding) {
      if (Object.prototype.hasOwnProperty.call(binding, key) && known.indexOf(key) === -1) {
        return "配置绑定含未声明字段 " + key;
      }
    }
    var required = ["case_id", "model_id", "capabilities_version", "input_fingerprints", "report_ref"];
    for (var index = 0; index < required.length; index += 1) {
      if (!Object.prototype.hasOwnProperty.call(binding, required[index])) {
        return "配置绑定缺少字段 " + required[index];
      }
    }
    if (binding.case_id !== current.case_id) { return "配置案例与当前页面不一致"; }
    if (binding.model_id !== current.model_id) { return "配置模型与当前页面不一致"; }
    if (binding.capabilities_version !== current.capabilities_version) { return "配置能力版本与当前页面不一致"; }
    if (binding.ui_contract_version !== undefined && binding.ui_contract_version !== current.ui_contract_version) {
      return "配置 UI 契约版本与当前页面不一致";
    }
    if (binding.renderer_fingerprint !== undefined && binding.renderer_fingerprint !== current.renderer_fingerprint) {
      return "配置渲染器指纹与当前页面不一致";
    }
    var currentFingerprints = current.input_fingerprints || {};
    var fingerprints = binding.input_fingerprints || {};
    var fingerprintKeys = ["calc_input", "ruleset", "snapshot", "dataset"];
    for (var first = 0; first < fingerprintKeys.length; first += 1) {
      if (fingerprints[fingerprintKeys[first]] !== currentFingerprints[fingerprintKeys[first]]) {
        return "配置输入指纹与当前页面不一致（" + fingerprintKeys[first] + "）";
      }
    }
    var currentRef = current.report_ref || {};
    var configRef = binding.report_ref || {};
    var refKeys = ["case_id", "model_id", "run_id", "report_path", "ui_contract_version", "renderer_fingerprint"];
    for (var second = 0; second < refKeys.length; second += 1) {
      if (configRef[refKeys[second]] !== currentRef[refKeys[second]]) {
        return "配置报告标识与当前页面不一致（" + refKeys[second] + "）";
      }
    }
    return "";
  }
  function buildConfig() {
    return {
      schema_version: "le-report-config-v1",
      binding: bindingSnapshot(),
      scenario: readScenario()
    };
  }

  var preset = page.querySelector("[data-cond-preset]");
  if (preset) {
    preset.addEventListener("change", function () {
      var profileStates = PROFILE_STATES[preset.value];
      if (isObject(profileStates)) {
        Array.prototype.forEach.call(page.querySelectorAll("[data-cond][data-state]"), function (el) {
          var state = el.getAttribute("data-state");
          if (Object.prototype.hasOwnProperty.call(profileStates, state)) {
            writeControlState(el, profileStates[state]);
          }
        });
      }
      markEdit("已切换预设，点击“计算 DPS”应用。");
    });
  }
  page.addEventListener("change", function () { markEdit("已更改，点击“计算 DPS”应用。"); });
  page.addEventListener("input", function () { markEdit("已更改，点击“计算 DPS”应用。"); });

  var targetInputs = [
    document.querySelector("[data-cond-level]"),
    document.querySelector("[data-cond-target]"),
    document.querySelector("[data-cond-fire-res]")
  ];
  var hitsControl = document.querySelector("[data-cond-hits]");
  if (hitsControl) {
    hitsControl.addEventListener("input", function () { markEdit("已更改命中次数，点击“计算 DPS”应用。"); });
    hitsControl.addEventListener("change", function () { markEdit("已更改命中次数，点击“计算 DPS”应用。"); });
  }
  targetInputs.forEach(function (input) {
    if (!input) { return; }
    input.addEventListener("input", function () { markEdit("目标已更改，点击“计算 DPS”应用。"); });
    input.addEventListener("change", function () { markEdit("目标已更改，点击“计算 DPS”应用。"); });
  });

  var compute = page.querySelector("[data-cond-compute]");
  if (compute) {
    if (!controller) {
      compute.disabled = true;
      setStatus("页面缺少共享重算客户端，无法计算。", "error");
    } else {
      compute.addEventListener("click", function () { controller.compute("compute"); });
    }
  }
  var save = page.querySelector("[data-cond-save]");
  if (save) {
    save.addEventListener("click", function () {
      var config = buildConfig();
      var validation = validateConfig(config);
      if (!validation.ok) { setStatus("保存失败：" + validation.error, "error"); return; }
      var payloadText = JSON.stringify(config, null, 2);
      var link = null;
      try {
        link = document.createElement("a");
        link.href = "data:application/json;charset=utf-8," + encodeURIComponent(payloadText);
        link.download = "flame-reave-scenario.json";
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
      } catch (error) { setStatus("保存失败：" + error.message, "error"); return; }
      setStatus("配置已下载（保存后仍需点击计算才应用）。", "changed");
    });
  }
  var load = page.querySelector("[data-cond-load]");
  if (load) {
    load.addEventListener("change", function () {
      var file = load.files && load.files[0];
      if (!file) { return; }
      var reader = new FileReader();
      reader.onload = function () {
        var parsed = null;
        try { parsed = JSON.parse(String(reader.result || "{}")); } catch (error) {
          setStatus("载入失败：" + error.message, "error");
          return;
        }
        var validation = validateConfig(parsed);
        if (!validation.ok) {
          setStatus("载入失败：" + validation.error + "（未修改当前配置）", "error");
          return;
        }
        applyScenario(validation.scenario);
        markEdit("配置已载入，点击“计算 DPS”应用。");
      };
      reader.readAsText(file);
    });
  }
  var reset = page.querySelector("[data-cond-reset]");
  if (reset) {
    reset.addEventListener("click", function () {
      canonicalScenario = clone(initialCanonical);
      customBaseWeights = initialCustomWeights ? initialCustomWeights.slice() : null;
      try {
        window.sessionStorage.setItem("le-hit-canonical-scenario", JSON.stringify(canonicalScenario));
      } catch (error) { /* optional */ }
      persistCustomWeights();
      applyScenario(clone(initialCanonical));
      var hitsInput = page.querySelector("[data-cond-hits]") || document.querySelector("[data-cond-hits]");
      if (hitsInput) { hitsInput.value = String(initialHits); }
      markEdit("Buff 已重置为生成时的配置。");
    });
  }
  var applyTarget = document.querySelector("[data-apply-scenario]");
  if (applyTarget) {
    applyTarget.addEventListener("click", function () {
      var drawer = document.getElementById("drawer-scenario");
      if (drawer) { drawer.hidden = true; }
      markEdit("目标已更新，点击“计算 DPS”应用。");
    });
  }
  var resetTarget = document.querySelector("[data-reset-target]");
  if (resetTarget) {
    resetTarget.addEventListener("click", function () {
      var levelInput = document.querySelector("[data-cond-level]");
      var targetSelect = document.querySelector("[data-cond-target]");
      var fireInput = document.querySelector("[data-cond-fire-res]");
      if (levelInput) { levelInput.value = String(initialLevel); }
      if (targetSelect) { targetSelect.value = initialBoss ? "boss" : "normal"; }
      if (fireInput) { fireInput.value = String(Math.round(initialFireResistance * 100.0)); }
      markEdit("目标已恢复为生成时的配置。");
    });
  }
  if (controller && controller.bindings && controller.bindings().error) {
    setStatus(controller.bindings().error, "error");
  }
})();
"""


def render_hit_html(
    result: dict[str, Any],
    *,
    capabilities: dict | None = None,
    recalc_endpoint: str | None = None,
    bindings: dict | None = None,
    recalc_bindings: dict | None = None,
) -> str:
    """Offline player-facing single-file report rendered through the shared shell.

    The shared document layer owns the page skeleton/nav/footer/source panel/
    drawers/embedded data; only the hit-model result pages and chart/result
    components live here.  ``capabilities``/``recalc_endpoint`` are injected by
    the service adapter (P3R-B); offline reports leave the endpoint placeholder
    so the page can state "本机重算未启用" instead of hardcoding a port.
    """

    skill = _as_dict(result.get("skill"))
    scenario = _as_dict(result.get("scenario"))
    target = _as_dict(_as_dict(_as_dict(result.get("single_hit")).get("totals")).get("target"))
    skill_name = _text(skill.get("name")) or "Flame Reave"
    pages = [
        ("overview", "总览", "本次模拟的关键结果与基线口径"),
        ("composition", "伤害构成", "单次命中的乘区与来源构成"),
        ("dps", "命中与 DPS", "命中、攻速与冷却决定的每秒伤害"),
        ("sources", "增伤来源", "按来源与状态列出的增伤构成"),
        ("buffs", "Buff 设置", "目标条件、Buff 与条件预设"),
    ]
    panels = {
        "overview": _report_overview(result),
        "composition": _report_composition(result),
        "dps": _report_dps(result),
        "sources": _report_sources(result),
        "buffs": _report_buffs(result, capabilities),
    }
    page_html = {
        page_id: _ui_components.page(page_id, label, question, panels[page_id])
        for page_id, label, question in pages
    }
    level = target.get("monster_level")
    level_text = _format_number(level, 0) if level is not None else "100"
    fire = target.get("fire_resistance")
    fire_text = _pct_or_number(fire) if fire is not None else "0%"
    dr = target.get("monster_damage_reduction")
    dr_text = _pct_or_number(dr) if dr is not None else "—"
    header_summary = (
        "目标 <b>Lv" + _esc(level_text) + "</b> · 火抗 <b>" + _esc(fire_text) + "</b> · DR <b>" + _esc(dr_text) + "</b>"
    )
    class_text = " / ".join(part for part in (skill.get("class"), skill.get("mastery")) if part)
    subtitle = (class_text + " · " if class_text else "") + "预计伤害（本体命中）"
    display_view = {
        "model": "hit",
        "context": {
            "status": _text(_as_dict(result.get("run")).get("status")) or "modeled",
            "status_label": "结果已计入",
            "character": {"class": skill.get("class"), "mastery": skill.get("mastery")},
            "scenario": {
                "level": level,
                "target_resistance": fire,
                "target_resistance_label": "火焰抗性",
                "baseline_profile": _text(scenario.get("baseline_profile")) or "build_base",
                "baseline_profile_label": _BASELINE_LABELS.get(
                    _text(scenario.get("baseline_profile")), "完整构筑基线（常驻效果生效）"
                ),
                "conditional_applied": bool(_as_dict(result.get("buffs")).get("applied")),
            },
        },
        "pages": [{"id": page_id, "label": label, "question": question} for page_id, label, question in pages],
    }
    default_capabilities = {
        "model": "hit",
        "ui_contract_version": _ui_document.UI_CONTRACT_VERSION,
        "pages": [{"id": page_id, "label": label} for page_id, label, _question in pages],
        "baseline_profiles": [
            {"id": key, "label": label} for key, label in _BASELINE_LABELS.items()
        ],
        "target_fields": ["monster_level", "boss_or_rare"],
        "buff_controls": ["states", "hit_override", "baseline_profile"],
        "recalc": {"supported": True, "endpoint_injected": bool(recalc_endpoint)},
    }
    spec = _ui_document.DocumentSpec(
        model="hit",
        title=f"{skill_name} 伤害分析",
        brand_mark="FR",
        heading=f"{skill_name} 伤害分析",
        subtitle=subtitle,
        result=_display_safe(result),
        view=_display_safe(display_view),
        pages=[{"id": page_id, "label": label} for page_id, label, _question in pages],
        page_html=page_html,
        first_page="overview",
        header_summary_html=header_summary,
        target_button=True,
        number_toggle=False,
        drawers_html=_hit_target_drawer(result),
        extra_css=_FR_CSS_OVERRIDES,
        model_scripts=("shell.js",),
        inline_scripts=(_hit_report_js(),),
        capabilities=capabilities or default_capabilities,
        recalc_endpoint=recalc_endpoint,
        bindings=bindings if bindings is not None else recalc_bindings,
        status_label="",
        body_attrs_extra=' data-mode="single"',
    )
    return _ui_document.render_document(spec)
