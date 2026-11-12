"""Conditional Buff configuration package (backend-conditional).

Public contract (``docs/contracts/conditional-interface.md`` section 11)::

    from le_calc.conditional import build_context, write_artifacts

    context = build_context(
        snapshot,
        scenario=None,
        ruleset=None,
        ruleset_path=None,
        calc_input_path=None,
        build_path=None,
        registry_path=None,
        scenario_path=None,
        preset_id=None,
        repo_root=None,
    )

``build_context`` returns ``result["conditional"]`` (never raises; degrades to
``status="degraded"``/``"unavailable"`` with readable ``issues``).
"""

from __future__ import annotations

from .context import (
    build_context,
    export_scenario,
    write_artifacts,
)
from .conditions import build_client_rules
from .discover import build_catalog
from .evaluate import evaluate_scenario
from .registry import DEFAULT_REGISTRY_PATH, Registry, RegistryError, load_registry, resolve_actuals, validate_registry
from .recompute import (
    channel_support,
    compute_scenario_outputs,
    prepare_scenario,
    recompute_scenario,
)
from .scenario import (
    ScenarioError,
    TARGET_KEYS,
    apply_preset,
    default_scenario,
    load_scenario,
    normalize_scenario,
    resolve_target,
    save_scenario,
    scenario_fingerprint,
    target_from_context,
    validate_scenario,
)
from .view import build_view

__all__ = [
    "DEFAULT_REGISTRY_PATH",
    "Registry",
    "RegistryError",
    "ScenarioError",
    "TARGET_KEYS",
    "apply_preset",
    "build_catalog",
    "build_client_rules",
    "build_context",
    "build_view",
    "channel_support",
    "compute_scenario_outputs",
    "default_scenario",
    "evaluate_scenario",
    "export_scenario",
    "load_registry",
    "load_scenario",
    "normalize_scenario",
    "prepare_scenario",
    "recompute_scenario",
    "resolve_actuals",
    "resolve_target",
    "save_scenario",
    "scenario_fingerprint",
    "target_from_context",
    "validate_registry",
    "validate_scenario",
    "write_artifacts",
]
