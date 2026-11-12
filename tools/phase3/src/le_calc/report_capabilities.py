# -*- coding: utf-8 -*-
"""Model capability metadata for the player report and local recompute service (3-A).

The artifact ``report-capabilities.json`` is written next to every run report
(``reports/<case>/<run>/report-capabilities.json``).  It carries, per model:

* ``ui_contract_version`` — which player-report contract the report follows;
* ``renderer_fingerprint`` — hash of the renderer/template/CSS/JS sources, so a
  renderer change invalidates the report cache without touching numeric caches;
* ``capabilities`` — the ``le-model-capabilities-v1`` document consumed by the
  report renderer (pages, target fields, buff controls, windows/modes,
  recalculation contract, informational notes) and by the service-side request
  validation (``le_calc.recalc``);
* ``acceptance_refs`` — repository-relative references to the report contract /
  acceptance matrix and, when present, the case expected baseline and the case
  configuration.  Author-local audit reports are referenced only if they exist.

The capability declarations are the **single source** for control defaults,
limits and supported states: the renderer must not hardcode them in JS
(``docs/contracts/workflow-interface.md`` §10.1).  When the shared renderer
(P3R-A) accepts ``capabilities``/``recalc_endpoint`` keyword arguments, the
capability document is passed through unchanged; when it does not (staged
implementation) it is still written to disk and exposed by the service.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from . import recalc, report_fingerprint

UI_CONTRACT_VERSION = "le-player-report-ui-v1"
SCHEMA_REPORT_CAPABILITIES = "report-capabilities-v1"
CAPABILITIES_VERSION_PREFIX = "le-model-capabilities-v1"
CAPABILITIES_FILE = "report-capabilities.json"

#: Renderer/template/assets that determine the report HTML (cache fingerprint input).
#: Deprecated lookup view: the authoritative coverage lives in
#: :mod:`le_calc.report_fingerprint` (O-C single source).
RENDERER_SOURCE_PATHS: tuple[str, ...] = (
    *report_fingerprint.MODEL_RENDERER_PATHS,
    report_fingerprint.REPORT_UI_DIR,
)

MODEL_ALIASES = {
    "black-hole": "black-hole",
    "bh": "black-hole",
    "event": "black-hole",
    "hit": "hit",
    "flame-reave": "hit",
    "fr": "hit",
}

#: Pages enabled per model (generic pages + model-specific pages, §10.1).
MODEL_PAGES: dict[str, list[dict[str, Any]]] = {
    "black-hole": [
        {"id": "overview", "label": "总览", "kind": "generic"},
        {"id": "composition", "label": "伤害构成", "kind": "generic"},
        {"id": "ignite", "label": "点燃", "kind": "model"},
        {"id": "timeline", "label": "时间轴", "kind": "model"},
        {"id": "sources", "label": "增伤来源", "kind": "generic"},
        {"id": "buffs", "label": "Buff 设置", "kind": "generic"},
    ],
    "hit": [
        {"id": "overview", "label": "总览", "kind": "generic"},
        {"id": "composition", "label": "伤害构成", "kind": "generic"},
        {"id": "dps", "label": "命中与 DPS", "kind": "model"},
        {"id": "sources", "label": "增伤来源", "kind": "generic"},
        {"id": "buffs", "label": "Buff 设置", "kind": "generic"},
    ],
}

#: Target panel fields per model (editable values that change the target, §10.1).
MODEL_TARGET_FIELDS: dict[str, list[dict[str, Any]]] = {
    "black-hole": [
        {
            "field": "monster_level",
            "kind": "int",
            "min": 1,
            "max": 100,
            "default": 100,
            "unit": "level",
            "label": "怪物等级",
        },
        {
            "field": "fire_resistance",
            "kind": "float",
            "min": -1.0,
            "max": 1.0,
            "default": 0.0,
            "unit": "fraction",
            "label": "目标火抗（0 抗为 0.0，负抗为负值）",
        },
        {"field": "boss", "kind": "bool", "default": True, "label": "Boss"},
        {"field": "rare", "kind": "bool", "default": True, "label": "稀有"},
        {"field": "boss_or_rare", "kind": "bool", "default": True, "label": "目标为 Boss 或稀有"},
    ],
    "hit": [
        {
            "field": "monster_level",
            "kind": "int",
            "min": 1,
            "max": 100,
            "default": 100,
            "unit": "level",
            "label": "怪物等级",
        },
        {
            "field": "fire_resistance",
            "kind": "float",
            "min": -1.0,
            "max": 1.0,
            "default": 0.0,
            "unit": "fraction",
            "label": "目标火抗",
        },
        {"field": "boss_or_rare", "kind": "bool", "default": True, "label": "目标为 Boss 或稀有"},
        {"field": "enemy_high_health", "kind": "bool", "default": True, "label": "目标高血量（Army of Skin 条件）"},
        {"field": "ignited", "kind": "bool", "default": False, "label": "目标处于点燃状态"},
    ],
}

#: Buff controls declared per model.  BH is enriched from the conditional
#: registry / report view at build time; FR controls come from the hit model's
#: supported states with their stack limits (previously hardcoded in the report JS).
MODEL_BUFF_CONTROLS: dict[str, list[dict[str, Any]]] = {
    "black-hole": [],
    "hit": [
        {
            "state_key": "ambition_stacks",
            "kind": "stacks",
            "min": 0,
            "max": 20,
            "default": 0,
            "label": "Ambition 层数（Throne of Ambition）",
            "source": "unique",
        },
        {
            "state_key": "blade_weaving_stacks",
            "kind": "stacks",
            "min": 0,
            "max": 10,
            "default": 0,
            "label": "Blade Weaving 层数",
            "source": "skill_node",
        },
        {
            "state_key": "rhythm_of_fire_stacks",
            "kind": "stacks",
            "min": 0,
            "max": 12,
            "default": 0,
            "label": "Rhythm of Fire 层数（12 层可消耗）",
            "source": "skill_node",
        },
        {
            "state_key": "firebrand_stacks",
            "kind": "stacks",
            "min": 0,
            "max": 10,
            "default": 0,
            "label": "Firebrand 层数",
            "source": "skill_node",
        },
        {
            "state_key": "frenzy_active",
            "kind": "toggle",
            "default": False,
            "label": "Frenzy 生效",
            "source": "skill_node",
        },
        {
            "state_key": "enemy_high_health",
            "kind": "toggle",
            "default": True,
            "label": "目标高血量（Army of Skin 49% more Melee）",
            "source": "unique",
        },
        {
            "state_key": "ignited",
            "kind": "toggle",
            "default": False,
            "label": "目标处于点燃状态（Scorching Tide / Chaos Flare）",
            "source": "condition",
        },
        {
            "state_key": "consume_blade_weaving",
            "kind": "toggle",
            "default": False,
            "label": "消耗 Blade Weaving",
            "source": "skill_node",
        },
        {
            "state_key": "consume_firebrand",
            "kind": "toggle",
            "default": False,
            "label": "消耗 Firebrand",
            "source": "skill_node",
        },
        {
            "state_key": "consume_rhythm_of_fire",
            "kind": "toggle",
            "default": False,
            "label": "消耗 Rhythm of Fire",
            "source": "skill_node",
        },
        {
            "state_key": "enchant_weapon_active",
            "kind": "toggle",
            "default": True,
            "label": "Enchant Weapon 生效",
            "source": "skill_node",
        },
        {
            "state_key": "dual_wield",
            "kind": "toggle",
            "default": True,
            "readonly": True,
            "label": "双持（由输入决定）",
            "source": "input",
        },
        {
            "state_key": "target_in_melee_range",
            "kind": "toggle",
            "default": True,
            "readonly": True,
            "label": "目标在近战范围内（由输入决定）",
            "source": "input",
        },
    ],
}

MODEL_MODEL_PARAMETERS: dict[str, list[dict[str, Any]]] = {
    "black-hole": [],
    "hit": [
        {
            "key": "hits_per_cast",
            "kind": "int",
            "min": 1,
            "max": 8,
            "default": 4,
            "unit": "hits",
            "label": "每次施放命中次数（命中权重默认 1/0.5/…）",
            "aliases": ["hit_override.hits_per_cast"],
        }
    ],
}

#: F1 hit-weight policy: how the skill/model expands default hit weights.
#: ``first_full_then_fraction`` = hit 1 gets 1.0, subsequent hits get 0.5 each
#: (policy, not "repeat the last value of a truncated list").  Custom weights are
#: only used when a config/scenario explicitly declares ``hit_override.damage_weights``.
#: BH has no hit policy (event model).
MODEL_HIT_POLICY: dict[str, dict[str, Any]] = {
    "black-hole": {"kind": "not_applicable"},
    "hit": {
        "kind": "first_full_then_fraction",
        "first_weight": 1.0,
        "subsequent_weight": 0.5,
        "hits": {"min": 1, "max": 8, "default": 4},
        "custom_weights_supported": True,
        "semantics": (
            "默认命中权重由技能政策按命中数展开：第 1 击 1.0，后续每击 0.5；"
            "不是重复已截断列表的末项。自定义 damage_weights 仅在显式配置/场景中作为覆盖政策。"
        ),
    },
}

MODEL_BASELINE_PROFILES: dict[str, list[str]] = {
    "black-hole": ["legacy_single_skill", "build_base"],
    "hit": ["legacy_single_skill", "build_base"],
}

MODEL_MODES: dict[str, list[str]] = {
    "black-hole": ["eager", "patient"],
    "hit": ["cooldown_limited", "attack_speed_limited"],
}

MODEL_WINDOWS: dict[str, list[float]] = {
    "black-hole": [],
    "hit": [5.0, 30.0, 120.0],
}

#: Complete-scenario top-level fields; ``allowed`` keeps legacy keys the current
#: report JS still posts (they are validated with a strict type/range check).
MODEL_SCENARIO_FIELDS: dict[str, dict[str, list[str]]] = {
    "black-hole": {
        "required": ["baseline_profile", "target", "states"],
        "allowed": [
            "baseline_profile",
            "target",
            "states",
            "mode_policy",
            "model_parameters",
            "effects",
            "single_cast",
            "loop",
            "mode",
            "preset_id",
            "schema",
            "binding",
        ],
    },
    "hit": {
        "required": ["baseline_profile", "target", "states"],
        "allowed": [
            "baseline_profile",
            "target",
            "states",
            "mode_policy",
            "model_parameters",
            "hit_override",
            "single_cast",
            "single_hit",
            "dps",
            "profiles",
            "mode",
            "assumptions",
            "schema",
            "case_id",
            "binding",
        ],
    },
}

#: Legacy scenario keys the current report JS still posts; typed/range validated.
MODEL_LEGACY_SCENARIO_FIELDS: dict[str, dict[str, dict[str, str]]] = {
    "black-hole": {
        "effects": {"kind": "effect_map"},
        "single_cast": {"kind": "object"},
        "loop": {"kind": "object"},
        "mode": {"kind": "string"},
        "preset_id": {"kind": "string"},
        "schema": {"kind": "string"},
        "binding": {"kind": "object"},
    },
    "hit": {
        "hit_override": {"kind": "hit_override"},
        "single_cast": {"kind": "object"},
        "single_hit": {"kind": "object"},
        "dps": {"kind": "object"},
        "profiles": {"kind": "object"},
        "mode": {"kind": "string"},
        "assumptions": {"kind": "list"},
        "schema": {"kind": "string"},
        "case_id": {"kind": "string"},
        "binding": {"kind": "object"},
    },
}

MODEL_INFORMATIONAL: dict[str, list[dict[str, Any]]] = {    "black-hole": [
        {
            "id": "scope.conditional-fixed-estimate",
            "message": "条件 Buff 按固定条件估算，不模拟动作序列/获得成本；未映射效果仍保留在数据与短范围标签。",
        },
        {
            "id": "scope.no-auto-recompute",
            "message": "页面加载不自动连接服务；仅用户点击计算时请求本机重算。",
        },
    ],
    "hit": [
        {
            "id": "scope.skill-level-scaling",
            "message": "技能等级缩放未建模（R16 unresolved），单次绝对伤害可能系统性偏低。",
        },
        {
            "id": "scope.condition-fixed-estimate",
            "message": "条件 Buff 按固定条件估算，不模拟叠层/资源/操作序列。",
        },
    ],
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _canonical_model_id(model_id: str) -> str:
    key = str(model_id or "").strip().lower()
    canonical = MODEL_ALIASES.get(key)
    if canonical is None:
        raise ValueError(
            f"unknown report model {model_id!r}（已知：{', '.join(sorted(MODEL_ALIASES))}）"
        )
    return canonical


def _read_json(path: str | Path) -> Optional[dict[str, Any]]:
    candidate = Path(path)
    if not candidate.is_file():
        return None
    try:
        document = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# renderer fingerprint (report cache invalidation) — O-C single source
# ---------------------------------------------------------------------------


def renderer_sources(repo_root: str | Path) -> list[Path]:
    """Return the renderer/template/CSS/JS files that make up the report HTML.

    Delegates to :mod:`le_calc.report_fingerprint`; there is no second
    implementation of the fingerprint coverage anywhere in the repo.
    """
    return report_fingerprint.renderer_sources(repo_root)


def renderer_fingerprint(repo_root: str | Path) -> str:
    """Authoritative renderer fingerprint (delegates to ``report_fingerprint``)."""
    return report_fingerprint.renderer_fingerprint(repo_root)


# ---------------------------------------------------------------------------
# capability derivation from registry / run result
# ---------------------------------------------------------------------------


def _control_from_shared_state(state: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    state_id = state.get("id")
    if not isinstance(state_id, str) or not state_id:
        return None
    kind = str(state.get("kind") or "bool").lower()
    if kind in ("bool", "boolean", "toggle"):
        control: dict[str, Any] = {"state_key": state_id, "kind": "toggle", "default": bool(state.get("default"))}
        if state.get("source") == "auto":
            control["readonly"] = True
    elif kind in ("stacks", "int", "integer"):
        bounds = state.get("bounds") or {}
        control = {
            "state_key": state_id,
            "kind": "stacks",
            "min": int(bounds.get("min", 0) or 0),
            "max": int(bounds.get("max", 0) or 0) or None,
            "default": int(state.get("default") or 0),
        }
        if state.get("source") == "auto":
            control["readonly"] = True
    else:
        return None
    if state.get("label"):
        control["label"] = state["label"]
    if state.get("source"):
        control["source"] = state["source"]
    return control


def _controls_from_effect(effect: Mapping[str, Any]) -> list[dict[str, Any]]:
    control = effect.get("control")
    if not isinstance(control, Mapping):
        return []
    kind = str(control.get("kind") or "").lower()
    label = control.get("label") or effect.get("label") or effect.get("effect_id")
    if kind == "toggle":
        state_id = control.get("state_id")
        if not isinstance(state_id, str) or not state_id:
            return []
        return [
            {
                "state_key": state_id,
                "kind": "toggle",
                "default": bool(control.get("default")),
                "label": str(label),
                "source": "conditional_effect",
                "effect_id": effect.get("effect_id"),
            }
        ]
    if kind == "stacks":
        state_id = control.get("stack_state_id")
        if not isinstance(state_id, str) or not state_id:
            return []
        bounds = control.get("bounds") or {}
        return [
            {
                "state_key": state_id,
                "kind": "stacks",
                "min": int(bounds.get("min", 0) or 0),
                "max": int(bounds.get("max", 0) or 0) or None,
                "default": int(control.get("default") or 0),
                "label": str(label),
                "source": "conditional_effect",
                "effect_id": effect.get("effect_id"),
            }
        ]
    if kind == "cast_state":
        state_id = control.get("cast_state_id")
        if not isinstance(state_id, str) or not state_id:
            return []
        return [
            {
                "state_key": state_id,
                "kind": "toggle",
                "default": bool(control.get("default")),
                "label": str(label),
                "source": "conditional_effect",
                "effect_id": effect.get("effect_id"),
            }
        ]
    return []


def _merge_controls(*groups: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def add(control: Mapping[str, Any]) -> None:
        state_key = control.get("state_key")
        if not isinstance(state_key, str) or not state_key:
            return
        existing = merged.get(state_key)
        if existing is None:
            merged[state_key] = dict(control)
            order.append(state_key)
            return
        # Editable declarations win over readonly/auto ones; explicit limits win.
        if existing.get("readonly") and not control.get("readonly"):
            merged[state_key] = dict(control)
        else:
            for key, value in control.items():
                existing.setdefault(key, value)

    for group in groups:
        for control in group:
            add(control)
    return [merged[key] for key in order]


def _bh_capabilities(
    *,
    result: Optional[Mapping[str, Any]] = None,
    registry: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    controls: list[dict[str, Any]] = []
    if isinstance(registry, Mapping):
        shared = [_control_from_shared_state(state) for state in registry.get("shared_states") or []]
        effects: list[dict[str, Any]] = []
        for effect in registry.get("effects") or []:
            if isinstance(effect, Mapping):
                effects.extend(_controls_from_effect(effect))
        controls = _merge_controls([item for item in shared if item], effects)
    if isinstance(result, Mapping):
        conditional = result.get("conditional") if isinstance(result.get("conditional"), Mapping) else {}
        view = conditional.get("view") if isinstance(conditional.get("view"), Mapping) else {}
        view_controls: list[dict[str, Any]] = []
        for state in view.get("states") or []:
            if not isinstance(state, Mapping):
                continue
            state_id = state.get("id")
            if not isinstance(state_id, str) or not state_id:
                continue
            kind = str(state.get("kind") or "bool").lower()
            control: dict[str, Any] = {
                "state_key": state_id,
                "kind": "stacks" if kind in ("stacks", "int") else "toggle",
                "default": state.get("value"),
                "label": str(state.get("label") or state_id),
                "source": str(state.get("source") or "user"),
            }
            if control["kind"] == "stacks":
                control["min"] = 0
            if state.get("editable") is False:
                control["readonly"] = True
            view_controls.append(control)
        controls = _merge_controls(controls, view_controls)
    # Target-panel states from the report view become target fields.
    target_fields = [dict(item) for item in MODEL_TARGET_FIELDS["black-hole"]]
    if isinstance(result, Mapping):
        conditional = result.get("conditional") if isinstance(result.get("conditional"), Mapping) else {}
        view = conditional.get("view") if isinstance(conditional.get("view"), Mapping) else {}
        seen = {str(item.get("field")) for item in target_fields}
        for state in view.get("states") or []:
            if not isinstance(state, Mapping):
                continue
            state_id = state.get("id")
            if not isinstance(state_id, str) or not state_id.startswith("target.") or state_id in seen:
                continue
            if state.get("editable") is False:
                continue
            target_fields.append(
                {
                    "field": state_id,
                    "kind": "bool" if str(state.get("kind") or "bool") == "bool" else "int",
                    "default": state.get("value"),
                    "label": str(state.get("label") or state_id),
                    "source": "target_panel",
                }
            )
            seen.add(state_id)
    baseline_profiles = list(MODEL_BASELINE_PROFILES["black-hole"])
    modes = list(MODEL_MODES["black-hole"])
    if isinstance(result, Mapping):
        conditional = result.get("conditional") if isinstance(result.get("conditional"), Mapping) else {}
        view = conditional.get("view") if isinstance(conditional.get("view"), Mapping) else {}
        presets = [item.get("id") for item in view.get("presets") or [] if isinstance(item, Mapping)]
        presets = [item for item in presets if isinstance(item, str)]
        if presets:
            # ``legacy_single_skill`` is the engine's main-result口径 (auto proposals
            # excluded).  Report-view presets come from the conditional registry and
            # do not contain it, so merge instead of replacing: a default recompute
            # of the initial page must accept its own profile (LIVE-08) and keep the
            # main口径 unchanged (otherwise it silently becomes build_base).
            merged_profiles: list[str] = list(MODEL_BASELINE_PROFILES["black-hole"])
            for preset_id in presets:
                if preset_id not in merged_profiles:
                    merged_profiles.append(preset_id)
            baseline_profiles = merged_profiles
        policies = []
        for loop in (result.get("action_policy") or {}).get("loops") or []:
            if isinstance(loop, Mapping) and loop.get("policy_id"):
                policies.append(str(loop["policy_id"]))
        if policies:
            modes = policies
    informational = [dict(item) for item in MODEL_INFORMATIONAL["black-hole"]]
    if isinstance(result, Mapping):
        for issue in result.get("issues") or []:
            if isinstance(issue, Mapping) and issue.get("message"):
                informational.append(
                    {
                        "id": str(issue.get("code") or "issue"),
                        "message": str(issue.get("message")),
                        "status": str(issue.get("status") or "info"),
                    }
                )
    return {
        "pages": [dict(item) for item in MODEL_PAGES["black-hole"]],
        "target_fields": target_fields,
        "buff_controls": controls,
        "windows": list(MODEL_WINDOWS["black-hole"]),
        "modes": modes,
        "baseline_profiles": baseline_profiles,
        "model_parameters": [dict(item) for item in MODEL_MODEL_PARAMETERS["black-hole"]],
        "informational": informational,
    }


def _fr_capabilities(*, result: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    controls = [dict(item) for item in MODEL_BUFF_CONTROLS["hit"]]
    modes = list(MODEL_MODES["hit"])
    windows = list(MODEL_WINDOWS["hit"])
    baseline_profiles = list(MODEL_BASELINE_PROFILES["hit"])
    informational = [dict(item) for item in MODEL_INFORMATIONAL["hit"]]
    if isinstance(result, Mapping):
        buffs = result.get("buffs") if isinstance(result.get("buffs"), Mapping) else {}
        derived: list[dict[str, Any]] = []
        for entry in buffs.get("applied") or []:
            if not isinstance(entry, Mapping):
                continue
            state = entry.get("state")
            if not isinstance(state, str) or not state:
                continue
            value = entry.get("value")
            if isinstance(value, bool):
                derived.append(
                    {"state_key": state, "kind": "toggle", "default": value, "source": "screening"}
                )
            elif isinstance(value, int) and not isinstance(value, bool):
                derived.append(
                    {
                        "state_key": state,
                        "kind": "stacks",
                        "min": 0,
                        "default": int(value),
                        "source": "screening",
                    }
                )
        controls = _merge_controls(controls, derived)
        screening = buffs.get("screening") if isinstance(buffs.get("screening"), Mapping) else {}
        for candidate in screening.get("candidates") or []:
            if not isinstance(candidate, Mapping):
                continue
            stacks_state = candidate.get("stacks_state")
            if not isinstance(stacks_state, str) or not stacks_state:
                continue
            max_stacks = candidate.get("max_stacks")
            controls = _merge_controls(
                controls,
                [
                    {
                        "state_key": stacks_state,
                        "kind": "stacks",
                        "min": 0,
                        "max": int(max_stacks) if isinstance(max_stacks, int) else None,
                        "default": 0,
                        "label": str(candidate.get("label") or stacks_state),
                        "source": "skill_node",
                    }
                ],
            )
        dps = result.get("dps") if isinstance(result.get("dps"), Mapping) else {}
        policy_ids = []
        for key in ("policy", "alternate_policy"):
            block = dps.get(key)
            if isinstance(block, Mapping) and block.get("id"):
                policy_ids.append(str(block["id"]))
        if policy_ids:
            modes = policy_ids
        window_s = [float(item) for item in (dps.get("scenario_windows_s") or []) if isinstance(item, (int, float))]
        if window_s:
            windows = window_s
        scenario = result.get("scenario") if isinstance(result.get("scenario"), Mapping) else {}
        profiles = scenario.get("profiles")
        if isinstance(profiles, Mapping) and profiles:
            baseline_profiles = [str(key) for key in profiles]
        for issue in result.get("issues") or []:
            if isinstance(issue, Mapping) and issue.get("message"):
                informational.append(
                    {
                        "id": str(issue.get("code") or "issue"),
                        "message": str(issue.get("message")),
                        "status": str(issue.get("status") or "info"),
                    }
                )
    return {
        "pages": [dict(item) for item in MODEL_PAGES["hit"]],
        "target_fields": [dict(item) for item in MODEL_TARGET_FIELDS["hit"]],
        "buff_controls": controls,
        "windows": windows,
        "modes": modes,
        "baseline_profiles": baseline_profiles,
        "model_parameters": [dict(item) for item in MODEL_MODEL_PARAMETERS["hit"]],
        "informational": informational,
    }


# ---------------------------------------------------------------------------
# model capabilities document (``le-model-capabilities-v1``)
# ---------------------------------------------------------------------------


def capabilities_for_model(
    model_id: str,
    *,
    repo_root: str | Path,
    result: Optional[Mapping[str, Any]] = None,
    registry: Optional[Mapping[str, Any]] = None,
    scenario_doc: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build the ``le-model-capabilities-v1`` document for one model."""
    canonical = _canonical_model_id(model_id)
    if canonical == "black-hole":
        specifics = _bh_capabilities(result=result, registry=registry)
        skill_id = "bh2"
    else:
        specifics = _fr_capabilities(result=result)
        skill_id = "fr11mv"
    profile_states: dict[str, dict[str, Any]] = {}
    profiles = (scenario_doc or {}).get("profiles") if isinstance(scenario_doc, Mapping) else None
    if isinstance(profiles, Mapping):
        for profile_id, profile in profiles.items():
            if isinstance(profile, Mapping) and isinstance(profile.get("states"), Mapping):
                profile_states[str(profile_id)] = dict(profile["states"])
    document: dict[str, Any] = {
        "schema_version": recalc.SCHEMA_MODEL_CAPABILITIES,
        "model_id": canonical,
        "skill_id": skill_id,
        "ui_contract_version": UI_CONTRACT_VERSION,
        "renderer_fingerprint": renderer_fingerprint(repo_root),
        "hit_policy": _hit_policy_for(canonical, specifics["model_parameters"]),
        "pages": specifics["pages"],
        "target_fields": specifics["target_fields"],
        "buff_controls": specifics["buff_controls"],
        "windows": specifics["windows"],
        "modes": specifics["modes"],
        "baseline_profiles": specifics["baseline_profiles"],
        "profile_states": profile_states,
        "model_parameters": specifics["model_parameters"],
        "scenario_fields": MODEL_SCENARIO_FIELDS[canonical],
        "legacy_scenario_fields": dict(MODEL_LEGACY_SCENARIO_FIELDS[canonical]),
        "recalculation": {
            "supported": True,
            "request_kind": recalc.SCHEMA_RECALC_REQUEST,
            "response_kind": recalc.SCHEMA_RECALC_RESPONSE,
            "bindings_kind": recalc.SCHEMA_RECALC_BINDINGS,
            "report_config_schema": recalc.SCHEMA_REPORT_CONFIG,
            "session_scoped_revisions": True,
            "client_session": {
                "field": "client_session_id",
                "required": False,
                "pattern": recalc.CLIENT_SESSION_ID_PATTERN.pattern,
                "generation": "per page load (crypto.randomUUID() or equivalent)",
                "legacy_policy": (
                    "missing id => isolated per-request scope (client_session_legacy=true); "
                    "upgrade required for LIVE-07 old-response protection"
                ),
            },
            "capabilities_version": "",
            "binding_checks": [
                "schema_version",
                "case_id",
                "model_id",
                "capabilities_version",
                "input_fingerprints",
                "report_ref",
                "client_edit_revision",
            ],
            "commands": list(recalc.COMMANDS),
        },
        "informational": specifics["informational"],
    }
    document["recalculation"]["capabilities_version"] = capabilities_version(document)
    return document


def _hit_policy_for(
    canonical: str, model_parameters: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Capacity declaration for the hit-weight policy (F1)."""
    policy = dict(MODEL_HIT_POLICY[canonical])
    if canonical != "hit":
        return policy
    declaration = next(
        (
            item
            for item in model_parameters
            if str(item.get("key") or item.get("parameter")) == "hits_per_cast"
        ),
        None,
    )
    if isinstance(declaration, Mapping):
        policy["hits"] = {
            "min": int(declaration.get("min", 1)),
            "max": int(declaration.get("max", 8)),
            "default": int(declaration.get("default", 4)),
        }
    return policy


def capabilities_version(capabilities: Mapping[str, Any]) -> str:
    """Version derived from the declaration content (changes invalidate clients)."""
    payload = {key: value for key, value in capabilities.items() if key != "recalculation"}
    payload["recalculation"] = {
        key: value
        for key, value in (capabilities.get("recalculation") or {}).items()
        if key != "capabilities_version"
    }
    return f"{CAPABILITIES_VERSION_PREFIX}:{recalc.digest_document(payload)[:16]}"


def capabilities_digest(capabilities: Mapping[str, Any]) -> str:
    return recalc.digest_document(dict(capabilities))


# ---------------------------------------------------------------------------
# acceptance_refs
# ---------------------------------------------------------------------------

#: Contract documents the report capabilities refer to.  Only refs whose file is
#: actually present in the tree are emitted, so an unpacked public tree (no
#: author-local reports/examples) still produces a valid capabilities document.
_ACCEPTANCE_CONTRACT_REFS: tuple[dict[str, Any], ...] = (
    {
        "id": "le-player-report-ui-v1",
        "kind": "contract",
        "path": "docs/contracts/player-report-ui.md",
        "locator": "跨 BD 玩家 HTML 与本地重算交付契约",
    },
    {
        "id": "HTML-01..12",
        "kind": "acceptance_matrix",
        "path": "docs/contracts/player-report-ui.md",
        "locator": "## 2. 页面与文案（HTML 系列）",
    },
    {
        "id": "LIVE-01..08",
        "kind": "acceptance_matrix",
        "path": "docs/contracts/player-report-ui.md",
        "locator": "## 3. 本地重算（LIVE 系列）",
    },
    {
        "id": "workflow-interface-10",
        "kind": "contract",
        "path": "docs/contracts/workflow-interface.md",
        "locator": "## 10. 模型视图与本地重算能力边界（P0 定稿）",
    },
)

#: Fallback locator for the model view when the caller has no case config path.
_MODEL_CONTRACT_FALLBACK = "docs/contracts/workflow-interface.md"


def _repo_relative_posix(value: Any) -> Optional[str]:
    """Return a portable repo-relative POSIX path string, or ``None``."""
    if value is None:
        return None
    text = str(value).replace("\\", "/").strip()
    if not text:
        return None
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        return None
    return text


def acceptance_refs(
    case_id: str,
    *,
    repo_root: str | Path,
    model_id: str,
    case_ref: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Repository-relative acceptance references.

    Every emitted ref points at a file that exists under ``repo_root``: contract
    refs are included only when the contract file is present, the case expected
    summary only when the case ships one, and the model ref points at the actual
    case configuration when the caller provides its repo-relative path (falling
    back to the model-view contract when there is no case file).
    """
    root = Path(repo_root)
    canonical = _canonical_model_id(model_id)
    refs: list[dict[str, Any]] = []
    for ref in _ACCEPTANCE_CONTRACT_REFS:
        if (root / ref["path"]).is_file():
            refs.append(dict(ref))
    expected = f"examples/{case_id}/expected/expected-summary.json"
    if (root / expected).is_file():
        refs.append(
            {
                "id": "case-expected",
                "kind": "case_expected",
                "path": expected,
                "locator": "$.checks",
            }
        )
    model_ref: Optional[dict[str, Any]] = None
    case_relative = _repo_relative_posix(case_ref)
    if case_relative and (root / case_relative).is_file():
        model_ref = {
            "id": f"model-{canonical}",
            "kind": "model",
            "path": case_relative,
            "locator": "$.model",
        }
    if model_ref is None:
        legacy_case = f"examples/{case_id}/case.json"
        if (root / legacy_case).is_file():
            model_ref = {
                "id": f"model-{canonical}",
                "kind": "model",
                "path": legacy_case,
                "locator": "$.model",
            }
    if model_ref is None and (root / _MODEL_CONTRACT_FALLBACK).is_file():
        model_ref = {
            "id": f"model-{canonical}",
            "kind": "model",
            "path": _MODEL_CONTRACT_FALLBACK,
            "locator": "## 10. 模型视图与本地重算能力边界（P0 定稿）",
        }
    if model_ref is not None:
        refs.append(model_ref)
    if not refs:
        raise ValueError(
            "无法生成 acceptance_refs：树内既没有 docs/contracts 契约文件，"
            "也没有可引用的 case 配置；请确认发布树完整"
        )
    return refs


def validate_acceptance_refs(
    refs: Sequence[Mapping[str, Any]], *, repo_root: str | Path
) -> list[str]:
    """Return human readable problems for missing/non-portable acceptance refs."""
    problems: list[str] = []
    root = Path(repo_root)
    for index, ref in enumerate(refs):
        if not isinstance(ref, Mapping):
            problems.append(f"acceptance_refs[{index}] 必须是对象")
            continue
        path = ref.get("path")
        if not isinstance(path, str) or not path:
            problems.append(f"acceptance_refs[{index}].path 缺失")
            continue
        if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
            problems.append(f"acceptance_refs[{index}].path 必须是仓库相对路径：{path!r}")
            continue
        if not (root / path).exists():
            problems.append(f"acceptance_refs[{index}].path 不存在：{path}")
    return problems


# ---------------------------------------------------------------------------
# report-capabilities.json artifact
# ---------------------------------------------------------------------------


def build_report_capabilities(
    *,
    case_id: str,
    model_id: str,
    repo_root: str | Path,
    run_id: Optional[str] = None,
    report_path: Optional[str] = None,
    result: Optional[Mapping[str, Any]] = None,
    registry: Optional[Mapping[str, Any]] = None,
    scenario_doc: Optional[Mapping[str, Any]] = None,
    bindings: Optional[Mapping[str, Any]] = None,
    generated_at: Optional[str] = None,
    case_ref: Optional[str] = None,
) -> dict[str, Any]:
    """Build the ``report-capabilities.json`` payload for one run report.

    ``case_ref`` is the optional repository-relative path of the case
    configuration; when given it becomes the ``model`` acceptance ref instead of
    guessing an author-local example path.
    """
    canonical = _canonical_model_id(model_id)
    capabilities = capabilities_for_model(
        canonical, repo_root=repo_root, result=result, registry=registry, scenario_doc=scenario_doc
    )
    refs = acceptance_refs(case_id, repo_root=repo_root, model_id=canonical, case_ref=case_ref)
    document: dict[str, Any] = {
        "schema_version": SCHEMA_REPORT_CAPABILITIES,
        "case_id": str(case_id),
        "model_id": canonical,
        "skill_id": capabilities.get("skill_id"),
        "run_id": str(run_id) if run_id else None,
        "report_path": str(report_path).replace("\\", "/") if report_path else None,
        "ui_contract_version": UI_CONTRACT_VERSION,
        "renderer_fingerprint": renderer_fingerprint(repo_root),
        "capabilities_digest": capabilities_digest(capabilities),
        "capabilities": capabilities,
        "profile_states": dict(capabilities.get("profile_states") or {}),
        "acceptance_refs": refs,
        "generated_at": generated_at,
    }
    if bindings is not None:
        document["bindings"] = dict(bindings)
    validate_report_capabilities(document, repo_root=repo_root)
    return document


def validate_report_capabilities(
    document: Any,
    *,
    repo_root: Optional[str | Path] = None,
    strict_acceptance_refs: bool = True,
) -> dict[str, Any]:
    """Validate a ``report-capabilities-v1`` document; raises ``ValueError``."""
    problems: list[str] = []
    if not isinstance(document, Mapping):
        raise ValueError("report-capabilities 必须是 JSON 对象")
    if document.get("schema_version") != SCHEMA_REPORT_CAPABILITIES:
        problems.append(
            f"schema_version 必须是 {SCHEMA_REPORT_CAPABILITIES}，实际 {document.get('schema_version')!r}"
        )
    for name in (
        "case_id",
        "model_id",
        "ui_contract_version",
        "renderer_fingerprint",
        "capabilities_digest",
        "capabilities",
        "acceptance_refs",
    ):
        if name not in document:
            problems.append(f"缺少字段 {name!r}")
    capabilities = document.get("capabilities")
    if isinstance(capabilities, Mapping):
        if capabilities.get("schema_version") != recalc.SCHEMA_MODEL_CAPABILITIES:
            problems.append(f"capabilities.schema_version 应为 {recalc.SCHEMA_MODEL_CAPABILITIES}")
        for name in (
            "model_id",
            "ui_contract_version",
            "pages",
            "target_fields",
            "buff_controls",
            "windows",
            "modes",
            "recalculation",
            "informational",
        ):
            if name not in capabilities:
                problems.append(f"capabilities 缺少字段 {name!r}")
        recalculation = capabilities.get("recalculation")
        if isinstance(recalculation, Mapping):
            if recalculation.get("supported") is not True:
                problems.append("capabilities.recalculation.supported 应为 true")
            expected_version = capabilities_version(capabilities)
            if recalculation.get("capabilities_version") != expected_version:
                problems.append(
                    "capabilities.recalculation.capabilities_version 与内容不一致："
                    f"{recalculation.get('capabilities_version')!r} != {expected_version!r}"
                )
        else:
            problems.append("capabilities.recalculation 必须是对象")
        hit_policy = capabilities.get("hit_policy")
        if hit_policy is not None:
            if not isinstance(hit_policy, Mapping):
                problems.append("capabilities.hit_policy 必须是对象")
            else:
                kind = str(hit_policy.get("kind") or "")
                if kind not in ("not_applicable", "first_full_then_fraction"):
                    problems.append(
                        "capabilities.hit_policy.kind 必须是 not_applicable 或 "
                        "first_full_then_fraction"
                    )
                elif kind == "first_full_then_fraction":
                    for name in ("first_weight", "subsequent_weight"):
                        value = hit_policy.get(name)
                        if isinstance(value, bool) or not isinstance(value, (int, float)):
                            problems.append(f"capabilities.hit_policy.{name} 必须是数值")
                    hits = hit_policy.get("hits")
                    if not isinstance(hits, Mapping):
                        problems.append("capabilities.hit_policy.hits 必须是对象")
                    else:
                        bounds = {}
                        for name in ("min", "max", "default"):
                            value = hits.get(name)
                            if isinstance(value, bool) or not isinstance(value, int):
                                problems.append(
                                    f"capabilities.hit_policy.hits.{name} 必须是整数"
                                )
                            else:
                                bounds[name] = value
                        if len(bounds) == 3 and not (bounds["min"] <= bounds["default"] <= bounds["max"]):
                            problems.append(
                                "capabilities.hit_policy.hits 必须满足 min <= default <= max"
                            )
                    if not isinstance(hit_policy.get("custom_weights_supported"), bool):
                        problems.append(
                            "capabilities.hit_policy.custom_weights_supported 必须是布尔值"
                        )
        if document.get("capabilities_digest") != capabilities_digest(capabilities):
            problems.append("capabilities_digest 与 capabilities 内容不一致")
    else:
        problems.append("capabilities 必须是对象")
    refs = document.get("acceptance_refs")
    if not isinstance(refs, list) or not refs:
        problems.append("acceptance_refs 必须是非空数组")
    elif repo_root is not None and strict_acceptance_refs:
        problems.extend(validate_acceptance_refs(refs, repo_root=repo_root))
    cache = document.get("cache")
    if cache is not None:
        if not isinstance(cache, Mapping):
            problems.append("cache 必须是对象")
        else:
            for key in ("numeric_key", "report_key", "recompute_endpoint"):
                if not cache.get(key):
                    problems.append(f"cache 缺少字段 {key!r}")
            for key in ("numeric_key", "report_key"):
                value = cache.get(key)
                if value is not None and not recalc.is_sha256(value):
                    problems.append(f"cache.{key} 必须是 sha256")
    bindings = document.get("bindings")
    if bindings is not None:
        try:
            validated_bindings = recalc.validate_recalc_bindings(bindings)
        except recalc.RecalcRejected as exc:
            problems.append(
                "bindings 非法：" + "；".join(str(issue.get("message")) for issue in exc.issues)
            )
        else:
            if str(validated_bindings.get("case_id")) != str(document.get("case_id")):
                problems.append("bindings.case_id 与报告 case_id 不一致")
            if str(validated_bindings.get("model_id")) != str(document.get("model_id")):
                problems.append("bindings.model_id 与报告 model_id 不一致")
            if isinstance(capabilities, Mapping):
                if (
                    validated_bindings.get("capabilities_version")
                    != (capabilities.get("recalculation") or {}).get("capabilities_version")
                ):
                    problems.append("bindings.capabilities_version 与 capabilities 不一致")
            if str(validated_bindings.get("ui_contract_version")) != str(
                document.get("ui_contract_version")
            ):
                problems.append("bindings.ui_contract_version 与报告不一致")
            if str(validated_bindings.get("renderer_fingerprint")) != str(
                document.get("renderer_fingerprint")
            ):
                problems.append("bindings.renderer_fingerprint 与报告不一致")
    profile_states = document.get("profile_states")
    if profile_states is not None and not isinstance(profile_states, Mapping):
        problems.append("profile_states 必须是对象")
    if repo_root is not None and "renderer_fingerprint" in document:
        expected = renderer_fingerprint(repo_root)
        if str(document.get("renderer_fingerprint")) != expected:
            problems.append("renderer_fingerprint 与当前渲染器源码不一致（报告缓存应失效）")
    if problems:
        raise ValueError("report-capabilities 校验失败：" + "；".join(problems))
    return dict(document)


def write_report_capabilities(path: str | Path, document: Mapping[str, Any]) -> Path:
    """Write ``report-capabilities.json`` atomically (UTF-8, repository-relative paths)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(dict(document), ensure_ascii=False, indent=2) + "\n"
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(target)
    return target


def read_report_capabilities(
    path: str | Path, *, repo_root: Optional[str | Path] = None
) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        raise ValueError(f"report-capabilities 不存在：{target.name}")
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"report-capabilities 不是合法 JSON（{target.name}）：{exc}") from exc
    return validate_report_capabilities(document, repo_root=repo_root)


def capabilities_path_for_run(run_dir: str | Path) -> Path:
    return Path(run_dir) / CAPABILITIES_FILE


def capabilities_from_run_dir(
    run_dir: str | Path,
    *,
    case_id: str,
    model_id: str,
    repo_root: str | Path,
    registry_path: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Build capabilities from an existing run directory (``result.json`` + registry)."""
    directory = Path(run_dir)
    result = _read_json(directory / "result.json")
    registry = _read_json(registry_path) if registry_path else None
    return build_report_capabilities(
        case_id=case_id,
        model_id=model_id,
        repo_root=repo_root,
        run_id=directory.name,
        report_path=None,
        result=result,
        registry=registry,
    )


# ---------------------------------------------------------------------------
# renderer wiring (delegates to recalc.render_with_context)
# ---------------------------------------------------------------------------


def _capabilities_models(capabilities: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the model-capabilities documents inside a wrapper or a bare document."""
    nested = capabilities.get("capabilities")
    if isinstance(nested, Mapping):
        return [nested]
    if capabilities.get("model_id"):
        return [capabilities]
    return []


def recalc_bindings_for_report(
    *,
    capabilities: Mapping[str, Any],
    report_ref: Mapping[str, Any],
    input_fingerprints: Optional[Mapping[str, str]] = None,
    renderer_fingerprint: Optional[str] = None,
    recompute_endpoint: Optional[str] = None,
) -> dict[str, Any]:
    """Build the ``le-recalc-bindings-v1`` page payload (LIVE-04/07).

    Single builder shared by run/reproduce post-processing, the services and the
    HTTP handler: the page JS reads exactly one ``<script id="le-recalc-bindings">``
    block and uses it for the complete request + response binding checks.
    """
    models = _capabilities_models(capabilities)
    if not models:
        raise ValueError("capabilities 缺少模型能力声明")
    model = models[0]
    version = capabilities_version(model)
    if not version:
        raise ValueError("capabilities 缺少 capabilities_version")
    fingerprints = dict(input_fingerprints or report_ref.get("input_fingerprints") or {})
    fingerprint_value = (
        renderer_fingerprint
        or capabilities.get("renderer_fingerprint")
        or model.get("renderer_fingerprint")
        or report_ref.get("renderer_fingerprint")
    )
    if not fingerprint_value:
        raise ValueError("缺少 renderer_fingerprint")
    case_id = capabilities.get("case_id") or report_ref.get("case_id")
    model_id = capabilities.get("model_id") or model.get("model_id")
    return recalc.build_recalc_bindings(
        case_id=str(case_id),
        model_id=str(model_id),
        capabilities_version=version,
        ui_contract_version=str(
            capabilities.get("ui_contract_version") or model.get("ui_contract_version") or UI_CONTRACT_VERSION
        ),
        renderer_fingerprint=str(fingerprint_value),
        report_ref=report_ref,
        input_fingerprints=fingerprints,
        recompute_endpoint=recompute_endpoint or report_ref.get("recompute_endpoint"),
    )


def render_report(
    result: Mapping[str, Any],
    *,
    model_id: str,
    recalc_endpoint: Optional[str] = None,
    capabilities: Optional[Mapping[str, Any]] = None,
    bindings: Optional[Mapping[str, Any]] = None,
    report_ref: Optional[Mapping[str, Any]] = None,
    input_fingerprints: Optional[Mapping[str, str]] = None,
    renderer_fingerprint: Optional[str] = None,
) -> str:
    """Render one report through the shared renderer with endpoint/capabilities/bindings.

    Uses the P3R-A/P3R-B2 renderer interface (``recalc_endpoint`` /
    ``capabilities`` / ``bindings`` keyword arguments) and falls back to the
    current single-argument renderer in the staged implementation.  ``bindings``
    is the single page-binding block (never injected twice); when omitted it is
    built from ``report_ref`` + ``capabilities``.
    """
    canonical = _canonical_model_id(model_id)
    binding_payload = bindings
    if binding_payload is None and report_ref is not None and capabilities is not None:
        binding_payload = recalc_bindings_for_report(
            capabilities=capabilities,
            report_ref=report_ref,
            input_fingerprints=input_fingerprints,
            renderer_fingerprint=renderer_fingerprint,
            recompute_endpoint=recalc_endpoint,
        )
    if canonical == "black-hole":
        from .report_ui import render as renderer
    else:
        from .hit_report import render_hit_html as renderer
    html = recalc.render_with_context(
        renderer,
        dict(result),
        recalc_endpoint=recalc_endpoint,
        capabilities=capabilities,
        bindings=binding_payload,
    )
    if not isinstance(html, str) or not html:
        raise ValueError("渲染器未返回 HTML")
    return html


def rerender_report_html(
    result: Mapping[str, Any],
    *,
    model_id: str,
    recalc_endpoint: Optional[str],
    capabilities: Optional[Mapping[str, Any]],
    out_path: str | Path,
    bindings: Optional[Mapping[str, Any]] = None,
    report_ref: Optional[Mapping[str, Any]] = None,
    input_fingerprints: Optional[Mapping[str, str]] = None,
    renderer_fingerprint: Optional[str] = None,
) -> Path:
    """Re-render an existing result with the actual endpoint/bindings and write the HTML."""
    html = render_report(
        result,
        model_id=model_id,
        recalc_endpoint=recalc_endpoint,
        capabilities=capabilities,
        bindings=bindings,
        report_ref=report_ref,
        input_fingerprints=input_fingerprints,
        renderer_fingerprint=renderer_fingerprint,
    )
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(html, encoding="utf-8", newline="\n")
    temporary.replace(target)
    return target


__all__ = [
    "CAPABILITIES_FILE",
    "CAPABILITIES_VERSION_PREFIX",
    "MODEL_ALIASES",
    "MODEL_BASELINE_PROFILES",
    "MODEL_HIT_POLICY",
    "MODEL_BUFF_CONTROLS",
    "MODEL_INFORMATIONAL",
    "MODEL_MODEL_PARAMETERS",
    "MODEL_MODES",
    "MODEL_PAGES",
    "MODEL_SCENARIO_FIELDS",
    "MODEL_TARGET_FIELDS",
    "MODEL_WINDOWS",
    "RENDERER_SOURCE_PATHS",
    "SCHEMA_REPORT_CAPABILITIES",
    "UI_CONTRACT_VERSION",
    "acceptance_refs",
    "build_report_capabilities",
    "capabilities_digest",
    "capabilities_for_model",
    "capabilities_from_run_dir",
    "capabilities_path_for_run",
    "capabilities_version",
    "read_report_capabilities",
    "render_report",
    "renderer_fingerprint",
    "renderer_sources",
    "rerender_report_html",
    "validate_acceptance_refs",
    "validate_report_capabilities",
    "write_report_capabilities",
]
