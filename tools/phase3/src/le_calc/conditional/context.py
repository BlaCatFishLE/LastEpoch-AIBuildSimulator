"""Build the ``result["conditional"]`` context (contract §8.1 / §11).

Entry point consumed by the Lead-owned ``report.load_and_build`` integration and
by the future ``le_calc conditions`` CLI:

    from le_calc.conditional import build_context, write_artifacts

    context = build_context(
        snapshot,
        scenario=None,            # DPS result scenario (used for auto target states)
        ruleset=None,
        ruleset_path=None,
        calc_input_path=None,
        build_path=None,
        registry_path=None,
        scenario_path=None,
        preset_id=None,
        repo_root=None,
    )

``build_context`` **never raises**.  Missing registry/calc input/ruleset render a
degraded page with readable ``issues`` and ``status`` set to
``degraded``/``unavailable``; the DPS run itself is never blocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from . import constants as C
from .conditions import build_client_rules
from .discover import build_catalog
from .evaluate import evaluate_scenario
from .registry import (
    DEFAULT_REGISTRY_PATH,
    PROJECT_ROOT,
    Registry,
    RegistryError,
    load_registry,
    validate_registry,
)
from .scenario import (
    ScenarioError,
    apply_preset,
    default_scenario,
    load_scenario,
    normalize_scenario,
    save_scenario,
    scenario_fingerprint,
    target_from_context,
    validate_scenario,
)
from .view import build_view

DEFAULT_RULESET_PATH = PROJECT_ROOT / "tools" / "phase3" / "rules" / "ruleset-bh-stage1-v4.json"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _add_issue(
    issues: list[dict[str, Any]],
    severity: str,
    code: str,
    message: str,
    *,
    area: str | None = None,
) -> None:
    issue: dict[str, Any] = {"severity": severity, "code": code, "message": message}
    if area:
        issue["area"] = area
    issues.append(issue)


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def _coerce_snapshot(snapshot: Any) -> Any:
    if snapshot is None:
        raise ValueError("snapshot is required")
    if hasattr(snapshot, "build_meta") and hasattr(snapshot, "contributions"):
        return snapshot
    if isinstance(snapshot, dict):
        from .. import model

        return model.Snapshot.from_dict(snapshot)
    raise ValueError(f"unsupported snapshot type: {type(snapshot).__name__}")


def _resolve(
    path: str | Path | None,
    repo: Path,
    *,
    fallback_root: Path | None = None,
) -> Optional[Path]:
    if path is None:
        return None
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    primary = repo / candidate
    if primary.exists():
        return primary
    if fallback_root is not None:
        secondary = Path(fallback_root) / candidate
        if secondary.exists():
            return secondary
    return primary


def _rel(path: str | Path | None, repo: Path) -> Optional[str]:
    return C.repo_relative(path, repo)


def _file_sha256(path: Path) -> Optional[str]:
    try:
        return C.sha256_file(path)
    except OSError:  # pragma: no cover - defensive
        return None


def _display_path(path: str | Path | None, repo: Path) -> str:
    """Repo-relative display path (never leaks an absolute host path)."""
    if path is None:
        return "（未配置路径）"
    relative = C.repo_relative(path, repo)
    return relative or Path(path).name


def _sanitize_issues(issues: list[dict[str, Any]], roots: tuple[Path, ...]) -> list[dict[str, Any]]:
    """Strip absolute workspace prefixes from issue messages (view is path-free)."""
    cleaned: list[dict[str, Any]] = []
    for issue in issues:
        entry = dict(issue)
        message = str(entry.get("message") or "")
        for root in roots:
            for token in (str(root) + "\\", str(root) + "/", str(root)):
                message = message.replace(token, "")
        entry["message"] = message.strip()
        cleaned.append(entry)
    return cleaned


# ---------------------------------------------------------------------------
# Input / ruleset / catalog loading
# ---------------------------------------------------------------------------


def _snapshot_build_meta(snapshot: Any) -> dict[str, Any]:
    meta = getattr(snapshot, "build_meta", None)
    return dict(meta) if isinstance(meta, dict) else {}


def _build_input_block(
    snapshot: Any,
    *,
    repo: Path,
    fallback_root: Path,
    calc_path: Optional[Path],
    calc_input: Optional[dict[str, Any]],
    calc_actual_sha: Optional[str],
    build_path: Optional[Path],
    build_data: Optional[dict[str, Any]],
    build_actual_sha: Optional[str],
    registry: Optional[Registry],
    registry_path: Optional[Path],
    ruleset: Any,
    ruleset_path: Optional[Path],
) -> dict[str, Any]:
    meta = _snapshot_build_meta(snapshot)
    build_meta = meta.get("build") if isinstance(meta.get("build"), dict) else {}
    calc_meta = meta.get("calc_input") if isinstance(meta.get("calc_input"), dict) else {}
    dataset_meta = meta.get("dataset") if isinstance(meta.get("dataset"), dict) else {}
    files: list[dict[str, Any]] = []
    if calc_path is not None:
        files.append(
            {
                "role": "calc_input",
                "path": _rel(calc_path, repo),
                "sha256": calc_actual_sha or calc_meta.get("sha256"),
                "expected_sha256": calc_meta.get("sha256"),
                "exists": calc_path.is_file(),
            }
        )
    if registry_path is not None:
        files.append(
            {
                "role": "registry",
                "path": _rel(registry_path, repo),
                "sha256": registry.sha256 if registry else None,
                "expected_sha256": None,
                "exists": registry_path.is_file(),
            }
        )
    if ruleset_path is not None:
        files.append(
            {
                "role": "ruleset",
                "path": _rel(ruleset_path, repo),
                "sha256": getattr(ruleset, "sha256", None),
                "expected_sha256": None,
                "exists": ruleset_path.is_file(),
            }
        )
    if build_path is not None:
        files.append(
            {
                "role": "build",
                "path": _rel(build_path, repo),
                "sha256": build_actual_sha,
                "expected_sha256": None,
                "exists": build_path.is_file(),
            }
        )
    fingerprints = {
        "build_spec_sha256": meta.get("build_spec_sha256"),
        "build_content_sha256": meta.get("content_sha256") or build_meta.get("content_sha256"),
        "calc_input_sha256": calc_actual_sha or meta.get("calc_input_sha256"),
        "database_sha256": meta.get("database_sha256"),
        "dataset_logical_content_sha256": meta.get("dataset_logical_content_sha256"),
        "registry_sha256": registry.sha256 if registry else None,
        "ruleset_sha256": getattr(ruleset, "sha256", None),
    }
    return {
        "build": {
            "build_id": build_meta.get("build_id"),
            "revision_id": build_meta.get("revision_id"),
            "content_sha256": build_meta.get("content_sha256") or meta.get("content_sha256"),
            "revision_seq": build_meta.get("revision_seq"),
        },
        "calc_input": {
            "path": _rel(calc_path, repo) if calc_path is not None else calc_meta.get("path"),
            "sha256": calc_actual_sha or calc_meta.get("sha256"),
            "expected_sha256": calc_meta.get("sha256"),
            "exists": bool(calc_input is not None),
            "schema_version": (calc_input or {}).get("schema_version") if calc_input else None,
            "mod_count": len((calc_input or {}).get("mods") or []) if calc_input else None,
            "unresolved_inputs": len((calc_input or {}).get("unresolved_inputs") or []) if calc_input else None,
        },
        "dataset": {
            "dataset_id": dataset_meta.get("dataset_id"),
            "target_game_version": dataset_meta.get("target_game_version"),
            "file_sha256": dataset_meta.get("file_sha256"),
            "logical_content_sha256": dataset_meta.get("logical_content_sha256"),
        },
        "ruleset": {
            "id": getattr(ruleset, "ruleset_id", None),
            "sha256": getattr(ruleset, "sha256", None),
            "path": _rel(ruleset_path, repo) if ruleset_path is not None else None,
        },
        "game_version": meta.get("game_version")
        or dataset_meta.get("target_game_version")
        or (registry.game_version if registry else None),
        "fingerprints": fingerprints,
        "files": files,
    }


def _load_ruleset(
    ruleset: Any,
    ruleset_path: str | Path | None,
    *,
    repo: Path,
    fallback_root: Path,
    issues: list[dict[str, Any]],
) -> tuple[Any, Optional[Path]]:
    if ruleset is not None and hasattr(ruleset, "ruleset_id"):
        path = getattr(ruleset, "path", None)
        resolved = _resolve(path, repo, fallback_root=fallback_root) if path else (
            _resolve(ruleset_path, repo, fallback_root=fallback_root)
            if ruleset_path
            else None
        )
        return ruleset, resolved
    candidate = _resolve(
        ruleset_path if ruleset_path is not None else C.DEFAULT_RULESET_RELPATH,
        repo,
        fallback_root=fallback_root,
    )
    try:
        from ..ruleset import load_ruleset

        loaded = load_ruleset(candidate)
        return loaded, candidate
    except Exception as exc:  # noqa: BLE001 - degrade, never crash the DPS run
        _add_issue(
            issues,
            "warning",
            "ruleset_unavailable",
            f"规则包不可用（{exc}）；条件指纹将缺少 ruleset_sha256。",
            area="输入",
        )
        return None, candidate


def _open_catalog(
    *,
    repo: Path,
    fallback_root: Path,
    issues: list[dict[str, Any]],
) -> Any:
    try:
        from ..catalog import open_catalog

        last_error: Exception | None = None
        for root in (repo, fallback_root):
            try:
                return open_catalog(root=root)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        _add_issue(
            issues,
            "warning",
            "catalog_unavailable",
            f"目录数据库不可用（{last_error}）；节点描述/Buff 参数/门槛证据将降级。",
            area="目录",
        )
        return None
    except Exception as exc:  # noqa: BLE001 - catalog import should not break the run
        _add_issue(issues, "warning", "catalog_unavailable", f"目录数据库不可用（{exc}）。", area="目录")
        return None


def _dps_scenario(scenario: Any, ruleset: Any) -> dict[str, Any]:
    """Return the DPS scenario used to derive auto target states."""
    if isinstance(scenario, dict) and scenario:
        return scenario
    if ruleset is None:
        return {}
    try:
        data = dict(getattr(ruleset, "scenario", {}) or {})
        target_side = getattr(ruleset, "target_side", {}) or {}
        conditions = dict(data.get("target_conditions") or {})
        conditions.setdefault(
            "target_is_boss_or_rare", bool(target_side.get("boss_tag", True))
        )
        conditions.setdefault("target_within_center_1m", True)
        conditions.setdefault("both_stars_cover", True)
        data["target_conditions"] = conditions
        target = dict(data.get("target") or {})
        target.setdefault("boss_tag", conditions.get("target_is_boss_or_rare"))
        target.setdefault("rare_tag", bool(target_side.get("rare_tag", True)))
        data["target"] = target
        coverage = dict(data.get("coverage") or {})
        coverage.setdefault(
            "center_condition_met", bool(conditions.get("target_within_center_1m", True))
        )
        coverage.setdefault(
            "both_stars_cover_target", bool(conditions.get("both_stars_cover", True))
        )
        data["coverage"] = coverage
        return data
    except Exception:  # pragma: no cover - defensive
        return {}


# ---------------------------------------------------------------------------
# Fallback / placeholder context
# ---------------------------------------------------------------------------


def _dps_target(dps_scenario: Any, ruleset: Any) -> dict[str, Any]:
    """Unified target defaults derived from the DPS scenario + ruleset.

    Supports both shapes: the DPS-result scenario target (``resistances.fire``
    / ``boss_tag`` / ``rare_tag``) and the unified conditional-scenario target
    block (``fire_resistance`` / ``boss`` / ``rare``).  Falls back to the
    ruleset target side (level 100, 0% fire resistance, boss & rare true).
    """
    target_side = getattr(ruleset, "target_side", {}) or {}
    monster_dr = target_side.get("monster_dr") or {}
    base_resistances = target_side.get("base_resistances") or {}
    defaults: dict[str, Any] = {
        "monster_level": int(monster_dr.get("default_level", 0) or 0),
        "fire_resistance": float(base_resistances.get("fire", 0.0) or 0.0),
        "boss": bool(target_side.get("boss_tag", True)),
        "rare": bool(target_side.get("rare_tag", True)),
    }
    if not isinstance(dps_scenario, dict):
        return defaults
    candidate = dps_scenario.get("target")
    context = {
        "target": dict(candidate) if isinstance(candidate, dict) else {},
        "target_conditions": dps_scenario.get("target_conditions"),
        "monster_level": dps_scenario.get("monster_level"),
    }
    parsed = target_from_context(context)
    if isinstance(parsed, dict):
        defaults.update(
            {key: value for key, value in parsed.items() if key in defaults}
        )
    return defaults


def _enumerated_counts(snapshot: Any, calc_input: Optional[dict[str, Any]]) -> dict[str, Any]:
    items = (calc_input or {}).get("items") if calc_input else []
    mods = (calc_input or {}).get("mods") if calc_input else []
    items = items if isinstance(items, list) else []
    mods = mods if isinstance(mods, list) else []
    skills = (calc_input or {}).get("skills") if calc_input else {}
    specializations = (skills or {}).get("specializations") if isinstance(skills, dict) else []
    specializations = specializations if isinstance(specializations, list) else []
    passive_tree = (calc_input or {}).get("passive_tree") if calc_input else {}
    passive_nodes = (passive_tree or {}).get("nodes") if isinstance(passive_tree, dict) else []
    passive_nodes = passive_nodes if isinstance(passive_nodes, list) else []
    contributions = list(getattr(snapshot, "contributions", []) or [])
    excluded = list(getattr(snapshot, "excluded_sources", []) or [])
    return {
        "items": len(items),
        "item_mods": len(mods),
        "specializations": len(specializations),
        "skill_nodes_allocated": sum(
            len(spec.get("nodes") or []) for spec in specializations if isinstance(spec, dict)
        ),
        "passive_nodes": len(passive_nodes),
        "snapshot_contributions": len(contributions),
        "excluded_sources": len(excluded),
        "candidates": 0,
        "effect_instances": 0,
        "registered_effects": 0,
        "checked_records": 0,
        "excluded_records": 0,
        "baseline_records": 0,
    }


def _empty_catalog(
    snapshot: Any,
    calc_input: Optional[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    counts = _enumerated_counts(snapshot, calc_input)
    data_gaps = [
        {
            "id": f"issue:{index}",
            "severity": issue.get("severity") or "warning",
            "area": issue.get("area") or "输入",
            "message": issue.get("message") or "",
            "probe": "",
        }
        for index, issue in enumerate(issues)
    ]
    excluded_sources = [
        {
            "id": _text(source.get("source_id")),
            "label": _text(source.get("label")),
            "reason": _text(source.get("reason")),
            "category": _text(source.get("category")),
            "points_total": source.get("points_total"),
            "nodes": [],
        }
        for source in (getattr(snapshot, "excluded_sources", []) or [])
        if isinstance(source, dict)
    ]
    return {
        "schema": C.SCHEMA_CATALOG,
        "binding": {},
        "inputs": {
            "calc_input": {"present": bool(calc_input)},
            "build": {"present": False},
            "catalog": {"available": False},
        },
        "checked_scope": {
            "counts": counts,
            "categories": [],
            "source_instances": [],
            "excluded_sources": excluded_sources,
        },
        "effects": [],
        "candidates": [],
        "checked_records": [],
        "data_gaps": data_gaps,
        "auto_states": [],
        "auto_values": {},
        "baseline_states": [],
        "expectation_mismatches": [],
        "affects": {},
    }


def _empty_evaluation() -> dict[str, Any]:
    return {
        "schema": C.SCHEMA_EVALUATION,
        "summary": {
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
            "registered": 0,
            "relevant": 0,
            "scenario_dependent": 0,
            "unconfirmed": 0,
            "not_relevant": 0,
            "configured": 0,
            "condition_met": 0,
            "condition_unmet": 0,
            "not_selected": 0,
            "auto_active": 0,
            "auto_inactive": 0,
            "auto_unknown": 0,
            "already_in_baseline": 0,
            "engine_supported": 0,
            "needs_rule_mapping": 0,
            "needs_time": 0,
            "mechanism_unresolved": 0,
            "unsupported": 0,
            "proposed_contributions": 0,
        },
        "effects": [],
        "proposed_contributions": [],
        "unsupported": [],
        "validation": {"errors": [], "warnings": []},
        "scenario_fingerprint": None,
        "baseline_fingerprint": None,
        "changed_from_baseline": False,
        "dps_recomputed": False,
    }


def _empty_rules(registry: Optional[Registry], binding: dict[str, Any]) -> dict[str, Any]:
    from .conditions import build_client_rules

    if registry is None:
        return {
            "schema": C.SCHEMA_RULES,
            "states": [],
            "auto_states": [],
            "baseline_states": [],
            "effects": [],
            "status_labels": dict(C.STATUS_LABELS),
            "status_precedence": list(C.STATUS_ORDER),
            "presets": [],
            "baseline_scenario": {},
        }
    try:
        catalog = {
            "binding": binding,
            "effects": [],
            "auto_states": [],
            "baseline_states": [],
            "baseline_scenario": default_scenario(registry, {"binding": binding}),
        }
        return build_client_rules(registry, catalog)
    except Exception:  # pragma: no cover - defensive
        return {
            "schema": C.SCHEMA_RULES,
            "states": [],
            "auto_states": [],
            "effects": [],
            "status_labels": dict(C.STATUS_LABELS),
            "status_precedence": list(C.STATUS_ORDER),
            "presets": [],
            "baseline_scenario": {},
        }


def _minimal_context_block(registry: Optional[Registry], ruleset: Any) -> dict[str, Any]:
    if registry is not None:
        return {
            "main_skill": {
                **registry.main_skill,
                "game_version": registry.game_version,
            },
            "mode": registry.mode(),
            "baseline_profile": registry.baseline_profile(),
            "computation_modules": registry.computation_modules,
            "shared_states": registry.shared_states,
            "disclaimer": registry.disclaimer,
            "baseline_profiles": registry.baseline_profiles,
            "modes": registry.modes,
        }
    ruleset_id = getattr(ruleset, "ruleset_id", C.RULESET_ID_DEFAULT)
    game_version = getattr(ruleset, "game_version", C.GAME_VERSION_DEFAULT)
    return {
        "main_skill": {"id": "bh2", "name": "Black Hole", "ruleset_id": ruleset_id},
        "mode": {"id": C.DEFAULT_MODE_ID, "label": "固定条件估算", "description": ""},
        "baseline_profile": {
            "id": C.DEFAULT_BASELINE_PROFILE_ID,
            "label": "构筑基础状态",
            "description": "",
        },
        "computation_modules": [],
        "shared_states": [],
        "disclaimer": "当前设置描述计算时采用的条件；本页不重算 DPS。",
        "baseline_profiles": [],
        "modes": [],
        "game_version": game_version,
    }


def _assemble(
    *,
    snapshot: Any,
    status: str,
    issues: list[dict[str, Any]],
    registry_block: dict[str, Any],
    input_block: dict[str, Any],
    context_block: dict[str, Any],
    catalog: dict[str, Any],
    scenario: dict[str, Any],
    evaluation: dict[str, Any],
    rules: dict[str, Any],
    repo: Path | None = None,
) -> dict[str, Any]:
    roots = (repo, PROJECT_ROOT) if repo is not None else (PROJECT_ROOT,)
    issues = _sanitize_issues(issues, tuple(root for root in roots if root is not None))
    context: dict[str, Any] = {
        "schema": C.SCHEMA_CONTEXT,
        "status": status,
        "issues": issues,
        "registry": registry_block,
        "input": input_block,
        "context": context_block,
        "catalog": catalog,
        "scenario": scenario,
        "evaluation": evaluation,
        "rules": rules,
    }
    context["view"] = build_view(context)
    return C.json_safe(context)


def _placeholder_context(
    snapshot: Any,
    *,
    status: str,
    issues: list[dict[str, Any]],
    registry_block: dict[str, Any],
    input_block: dict[str, Any],
    context_block: dict[str, Any],
    calc_input: Optional[dict[str, Any]],
    repo: Path | None = None,
) -> dict[str, Any]:
    roots = (repo, PROJECT_ROOT) if repo is not None else (PROJECT_ROOT,)
    issues = _sanitize_issues(issues, tuple(root for root in roots if root is not None))
    catalog = _empty_catalog(snapshot, calc_input, issues)
    return _assemble(
        snapshot=snapshot,
        status=status,
        issues=issues,
        registry_block=registry_block,
        input_block=input_block,
        context_block=context_block,
        catalog=catalog,
        scenario={},
        evaluation=_empty_evaluation(),
        rules=_empty_rules(None, {}),
        repo=repo,
    )


def _empty_input_block(snapshot: Any) -> dict[str, Any]:
    meta = _snapshot_build_meta(snapshot)
    build = meta.get("build") if isinstance(meta.get("build"), dict) else {}
    dataset = meta.get("dataset") if isinstance(meta.get("dataset"), dict) else {}
    return {
        "build": build,
        "calc_input": {},
        "dataset": dataset,
        "fingerprints": {},
        "files": [],
        "game_version": meta.get("game_version"),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_context(
    snapshot: Any,
    *,
    scenario: Optional[dict[str, Any]] = None,
    ruleset: Any = None,
    ruleset_path: str | Path | None = None,
    calc_input_path: str | Path | None = None,
    build_path: str | Path | None = None,
    registry_path: str | Path | None = None,
    scenario_path: str | Path | None = None,
    preset_id: Optional[str] = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build ``result["conditional"]``; never raises (degrade on failure)."""
    issues: list[dict[str, Any]] = []
    repo = Path(repo_root) if repo_root is not None else Path.cwd()
    fallback_root = PROJECT_ROOT
    try:
        snapshot = _coerce_snapshot(snapshot)
    except Exception as exc:  # noqa: BLE001
        _add_issue(issues, "error", "snapshot_invalid", f"snapshot 不可用：{exc}")
        issues = _sanitize_issues(issues, (repo, PROJECT_ROOT))
        return C.json_safe(
            {
                "schema": C.SCHEMA_CONTEXT,
                "status": "unavailable",
                "issues": issues,
                "registry": {"id": None, "sha256": None, "path": None, "schema": None, "exists": False},
                "input": {},
                "context": _minimal_context_block(None, None),
                "catalog": _empty_catalog(None, None, issues),
                "scenario": {},
                "evaluation": _empty_evaluation(),
                "rules": _empty_rules(None, {}),
                "view": build_view({"status": "unavailable", "issues": issues, "context": {}, "catalog": {}}),
            }
        )

    status = "ready"
    try:
        # -- registry ------------------------------------------------------
        registry: Optional[Registry] = None
        resolved_registry_path = (
            _resolve(registry_path, repo, fallback_root=fallback_root)
            if registry_path is not None
            else DEFAULT_REGISTRY_PATH
        )
        if resolved_registry_path is None:  # pragma: no cover - defensive
            resolved_registry_path = DEFAULT_REGISTRY_PATH
        registry_block: dict[str, Any] = {
            "id": None,
            "sha256": None,
            "path": _rel(resolved_registry_path, repo),
            "schema": None,
            "exists": resolved_registry_path.is_file(),
        }
        try:
            registry = load_registry(resolved_registry_path)
            registry_block.update(
                {
                    "id": registry.registry_id,
                    "sha256": registry.sha256,
                    "schema": registry.schema,
                    "game_version": registry.game_version,
                    "dataset_id": registry.dataset_id,
                }
            )
        except RegistryError as exc:
            status = "degraded"
            for problem in exc.issues[:8]:
                _add_issue(
                    issues,
                    "error",
                    str(problem.get("code") or "registry_invalid"),
                    f"条件效果注册表校验失败：{problem.get('message')}"
                    + (f"（{problem.get('path')}）" if problem.get("path") else ""),
                    area="registry",
                )
        except Exception as exc:  # noqa: BLE001
            status = "degraded"
            _add_issue(issues, "error", "registry_unavailable", f"条件效果注册表不可用：{exc}", area="registry")

        # -- ruleset -------------------------------------------------------
        loaded_ruleset, resolved_ruleset_path = _load_ruleset(
            ruleset,
            ruleset_path,
            repo=repo,
            fallback_root=fallback_root,
            issues=issues,
        )

        # -- calc input ----------------------------------------------------
        meta = _snapshot_build_meta(snapshot)
        calc_meta = meta.get("calc_input") if isinstance(meta.get("calc_input"), dict) else {}
        expected_calc_path = calc_input_path if calc_input_path is not None else calc_meta.get("path")
        calc_path = _resolve(expected_calc_path, repo, fallback_root=fallback_root)
        calc_input: Optional[dict[str, Any]] = None
        calc_actual_sha: Optional[str] = None
        if calc_path is not None and calc_path.is_file():
            try:
                calc_input = _read_json(calc_path)
                calc_actual_sha = _file_sha256(calc_path)
            except Exception as exc:  # noqa: BLE001
                status = "degraded"
                _add_issue(issues, "error", "calc_input_invalid", f"计算输入无法读取：{exc}", area="输入")
        else:
            status = "degraded"
            _add_issue(
                issues,
                "error",
                "calc_input_missing",
                f"计算输入缺失或不存在：{_display_path(expected_calc_path, repo)}",
                area="输入",
            )
        expected_calc_sha = calc_meta.get("sha256")
        if calc_input is not None:
            if expected_calc_sha and calc_actual_sha and expected_calc_sha != calc_actual_sha:
                status = "degraded"
                _add_issue(
                    issues,
                    "warning",
                    "calc_input_sha_mismatch",
                    "计算输入实际 sha256 与 snapshot.build_meta.calc_input.sha256 不一致，"
                    "条件页结果按 unverified 输入标记。",
                    area="输入",
                )
            expected_mods = calc_meta.get("mod_count")
            if isinstance(expected_mods, int) and expected_mods != len(calc_input.get("mods") or []):
                _add_issue(
                    issues,
                    "warning",
                    "calc_input_mod_count_mismatch",
                    f"计算输入 mod 数 {len(calc_input.get('mods') or [])} 与 snapshot 记录的 {expected_mods} 不一致。",
                    area="输入",
                )

        # -- bd.json (optional cross-check) --------------------------------
        build_data: Optional[dict[str, Any]] = None
        build_actual_sha: Optional[str] = None
        resolved_build_path: Optional[Path] = None
        if build_path is not None:
            resolved_build_path = _resolve(build_path, repo, fallback_root=fallback_root)
            if resolved_build_path is not None and resolved_build_path.is_file():
                try:
                    build_data = _read_json(resolved_build_path)
                    build_actual_sha = _file_sha256(resolved_build_path)
                except Exception as exc:  # noqa: BLE001
                    _add_issue(issues, "warning", "build_invalid", f"bd.json 无法读取：{exc}", area="输入")
            else:
                _add_issue(
                    issues,
                    "warning",
                    "build_missing",
                    f"bd.json 不存在：{_display_path(resolved_build_path or build_path, repo)}；跳过与原始构筑的交叉核对。",
                    area="输入",
                )

        # -- conditional registry missing: degrade early -------------------
        input_block = _build_input_block(
            snapshot,
            repo=repo,
            fallback_root=fallback_root,
            calc_path=calc_path,
            calc_input=calc_input,
            calc_actual_sha=calc_actual_sha,
            build_path=resolved_build_path,
            build_data=build_data,
            build_actual_sha=build_actual_sha,
            registry=registry,
            registry_path=resolved_registry_path,
            ruleset=loaded_ruleset,
            ruleset_path=resolved_ruleset_path,
        )
        if registry is None:
            return _placeholder_context(
                snapshot,
                status="degraded",
                issues=issues,
                registry_block=registry_block,
                input_block=input_block,
                context_block=_minimal_context_block(None, loaded_ruleset),
                calc_input=calc_input,
                repo=repo,
            )

        # -- binding -------------------------------------------------------
        dataset_meta = meta.get("dataset") if isinstance(meta.get("dataset"), dict) else {}
        build_meta = meta.get("build") if isinstance(meta.get("build"), dict) else {}
        binding = {
            "build_id": build_meta.get("build_id"),
            "revision_id": build_meta.get("revision_id"),
            "build_content_sha256": build_meta.get("content_sha256") or meta.get("content_sha256"),
            "calc_input_sha256": calc_actual_sha or expected_calc_sha,
            "dataset_id": dataset_meta.get("dataset_id"),
            "registry_id": registry.registry_id,
            "registry_sha256": registry.sha256,
            "ruleset_id": getattr(loaded_ruleset, "ruleset_id", None),
            "ruleset_sha256": getattr(loaded_ruleset, "sha256", None),
        }

        # -- discovery -----------------------------------------------------
        dps_scenario = _dps_scenario(scenario, loaded_ruleset)
        target_context = _dps_target(dps_scenario, loaded_ruleset)
        db_catalog = _open_catalog(repo=repo, fallback_root=fallback_root, issues=issues)
        if db_catalog is not None:
            try:
                pointer_dataset = db_catalog.current_pointer.get("dataset_id")
                expected_dataset = dataset_meta.get("dataset_id")
                if pointer_dataset and expected_dataset and pointer_dataset != expected_dataset:
                    _add_issue(
                        issues,
                        "warning",
                        "dataset_mismatch",
                        f"catalog 数据集 {pointer_dataset} 与 snapshot 记录的 {expected_dataset} 不一致。",
                        area="目录",
                    )
            except Exception:  # noqa: BLE001 - optional cross-check
                pass
        try:
            catalog = build_catalog(
                snapshot,
                registry=registry,
                calc_input=calc_input,
                build=build_data,
                catalog=db_catalog,
                dps_scenario=dps_scenario,
                binding=binding,
            )
        except Exception as exc:  # noqa: BLE001 - degrade, never crash the DPS run
            status = "degraded"
            _add_issue(issues, "error", "discovery_failed", f"条件效果发现阶段失败：{exc}", area="发现")
            catalog = _empty_catalog(snapshot, calc_input, issues)
        finally:
            try:
                if db_catalog is not None:
                    db_catalog.close()
            except Exception:  # noqa: BLE001
                pass

        baseline = default_scenario(
            registry, {"binding": binding, "target": target_context}
        )
        catalog["baseline_scenario"] = baseline
        catalog["baseline_fingerprint"] = scenario_fingerprint(baseline)
        catalog["target_context"] = dict(target_context)

        # -- scenario ------------------------------------------------------
        raw_scenario: Optional[dict[str, Any]] = None
        if scenario_path is not None:
            resolved_scenario_path = _resolve(scenario_path, repo, fallback_root=fallback_root)
            try:
                if resolved_scenario_path is None or not resolved_scenario_path.is_file():
                    raise ScenarioError(
                        [
                            {
                                "code": "scenario_missing",
                                "message": f"场景文件不存在：{_display_path(resolved_scenario_path or scenario_path, repo)}",
                            }
                        ]
                    )
                raw_scenario = load_scenario(resolved_scenario_path)
            except ScenarioError as exc:
                status = "degraded"
                for problem in exc.issues[:6]:
                    _add_issue(
                        issues,
                        "error",
                        str(problem.get("code") or "scenario_invalid"),
                        f"场景文件无法加载：{problem.get('message')}",
                        area="场景",
                    )
        elif preset_id is not None:
            try:
                raw_scenario = apply_preset(registry, preset_id, {"binding": binding})
            except ScenarioError as exc:
                status = "degraded"
                _add_issue(issues, "error", "unknown_preset", f"未知的条件场景预设：{preset_id}", area="场景")
        elif (
            isinstance(scenario, dict)
            and scenario.get("schema") == "bh-conditional-scenario-v1"
        ):
            # Explicit conditional scenario dict (e.g. the local one-click
            # recompute service or API callers).  A DPS-result scenario dict does
            # NOT carry this schema and is only used for auto target states.
            raw_scenario = scenario
        if raw_scenario is None:
            raw_scenario = default_scenario(
                registry, {"binding": binding, "target": target_context}
            )
        validation = validate_scenario(raw_scenario, registry, catalog)
        if validation.get("errors"):
            status = "degraded"
            for problem in validation["errors"][:12]:
                _add_issue(
                    issues,
                    "error",
                    str(problem.get("code") or "scenario_validation_error"),
                    f"条件场景校验失败：{problem.get('message')}",
                    area="场景",
                )
        for problem in validation.get("warnings") or []:
            _add_issue(
                issues,
                "warning",
                str(problem.get("code") or "scenario_validation_warning"),
                f"条件场景提示：{problem.get('message')}",
                area="场景",
            )
        evaluation = evaluate_scenario(raw_scenario, registry, catalog)
        scenario_resolved = evaluation.pop("scenario_resolved", None)
        if not isinstance(scenario_resolved, dict):
            scenario_resolved = normalize_scenario(raw_scenario, registry, {"binding": binding})
        evaluation["validation"] = validation

        catalog["scenario_fingerprint"] = evaluation.get("scenario_fingerprint")
        rules = build_client_rules(registry, catalog, scenario_resolved, evaluation)
        context_block = _minimal_context_block(registry, loaded_ruleset)
        context_block["main_skill"] = {
            **context_block["main_skill"],
            "game_version": registry.game_version,
        }
        return _assemble(
            snapshot=snapshot,
            status=status,
            issues=issues,
            registry_block=registry_block,
            input_block=input_block,
            context_block=context_block,
            catalog=catalog,
            scenario=scenario_resolved,
            evaluation=evaluation,
            rules=rules,
            repo=repo,
        )
    except Exception as exc:  # noqa: BLE001 - absolute safety net
        _add_issue(issues, "error", "conditional_internal_error", f"条件上下文构建失败：{exc}")
        try:
            input_block = _build_input_block(
                snapshot,
                repo=repo,
                fallback_root=fallback_root,
                calc_path=None,
                calc_input=None,
                calc_actual_sha=None,
                build_path=None,
                build_data=None,
                build_actual_sha=None,
                registry=None,
                registry_path=None,
                ruleset=None,
                ruleset_path=None,
            )
        except Exception:  # pragma: no cover - defensive
            input_block = _empty_input_block(snapshot)
        return _placeholder_context(
            snapshot,
            status="degraded",
            issues=issues,
            registry_block={"id": None, "sha256": None, "path": None, "schema": None, "exists": False},
            input_block=input_block,
            context_block=_minimal_context_block(None, None),
            calc_input=None,
            repo=repo,
        )


# ---------------------------------------------------------------------------
# Artifacts / export helpers (contract §10 / §11)
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(C.json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")
    return str(path)


def write_artifacts(context: dict[str, Any], out_dir: str | Path) -> dict[str, str]:
    """Write the three conditional artifacts into ``out_dir`` (contract §10)."""
    if not isinstance(context, dict):
        return {}
    status = _text(context.get("status"))
    if status not in ("ready", "degraded"):
        return {}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    effects_payload = {
        "schema": C.SCHEMA_EFFECTS_ARTIFACT,
        "status": status,
        "issues": list(context.get("issues") or []),
        "registry": context.get("registry") or {},
        "input": context.get("input") or {},
        "context": context.get("context") or {},
        "catalog": context.get("catalog") or {},
    }
    written: dict[str, str] = {}
    written["conditional-effects.json"] = _write_json(out / "conditional-effects.json", effects_payload)
    written["conditional-scenario.json"] = _write_json(
        out / "conditional-scenario.json", context.get("scenario") or {}
    )
    written["conditional-evaluation.json"] = _write_json(
        out / "conditional-evaluation.json", context.get("evaluation") or {}
    )
    return written


def export_scenario(context: dict[str, Any], path: str | Path) -> str:
    """Save the context's current scenario as a stable UTF-8 JSON file."""
    scenario = context.get("scenario") if isinstance(context, dict) else None
    if not isinstance(scenario, dict) or not scenario:
        raise ScenarioError([{"code": "scenario_missing", "message": "context has no scenario to export"}])
    return save_scenario(scenario, path)


__all__ = [
    "DEFAULT_RULESET_PATH",
    "build_context",
    "export_scenario",
    "write_artifacts",
]
