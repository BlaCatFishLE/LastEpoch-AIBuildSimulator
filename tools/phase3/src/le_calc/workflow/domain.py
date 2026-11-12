"""P2 domain adapters: ``resolve_build`` / ``simulate`` / ``validate_result``.

These are the independent, JSON-in/JSON-out entry points described by
:file:`docs/contracts/workflow-interface.md` §5.  They:

* do **not** depend on the CLI, chat context or a hidden "current case" — every
  path and adapter is explicit (``case_ref`` / ``input_refs`` / ``knowledge`` /
  ``context``);
* do **not** re-implement the damage engine: ``simulate`` calls the same
  ``le_calc.report`` / ``le_calc.hit_report`` functions as the CLI, and
  ``validate_result`` mirrors the expected-summary comparison semantics of
  ``scripts/le_commands.py`` without importing ``scripts/``;
* keep the numeric result path untouched — only orchestration/verification code
  lives here.

Payload schemas live under ``tools/phase3/schemas/workflow-v1/``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from . import checklist as checklist_module
from . import contract
from . import expected_check
from .contract import (
    ContractError,
    SCHEMA_ANALYSIS_PLAN,
    SCHEMA_COVERAGE,
    SCHEMA_RESOLVE_BUILD_REQUEST,
    SCHEMA_RESOLVE_BUILD_RESULT,
    SCHEMA_SIMULATE_REQUEST,
    SCHEMA_SIMULATE_RESULT,
    deterministic_id,
    fingerprint,
    make_artifact_ref,
    make_cache_info,
    make_issue,
    normalize_relative,
    sha256_file,
)

#: Default repository root is derived from this file's location; callers can
#: override it with ``context.repo_root`` / ``root=``.
DEFAULT_ROOT = Path(__file__).resolve().parents[5]

MODEL_BLACK_HOLE = "black-hole"
MODEL_HIT = "hit"
MODEL_IDS = {MODEL_BLACK_HOLE: "bh-event", MODEL_HIT: "hit"}
MODEL_RESULT_SCHEMAS = {
    MODEL_BLACK_HOLE: "black-hole-stage1-result-v1",
    MODEL_HIT: "le-hit-result-v1",
}

DEFAULT_KNOWLEDGE_DIR = "knowledge"
DEFAULT_INDEX_PATH = "data/cache/knowledge.sqlite"

#: Mirror of the CLI's one-factor buff variants (policy, not calculation).  Kept
#: here so ``simulate`` produces the same ``result.json`` blocks as
#: ``le_calc hit-run``; unify with the CLI in P3.
HIT_SENSITIVITY_VARIANTS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("ignited", "目标 ignited（Scorching Tide +60% more / Chaos Flare crit ×1.2）", {"ignited": True}),
    ("frenzy_active", "Frenzy active（+45.6% increased attack speed / +8 added melee fire）", {"frenzy_active": True}),
    ("ambition_20", "Ambition 20 层（+40% more Fire）", {"ambition_stacks": 20}),
    (
        "blade_weaving_consume_8",
        "Blade Weaving 消耗 8 层（+120% more Elemental）",
        {"consume_blade_weaving": True, "blade_weaving_stacks": 8},
    ),
    (
        "rhythm_consume",
        "Rhythm of Fire 消耗单次（+130% more，单次命中口径）",
        {"consume_rhythm_of_fire": True},
    ),
)

RESULT_REQUIRED_KEYS = {
    MODEL_BLACK_HOLE: (
        "schema", "run", "input", "scope", "scenario", "snapshot",
        "source_ledger", "rule_ledger", "single_cast", "loops", "issues",
    ),
    MODEL_HIT: (
        "schema", "run", "input", "scope", "scenario", "snapshot",
        "single_hit", "dps", "hits_per_cast", "consistency", "issues",
    ),
}

_ALLOWED_BUILD_KINDS = ("mapping", "parameter_rule", "handler", "bounded_assumption")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _root(context: Any = None, root: Optional[str | Path] = None) -> Path:
    if root is not None:
        return Path(root).resolve()
    if context is not None:
        repo = getattr(context, "repo_root", None)
        if repo is not None:
            return Path(repo).resolve()
    return DEFAULT_ROOT


def _resolve_path(value: str | Path, root: Path) -> Path:
    p = Path(value)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    try:
        p.relative_to(root)
    except ValueError as exc:
        raise ContractError(f"path outside repository root: {value}") from exc
    return p


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"expected JSON object: {path}")
    return payload


def _load_case(case_ref: str | Path, root: Path) -> dict[str, Any]:
    path = _resolve_path(case_ref, root)
    case = _read_json(path)
    if case.get("schema") != "le-case-v1":
        raise ContractError(f"case schema must be le-case-v1, got {case.get('schema')!r}")
    case["_path"] = _relative(path, root)
    return case


def _case_model(case: Mapping[str, Any]) -> str:
    return str(case.get("model") or MODEL_BLACK_HOLE).strip().lower()


def _case_value(case: Mapping[str, Any], dotted: str) -> Any:
    """Read a dotted case field (``inputs.snapshot`` / ``ruleset.path`` ...)."""

    node: Any = case
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _case_path(case: Mapping[str, Any], dotted: str) -> Optional[str]:
    value = _case_value(case, dotted)
    return str(value) if isinstance(value, (str, Path)) and str(value) else None


def _sha256_or_none(path: Optional[Path]) -> Optional[str]:
    return sha256_file(path) if path is not None and path.is_file() else None


def _input_refs(request: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Map optional ``role`` -> artifact_ref; tolerate refs without a role."""

    mapped: dict[str, dict[str, Any]] = {}
    for ref in request.get("input_refs") or []:
        if not isinstance(ref, Mapping):
            raise ContractError("input_refs entries must be objects")
        role = ref.get("role") or ref.get("artifact_id")
        if role:
            mapped[str(role)] = dict(ref)
    return mapped


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _now(context: Any) -> str:
    clock = None
    if context is not None:
        adapters = getattr(context, "adapters", None) or {}
        clock = adapters.get("clock")
    if callable(clock):
        value = clock()
        return value if isinstance(value, str) else str(value)
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _portable_path(value: Any, root: Path) -> str:
    """Repository-relative display for a configured path; never leak machine paths."""

    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    p = Path(text)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return "<external-path>"
    return text.replace("\\", "/")


def _knowledge_settings(request: Mapping[str, Any]) -> dict[str, Any]:
    block = request.get("knowledge") or {}
    if not isinstance(block, Mapping):
        raise ContractError("knowledge must be an object")
    return {
        "knowledge_dir": str(block.get("knowledge_dir") or DEFAULT_KNOWLEDGE_DIR),
        "index_path": str(block.get("index_path") or DEFAULT_INDEX_PATH),
        "root": block.get("root"),
        "lookup": block.get("lookup"),
    }


# ---------------------------------------------------------------------------
# selected build sources (read-only extraction from calculation-input)
# ---------------------------------------------------------------------------
def _selected_nodes(node_entries: Iterable[Mapping[str, Any]]) -> list[str]:
    return sorted(
        str(node.get("node_id"))
        for node in node_entries
        if int(node.get("points") or 0) > 0
    )


def _root_nodes(node_entries: Iterable[Mapping[str, Any]]) -> list[str]:
    return sorted(
        str(node.get("node_id"))
        for node in node_entries
        if str(node.get("node_type") or "") == "root"
    )


def _build_scope(case: Mapping[str, Any], calc_input: Mapping[str, Any], root: Path) -> dict[str, Any]:
    skills = calc_input.get("skills") or {}
    specializations = []
    for spec in skills.get("specializations") or []:
        tree = spec.get("tree") or {}
        owner = spec.get("owner_skill") or {}
        specializations.append(
            {
                "slot": spec.get("slot_number"),
                "tree": tree.get("name"),
                "tree_entity_id": tree.get("entity_id"),
                "owner_skill": owner.get("name"),
                "owner_entity_id": owner.get("entity_id"),
                "points_total": spec.get("points_total"),
                "node_entries": spec.get("node_entries"),
                "selected_nodes": _selected_nodes(spec.get("nodes") or []),
                "root_nodes": _root_nodes(spec.get("nodes") or []),
            }
        )
    passive = calc_input.get("passive_tree") or {}
    scope_block = calc_input.get("scope") or {}
    return {
        "skill_trees": specializations,
        "passive_tree": {
            "name": (passive.get("tree") or {}).get("name"),
            "entity_id": (passive.get("tree") or {}).get("entity_id"),
            "points_total": passive.get("points_total"),
            "selected_nodes": _selected_nodes(passive.get("nodes") or []),
        },
        "equipment_items": len(calc_input.get("items") or []),
        "idols": len(calc_input.get("idols") or []),
        "blessings": len(calc_input.get("blessings") or {}),
        "excluded_features": list(scope_block.get("excluded_features") or []),
        "scope_version": scope_block.get("scope_version"),
        "inputs": {
            "calculation_input": _case_path(case, "inputs.calculation_input"),
            "snapshot": _case_path(case, "inputs.snapshot"),
            "bd": _case_path(case, "inputs.bd"),
        },
    }


def _find_main_specialization(scope: Mapping[str, Any], ability_id: str) -> Optional[dict[str, Any]]:
    needle = ability_id.strip().casefold()
    for tree in scope.get("skill_trees") or []:
        for key in ("tree_entity_id", "owner_entity_id"):
            value = str(tree.get(key) or "").casefold()
            if value.endswith(f":{needle}"):
                return tree
        tree_name = str(tree.get("tree") or "").casefold().replace(" ", "_")
        if tree_name == needle:
            return tree
    return None


def _scenario_state_ids(case: Mapping[str, Any], root: Path) -> list[str]:
    """Condition ids that are true in the shipped scenario (for applicability)."""

    scenario_rel = _case_path(case, "inputs.scenario")
    if not scenario_rel:
        return []
    path = _resolve_path(scenario_rel, root)
    if not path.is_file():
        return []
    scenario = _read_json(path)
    true_states: set[str] = set()

    def absorb(states: Any) -> None:
        if not isinstance(states, Mapping):
            return
        for key, value in states.items():
            if value is True:
                true_states.add(str(key))

    absorb(scenario.get("states"))
    for section in ("single_hit", "dps"):
        block = scenario.get(section)
        if isinstance(block, Mapping):
            absorb(block.get("states"))
    profiles = scenario.get("profiles") or {}
    profile_id = str(scenario.get("baseline_profile") or "")
    if profile_id in profiles and isinstance(profiles[profile_id], Mapping):
        absorb(profiles[profile_id].get("states"))
    return sorted(true_states)


class _KnowledgeAdapter:
    """Lazy `le_knowledge` wrapper that never crashes the resolver on import."""

    def __init__(self, settings: Mapping[str, Any], root: Path):
        self.settings = settings
        self.root = root
        self._search: Optional[Callable[..., dict[str, Any]]] = None
        self.error: Optional[str] = None

    def _resolve(self) -> Optional[Callable[..., dict[str, Any]]]:
        if self._search is not None:
            return self._search
        lookup = self.settings.get("lookup")
        if callable(lookup):
            self._search = lookup
            return self._search
        try:  # pragma: no cover - depends on the local knowledge install
            import le_knowledge  # type: ignore
        except Exception as exc:  # noqa: BLE001
            self.error = f"le_knowledge import failed: {exc}"
            return None
        search = getattr(le_knowledge, "search", None)
        if not callable(search):  # pragma: no cover - defensive
            self.error = "le_knowledge.search is not callable"
            return None
        self._search = search
        return search

    def search(self, query: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        lookup = self.settings.get("lookup")
        if callable(lookup):
            try:
                result = lookup(dict(query))
            except Exception as exc:  # noqa: BLE001
                self.error = f"injected knowledge lookup failed: {exc}"
                return None
            if not isinstance(result, dict):  # pragma: no cover - defensive
                self.error = "injected knowledge lookup returned a non-object"
            return result
        search = self._resolve()
        if search is None:
            return None
        kwargs: dict[str, Any] = {}
        index_path = self.settings.get("index_path")
        if index_path:
            p = Path(str(index_path))
            kwargs["index_path"] = str(p if p.is_absolute() else self.root / p)
        knowledge_dir = self.settings.get("knowledge_dir")
        if knowledge_dir:
            p = Path(str(knowledge_dir))
            kwargs["knowledge_dir"] = str(p if p.is_absolute() else self.root / p)
        kwargs["root"] = str(self.root)
        try:
            result = search(dict(query), **kwargs)
        except Exception as exc:  # noqa: BLE001
            self.error = f"le_knowledge search failed: {exc}"
            return None
        if not isinstance(result, dict):  # pragma: no cover - defensive
            self.error = "le_knowledge search returned a non-object"
            return None
        return result


# ---------------------------------------------------------------------------
# resolve_build
# ---------------------------------------------------------------------------
def resolve_build(request: Mapping[str, Any], *, context: Any = None,
                  root: Optional[str | Path] = None) -> dict[str, Any]:
    """Resolve a frozen build into an analysis plan, coverage and gaps.

    ``request`` (``le-resolve-build-request-v1``)::

        {
          "schema_version": "le-resolve-build-request-v1",
          "case_id": "black-hole",
          "main_skill": {"ability_id": "bh2", "name": "Black Hole"},
          "input_refs": [...],                 # optional artifact_ref[] (role aware)
          "compatibility_baseline": {...},     # optional metadata only
          "case_ref": "examples/<case>/case.json",   # required for P2
          "knowledge": {"knowledge_dir": "knowledge",
                        "index_path": "data/cache/knowledge.sqlite"},  # optional
          "context": {...}                     # optional query context overrides
        }

    The function never starts research and never modifies rules/artifacts; missing
    mappings become ``gaps``.  When ``le_knowledge`` is unavailable it returns a
    ``needs_input`` result with an explicit issue instead of raising.
    """

    started = time.perf_counter()
    root_path = _root(context, root)
    if request.get("schema_version") not in (None, SCHEMA_RESOLVE_BUILD_REQUEST):
        raise ContractError(
            f"unsupported schema_version: {request.get('schema_version')!r} "
            f"(expected {SCHEMA_RESOLVE_BUILD_REQUEST})"
        )
    case_ref = request.get("case_ref")
    if not case_ref:
        raise ContractError("case_ref is required (repository-relative case.json)")
    case = _load_case(case_ref, root_path)
    case_id = str(request.get("case_id") or case.get("case_id"))
    if case_id != case.get("case_id"):
        raise ContractError(f"case_id mismatch: request {case_id!r} vs case {case.get('case_id')!r}")
    model = _case_model(case)

    main_skill = dict(request.get("main_skill") or {})
    ability_id = str(main_skill.get("ability_id") or "").strip()
    if not ability_id:
        raise ContractError("main_skill.ability_id is required")

    refs = _input_refs(request)
    calc_rel = (refs.get("calculation_input") or {}).get("path") or _case_path(
        case, "inputs.calculation_input"
    )
    snapshot_rel = (refs.get("snapshot") or {}).get("path") or _case_path(case, "inputs.snapshot")
    if not calc_rel:
        raise ContractError("case has no inputs.calculation_input and no calculation_input ref")
    calc_path = _resolve_path(str(calc_rel), root_path)
    calc_input = _read_json(calc_path)
    scope = _build_scope(case, calc_input, root_path)

    main_spec = _find_main_specialization(scope, ability_id)
    if not ability_id and main_spec:  # pragma: no cover - guarded above
        ability_id = str(main_spec.get("tree_entity_id") or "").rsplit(":", 1)[-1]

    issues: list[dict[str, Any]] = []
    knowledge = _KnowledgeAdapter(_knowledge_settings(request), root_path)
    context_override = request.get("context") or {}
    condition_ids = sorted(
        set(_scenario_state_ids(case, root_path))
        | set(str(item) for item in context_override.get("conditions") or [])
    )
    from .components import collect_components, component_queries, bind_instances
    component_started = time.perf_counter()
    build_components = collect_components(calc_input)
    available_abilities = {r["selector"].split(":", 1)[1] for r in build_components
                           if r["selector"].startswith("ability:")}
    collect_ms = (time.perf_counter() - component_started) * 1000
    selected_nodes = list((main_spec or {}).get("selected_nodes") or [])
    root_nodes = list((main_spec or {}).get("root_nodes") or [])
    node_context = sorted(
        {f"node:{ability_id}:{node_id}" for node_id in selected_nodes + root_nodes}
        | set(str(item) for item in context_override.get("nodes") or [])
    )

    queries: list[dict[str, Any]] = [
        {
            "query_id": "main-skill",
            "term": f"ability:{ability_id}",
            "kind": "ability_id",
        }
    ]
    for node_id in selected_nodes:
        queries.append(
            {
                "query_id": f"node-{node_id}",
                "term": f"node:{ability_id}:{node_id}",
                "kind": "node_id",
            }
        )
    queries += component_queries(build_components)
    # Shared aggregation operators are explicit dependencies, not keyword guesses.
    queries += [{"query_id": name, "term": "mechanism:" + name, "kind": "id"}
                for name in ("common-tag-match", "common-more-groups",
                             "common-increased-aggregate", "common-ledger")]
    node_context = sorted(set(node_context) | {r["selector"] for r in build_components
                                             if r["selector"].startswith("node:")})
    query_started = time.perf_counter()
    query_payload = {
        "queries": queries,
        "filters": {},
        "context": {"conditions": condition_ids, "nodes": node_context, "exhaustive": True},
        "limit": max(50, len(queries) * 10),
    }
    knowledge_result = knowledge.search(query_payload)
    query_ms = (time.perf_counter() - query_started) * 1000
    bind_started = time.perf_counter()
    entries: list[dict[str, Any]] = []
    unmatched_queries: list[dict[str, Any]] = []
    index_digest = None
    if knowledge_result is None:
        issues.append(
            make_issue(
                "knowledge_unavailable",
                knowledge.error or "knowledge index is not available",
                severity=contract.SEVERITY_ERROR,
                related_ids=[case_id, ability_id],
                required_input={
                    "kind": "knowledge_index",
                    "artifact_hint": f"{DEFAULT_KNOWLEDGE_DIR}/mechanisms/",
                },
            )
        )
    else:
        entries = list(knowledge_result.get("entries") or [])
        unmatched_queries = list(knowledge_result.get("unmatched_queries") or [])
        index_digest = knowledge_result.get("index_digest")

    bound: list[dict[str, Any]] = []
    unresolved_entries: list[dict[str, Any]] = []
    excluded_entries: list[dict[str, Any]] = []
    out_of_scope_entries: list[dict[str, Any]] = []
    template_entry: Optional[dict[str, Any]] = None

    for entry in entries:
        applicable = (entry.get("applicability") or {}).get("status")
        exact = any(
            record.get("match_quality") == contract.MATCH_EXACT
            for record in entry.get("matched_queries") or []
        )
        candidate_only = (
            not exact
            and bool(entry.get("matched_queries"))
        )
        case_scope = entry.get("case_scope") or []
        shared = entry.get("reuse_mode") == "shared"
        required_abilities = set(entry.get("required_abilities") or [])
        if shared and required_abilities and not required_abilities.intersection(available_abilities):
            excluded_entries.append({"id": entry.get("mechanism_id"), "reason": "来源技能不在当前构筑"})
            continue
        if case_scope and case_id not in case_scope and not shared:
            out_of_scope_entries.append(
                {
                    "id": entry.get("mechanism_id"),
                    "entry_kind": entry.get("entry_kind"),
                    "reason": f"机制 case_scope={case_scope} 不包含案例 {case_id}",
                    "components": list(entry.get("components") or []),
                }
            )
            continue
        if entry.get("entry_kind") == "skill_template":
            if (exact and applicable != contract.APPLICABILITY_NOT_APPLICABLE
                    and str(entry.get("ability_id")) == ability_id):
                template_entry = entry
            continue
        if exact and applicable == contract.APPLICABILITY_SATISFIED:
            bound.append(
                {
                    "mechanism_id": entry.get("mechanism_id"),
                    "reuse_mode": entry.get("reuse_mode"),
                    "known_limitations": entry.get("unresolved") or [],
                    "matched_instances": bind_instances(entry, build_components),
                    "revision": entry.get("revision"),
                    "entry_kind": entry.get("entry_kind"),
                    "evidence_state": entry.get("evidence_state"),
                    "implementation_state": entry.get("implementation_state"),
                    "components": list(entry.get("components") or []),
                    "tags": list(entry.get("tags") or []),
                    "conditions": list(entry.get("conditions") or []),
                    "rule_refs": list(entry.get("rule_refs") or []),
                    "implementation_refs": list(entry.get("implementation_refs") or []),
                    "source": entry.get("source") or {},
                }
            )
        elif exact and applicable == contract.APPLICABILITY_UNKNOWN:
            unresolved_entries.append(
                {
                    "id": entry.get("mechanism_id"),
                    "entry_kind": entry.get("entry_kind"),
                    "reason": "必要条件未在请求上下文中给出",
                    "missing_conditions": list(
                        (entry.get("applicability") or {}).get("missing_conditions") or []
                    ),
                }
            )
        elif exact and applicable == contract.APPLICABILITY_NOT_APPLICABLE:
            excluded_entries.append(
                {
                    "id": entry.get("mechanism_id"),
                    "reason": "必要条件明确不满足",
                    "missing_conditions": list(
                        (entry.get("applicability") or {}).get("missing_conditions") or []
                    ),
                }
            )
        elif candidate_only:
            unresolved_entries.append(
                {
                    "id": entry.get("mechanism_id"),
                    "entry_kind": entry.get("entry_kind"),
                    "reason": "仅有 candidate 匹配，需人工确认映射",
                }
            )

    template_document: dict[str, Any] = {}
    if template_entry and (template_entry.get("source") or {}).get("path"):
        source_path = _resolve_path(str(template_entry["source"]["path"]), root_path)
        if source_path.is_file():
            template_document = _read_json(source_path)

    # ---- checklist / analysis plan -------------------------------------
    template_blocks = [
        str(block.get("block"))
        for block in (template_document.get("calculation_blocks") or [])
        if isinstance(block, Mapping)
    ]
    bound_components: dict[str, list[str]] = {}
    for mechanism in bound:
        for component in mechanism.get("components") or []:
            bound_components.setdefault(component, []).append(str(mechanism["mechanism_id"]))
    plan_items: list[dict[str, Any]] = []
    for item in checklist_module.checklist():
        mechanisms = sorted(
            {
                mechanism_id
                for component in item["components"]
                for mechanism_id in bound_components.get(component, [])
            }
        )
        blocks = [block for block in item["calculation_blocks"] if block in template_blocks]
        model_has_block = bool(blocks)
        if item["calculation_blocks"] and template_document and not model_has_block:
            status = "not_applicable"
            reason = f"技能模板未声明计算块 {item['calculation_blocks']}（模型不适用）"
        elif mechanisms:
            states = {
                str(m.get("implementation_state"))
                for m in bound
                if str(m.get("mechanism_id")) in mechanisms
            }
            if states and states <= {"validated", "reusable"}:
                status = "supported"
                reason = "已由绑定机制覆盖"
            else:
                status = "partial"
                reason = "绑定机制中存在 draft/not_implemented"
        elif model_has_block:
            status = "supported"
            reason = "由技能模板计算块覆盖"
        else:
            status = "partial"
            reason = "暂无绑定机制，需补充映射或研究"
        plan_items.append(
            {
                **item,
                "status": status,
                "reason": reason,
                "mechanisms": mechanisms,
                "model_blocks": blocks,
            }
        )

    plan_components = sorted(
        {
            f"{component.get('component_id')} ({component.get('damage_type')}/{component.get('kind')})"
            for component in (template_document.get("components") or [])
            if isinstance(component, Mapping)
        }
        | set(bound_components)
    )

    # ---- gaps / coverage ----------------------------------------------
    gaps: list[dict[str, Any]] = []
    gap_seen: set[str] = set()

    def add_gap(
        kind: str,
        question: str,
        *,
        related_ids: Sequence[str] = (),
        severity: str = contract.SEVERITY_WARNING,
        blocking: bool = False,
        components: Sequence[str] = (),
        mechanism_refs: Sequence[str] = (),
        researched_already: Sequence[str] = (),
        deliverable_kind: str = "mapping",
    ) -> None:
        payload = {"kind": kind, "question": question, "related_ids": list(related_ids)}
        gap_id = deterministic_id("gap", payload)
        if gap_id in gap_seen:
            return
        gap_seen.add(gap_id)
        if deliverable_kind not in _ALLOWED_BUILD_KINDS:
            deliverable_kind = "mapping"
        gaps.append(
            {
                "gap_id": gap_id,
                "kind": kind,
                "severity": severity,
                "blocking": bool(blocking),
                "question": question,
                "related_ids": list(related_ids),
                "source_refs": [
                    {
                        "kind": "case_input",
                        "locator": _case_path(case, "inputs.calculation_input") or "",
                    }
                ],
                "related_mechanism_refs": list(mechanism_refs),
                "researched_already": list(researched_already),
                "affected": {
                    "components": list(components),
                    "blocks_target": bool(blocking),
                },
                "deliverable_kind": deliverable_kind,
                "verification_required": [
                    "正向：新增映射/参数后重跑 resolve_build 与 simulate，数值或 coverage 按预期变化",
                    "边界：未满足条件/错误输入必须返回 unknown/not_applicable 而不是静默放行",
                ],
            }
        )

    # Input adapter remains authoritative for ordinary property mappings.
    # Existing ledger exclusions distinguish harmless stats from actual unknowns.
    component_coverage = []
    snapshot_path = _resolve_path(snapshot_rel, root_path) if snapshot_rel else None
    snapshot_data = _read_json(snapshot_path) if snapshot_path and snapshot_path.is_file() else {}
    ledger = snapshot_data.get("contributions") or []
    instance_knowledge = {}
    for mechanism in bound:
        for instance in mechanism.get("matched_instances", []):
            instance_knowledge.setdefault(instance["instance_id"], []).append(mechanism["mechanism_id"])
    seen_instances = set()
    for row in build_components:
        iid = row["instance_id"]
        if iid in seen_instances:
            continue
        seen_instances.add(iid)
        matches = instance_knowledge.get(iid, [])
        existing = [c for c in ledger if c.get("contribution_id") == "mod:" + str(iid)]
        known = any(c.get("stat_key") not in (None, "unsupported", "unmapped", "unknown") for c in existing)
        status = "mapped" if matches else ("existing_adapter" if known else "unclassified")
        component_coverage.append({"instance_id": iid, "selector": row["selector"],
                                   "status": status, "mechanisms": matches})
        if existing and not known and any(c.get("status") == "unresolved" for c in existing):
            add_gap("missing_component_mapping", f"当前输入适配器尚未解释 {iid}",
                    related_ids=[str(iid)], blocking=False, severity=contract.SEVERITY_WARNING)

    for raw in unmatched_queries:
        # Ordinary stats already use the calculator's property mapping; absence
        # of a separate research article is not a missing calculation rule.
        if str(raw.get("query_id", "")).startswith("component:"):
            continue
        add_gap(
            "missing_mechanism_mapping",
            f"查询项 {raw.get('term')!r} 在知识库中没有匹配机制条目",
            related_ids=[str(raw.get("term") or "")],
            severity=contract.SEVERITY_WARNING,
            blocking=False,
        )
    for item in unresolved_entries:
        add_gap(
            "unresolved_applicability",
            f"机制 {item.get('id')} 的必要条件未满足/未提供：{item.get('missing_conditions')}",
            related_ids=[str(item.get("id") or "")],
            severity=contract.SEVERITY_WARNING,
            blocking=False,
            mechanism_refs=[str(item.get("id") or "")],
        )
    for mechanism in bound:
        state = str(mechanism.get("implementation_state"))
        if state in ("draft", "not_implemented"):
            add_gap(
                "partial_implementation",
                f"机制 {mechanism.get('mechanism_id')} 的 implementation_state={state}，仅部分进入计算",
                related_ids=[str(mechanism.get("mechanism_id") or "")],
                severity=contract.SEVERITY_WARNING,
                blocking=False,
                components=list(mechanism.get("components") or []),
                mechanism_refs=[str(mechanism.get("mechanism_id") or "")],
                deliverable_kind="handler",
            )
        evidence_state = str(mechanism.get("evidence_state"))
        if evidence_state in ("assumed", "unresolved") and mechanism.get("reuse_mode") != "shared":
            add_gap(
                "evidence_not_confirmed",
                f"机制 {mechanism.get('mechanism_id')} 的 evidence_state={evidence_state}",
                related_ids=[str(mechanism.get("mechanism_id") or "")],
                severity=contract.SEVERITY_INFO,
                blocking=False,
                components=list(mechanism.get("components") or []),
                mechanism_refs=[str(mechanism.get("mechanism_id") or "")],
                researched_already=[
                    str((mechanism.get("source") or {}).get("path") or ""),
                ],
                deliverable_kind="bounded_assumption",
            )
    if not template_entry:
        add_gap(
            "missing_skill_template",
            f"知识库中没有技能 {ability_id} 的可用技能模板（entry_kind=skill_template）",
            related_ids=[ability_id],
            severity=contract.SEVERITY_ERROR,
            blocking=True,
            deliverable_kind="mapping",
        )
    for feature in scope.get("excluded_features") or []:
        add_gap(
            "out_of_scope_feature",
            f"构建范围排除了 {feature.get('feature')}：{feature.get('reason')}",
            related_ids=[str(feature.get("feature") or "")],
            severity=contract.SEVERITY_INFO,
            blocking=False,
            researched_already=["calculation-input scope.excluded_features"],
            deliverable_kind="bounded_assumption",
        )

    readiness = _resolve_readiness(issues, gaps, template_entry, unresolved_entries,
                                   out_of_scope_entries)

    input_sha = fingerprint(
        {
            "case_id": case_id,
            "main_skill": ability_id,
            "calculation_input_sha256": sha256_file(calc_path),
            "snapshot_sha256": _sha256_or_none(
                _resolve_path(str(snapshot_rel), root_path) if snapshot_rel else None
            ),
            "compatibility_baseline": request.get("compatibility_baseline") or {},
            "knowledge_index_digest": index_digest,
        }
    )
    plan = {
        "schema_version": SCHEMA_ANALYSIS_PLAN,
        "plan_id": deterministic_id("analysis_plan", {"case_id": case_id, "ability_id": ability_id}),
        "case_id": case_id,
        "main_skill": {
            "ability_id": ability_id,
            "name": (template_document.get("title") or {}).get("zh")
            or (main_spec or {}).get("tree")
            or main_skill.get("name"),
            "model_id": MODEL_IDS.get(model, model),
        },
        "checklist": plan_items,
        "components": plan_components,
        "skill_template": (
            {
                "skill_id": template_entry.get("skill_id") or template_entry.get("mechanism_id"),
                "source": template_entry.get("source") or {},
                "calculation_blocks": template_blocks,
            }
            if template_entry
            else None
        ),
        "knowledge_index": {
            "knowledge_dir": _portable_path(_knowledge_settings(request)["knowledge_dir"], root_path),
            "index_path": _portable_path(_knowledge_settings(request)["index_path"], root_path),
            "index_digest": index_digest,
        },
        "scope_sources": scope,
        "build_components": build_components,
        "component_coverage": component_coverage,
        "bound_instances": {m["mechanism_id"]: m.get("matched_instances", []) for m in bound},
        "counts": {
            "checklist_items": len(plan_items),
            "supported_items": sum(1 for item in plan_items if item["status"] == "supported"),
            "bound_mechanisms": len(bound),
            "gaps": len(gaps),
        },
    }

    coverage = {
        "schema_version": SCHEMA_COVERAGE,
        "case_id": case_id,
        "scope": {
            "case_id": case_id,
            "main_skill": {"ability_id": ability_id, "model_id": MODEL_IDS.get(model, model)},
            "selected_inputs": [
                {"role": role, "path": ref.get("path"), "sha256": ref.get("sha256")}
                for role, ref in sorted(refs.items())
            ],
            "calculation_input": _case_path(case, "inputs.calculation_input"),
            "snapshot": _case_path(case, "inputs.snapshot"),
            "skill_trees": [
                {
                    "tree": tree.get("tree"),
                    "points_total": tree.get("points_total"),
                    "selected_nodes": tree.get("selected_nodes"),
                }
                for tree in scope.get("skill_trees") or []
            ],
            "passive_tree": scope.get("passive_tree"),
            "equipment_items": scope.get("equipment_items"),
            "idols": scope.get("idols"),
            "blessings": scope.get("blessings"),
            "excluded_features": scope.get("excluded_features"),
        },
        "included": [
            {
                "id": mechanism.get("mechanism_id"),
                "entry_kind": mechanism.get("entry_kind"),
                "reason": "exact 匹配且 applicability=satisfied",
                "evidence_state": mechanism.get("evidence_state"),
                "implementation_state": mechanism.get("implementation_state"),
                "components": mechanism.get("components"),
            }
            for mechanism in bound
        ]
        + (
            [
                {
                    "id": template_entry.get("skill_id") or template_entry.get("mechanism_id"),
                    "entry_kind": "skill_template",
                    "reason": "主技能模板已绑定",
                    "evidence_state": template_entry.get("evidence_state"),
                    "implementation_state": template_entry.get("implementation_state"),
                    "components": template_entry.get("components"),
                }
            ]
            if template_entry
            else []
        ),
        "excluded_with_reason": excluded_entries,
        "unresolved": unresolved_entries,
        "out_of_scope": out_of_scope_entries,
        "counts": {
            "included": len(bound) + (1 if template_entry else 0),
            "excluded_with_reason": len(excluded_entries),
            "unresolved": len(unresolved_entries),
            "out_of_scope": len(out_of_scope_entries),
        },
    }

    result = {
        "schema_version": SCHEMA_RESOLVE_BUILD_RESULT,
        "case_id": case_id,
        "main_skill": {"ability_id": ability_id, "name": main_skill.get("name")},
        "model_id": MODEL_IDS.get(model, model),
        "compatibility_baseline": dict(request.get("compatibility_baseline") or {}),
        "analysis_plan": plan,
        "bound_mechanisms": bound,
        "coverage": coverage,
        "gaps": gaps,
        "issues": issues,
        "readiness": readiness,
        "input_fingerprint": input_sha,
        "metrics": {
            "elapsed_ms": _elapsed(started),
            "component_matching": {"collect": {"elapsed_ms": round(collect_ms, 3)},
                                   "query": {"elapsed_ms": round(query_ms, 3)},
                                   "bind": {"elapsed_ms": round((time.perf_counter() - bind_started) * 1000, 3)}},
            "mechanisms_bound": len(bound),
            "gaps": len(gaps),
            "checklist_items": len(plan_items),
        },
    }
    return result


def _resolve_readiness(
    issues: Sequence[Mapping[str, Any]],
    gaps: Sequence[Mapping[str, Any]],
    template_entry: Optional[Mapping[str, Any]],
    unresolved_entries: Sequence[Mapping[str, Any]],
    out_of_scope_entries: Sequence[Mapping[str, Any]],
) -> str:
    if any(issue.get("required_input") and issue.get("severity") == contract.SEVERITY_ERROR
           for issue in issues):
        return contract.NEEDS_INPUT
    if not template_entry:
        return contract.NEEDS_RESEARCH
    blocking = [gap for gap in gaps if gap.get("blocking")]
    if blocking:
        return contract.NEEDS_RESEARCH
    if unresolved_entries or out_of_scope_entries or any(not gap.get("blocking") for gap in gaps):
        return contract.PARTIAL
    return contract.READY


# ---------------------------------------------------------------------------
# simulate
# ---------------------------------------------------------------------------
def simulate(request: Mapping[str, Any], *, context: Any = None,
             root: Optional[str | Path] = None) -> dict[str, Any]:
    """Thin adapter over the existing engines (no new calculation code).

    ``request`` (``le-simulate-request-v1``)::

        {
          "schema_version": "le-simulate-request-v1",
          "case_id": "black-hole",
          "case_ref": "examples/<case>/case.json",
          "run_id": "p2-simulate-bh",              # defaults to output dir name
          "output_dir": "reports/<case>/<run-id>",
          "inputs": {"snapshot_ref": ..., "ruleset_ref": ...},   # optional overrides
          "implementation_fingerprints": {...},    # optional
          "render_html": false,
          "generated_at": "2026-11-07T00:00:00+00:00"            # optional
        }

    The output directory must not already contain files (same protection as the
    CLI run) so a simulate call cannot overwrite a frozen historical run.
    """

    started = time.perf_counter()
    root_path = _root(context, root)
    if request.get("schema_version") not in (None, SCHEMA_SIMULATE_REQUEST):
        raise ContractError(
            f"unsupported schema_version: {request.get('schema_version')!r} "
            f"(expected {SCHEMA_SIMULATE_REQUEST})"
        )
    case_ref = request.get("case_ref")
    if not case_ref:
        raise ContractError("case_ref is required (repository-relative case.json)")
    case = _load_case(case_ref, root_path)
    case_id = str(request.get("case_id") or case.get("case_id"))
    if case_id != case.get("case_id"):
        raise ContractError(f"case_id mismatch: request {case_id!r} vs case {case.get('case_id')!r}")
    from .. import calculation_route  # noqa: PLC0415

    route = calculation_route.resolve_calculation_route(case, root=root_path)
    if route.get("engine") == calculation_route.ENGINE_UNIFIED:
        return _simulate_unified(case, root_path, request, route, started)
    model = str(request.get("model_id") or "").strip() or _case_model(case)
    if model == MODEL_IDS[MODEL_BLACK_HOLE]:
        model = MODEL_BLACK_HOLE
    if model not in (MODEL_BLACK_HOLE, MODEL_HIT):
        raise ContractError(f"unsupported model_id: {model!r}")

    output_dir = request.get("output_dir")
    if not output_dir:
        raise ContractError("output_dir is required (repository-relative run directory)")
    out_dir = _resolve_path(str(output_dir), root_path)
    if out_dir.exists() and any(out_dir.iterdir()):
        issues = [
            make_issue(
                "output_dir_not_empty",
                f"输出目录非空，拒绝覆盖历史产物：{_relative(out_dir, root_path)}",
                related_ids=[case_id],
                required_input={"kind": "new_run_id", "artifact_hint": "reports/<case>/<new-run-id>"},
            )
        ]
        return _failed_simulate_result(case_id, model, request, issues, started)

    run_id = str(request.get("run_id") or out_dir.name)
    generated_at = str(request.get("generated_at") or _now(context))
    render_html = bool(request.get("render_html", False))
    overrides = request.get("inputs") or {}
    if not isinstance(overrides, Mapping):
        raise ContractError("inputs must be an object")

    try:
        if model == MODEL_BLACK_HOLE:
            result, written = _simulate_black_hole(
                case, root_path, request, overrides, out_dir, run_id, generated_at, render_html
            )
        else:
            result, written = _simulate_hit(
                case, root_path, request, overrides, out_dir, run_id, generated_at, render_html
            )
    except ContractError:
        raise
    except Exception as exc:  # noqa: BLE001 - engine failure becomes a structured issue
        issues = [
            make_issue(
                "simulate_failed",
                f"引擎计算失败：{exc}",
                related_ids=[case_id, run_id],
                retryable=False,
                details={"exception_type": type(exc).__name__},
            )
        ]
        return _failed_simulate_result(case_id, model, request, issues, started)

    output_refs = []
    artifacts: dict[str, str] = {}
    for name, raw_path in written.items():
        path = Path(str(raw_path))
        if not path.is_file():
            continue
        rel = _relative(path, root_path)
        artifacts[name] = rel
        schema_version = None
        if name == "result.json":
            schema_version = MODEL_RESULT_SCHEMAS[model]
        elif name.endswith(".json"):
            schema_version = f"le-calc-artifact-{name}-v1"
        output_refs.append(
            make_artifact_ref(path, root=root_path, artifact_id=f"{model}:{name}",
                              schema_version=schema_version)
        )
    consistency = result.get("consistency") or {}
    issues = []
    if not consistency.get("all_pass", False):
        failed = sorted(key for key, value in consistency.items() if value is False)
        issues.append(
            make_issue(
                "consistency_failed",
                f"一致性检查未通过：{', '.join(failed) or 'all_pass=false'}",
                related_ids=[case_id, run_id],
                details={"failed": failed},
            )
        )
    summary = _simulate_summary(model, result)
    readiness = contract.READY if not issues else contract.PARTIAL
    input_fingerprint = fingerprint(
        {
            "case_id": case_id,
            "model_id": model,
            "run_id": run_id,
            "ruleset_sha256": sha256_file(_resolve_path(_case_path(case, "ruleset.path"), root_path)),
            "snapshot_sha256": sha256_file(
                _resolve_path(
                    str(overrides.get("snapshot_ref") or _case_path(case, "inputs.snapshot")),
                    root_path,
                )
            ),
            "implementation": dict(request.get("implementation_fingerprints") or {}),
        }
    )
    return {
        "schema_version": SCHEMA_SIMULATE_RESULT,
        "case_id": case_id,
        "run_id": run_id,
        "model_id": model,
        "output_dir": _relative(out_dir, root_path),
        "output_refs": output_refs,
        "artifacts": artifacts,
        "summary": summary,
        "consistency": consistency,
        "readiness": readiness,
        "issues": issues,
        "warnings": [
            "simulate 是现有引擎的薄封装：数值路径未改动；生成/时间字段不参与语义比较。",
            "本结果不改写规则包；知识变更必须先验证、发布，再显式重跑。",
        ],
        "metrics": {"elapsed_ms": _elapsed(started)},
        "input_fingerprint": input_fingerprint,
        "implementation_fingerprints": {
            "engine": {"id": "le_calc", "version": _engine_version()},
            "model_id": model,
            "engine_modules": _engine_module_fingerprints(model),
            "ruleset": {
                "path": _case_path(case, "ruleset.path"),
                "sha256": sha256_file(_resolve_path(_case_path(case, "ruleset.path"), root_path)),
            },
            "shared_operators": dict(request.get("implementation_fingerprints") or {}),
        },
    }


def _simulate_unified(
    case: Mapping[str, Any],
    root: Path,
    request: Mapping[str, Any],
    route: Mapping[str, Any],
    started: float,
) -> dict[str, Any]:
    """统一计划 simulate：必须带 plan_ref，只执行既有计划。"""
    from . import unified  # noqa: PLC0415

    output_dir = request.get("output_dir")
    plan_ref = (request.get("inputs") or {}).get("plan_ref") or {}
    issues: list[dict[str, Any]] = []
    if not plan_ref:
        issues.append(
            make_issue(
                "plan_ref_required",
                "统一路径 simulate 必须提供带哈希的 plan artifact_ref；不得在执行器内重新读取/编译 profile",
                related_ids=[str(case.get("case_id"))],
                required_input={"kind": "calculation_plan", "artifact_hint": "compile_plan"},
            )
        )
        return _failed_simulate_result(str(case.get("case_id")), "dps", request, issues, started)
    if not output_dir:
        raise ContractError("output_dir is required (repository-relative run directory)")
    out_dir = _resolve_path(str(output_dir), root)
    plan_path_value = plan_ref.get("path")
    plan_path = _resolve_path(str(plan_path_value), root) if plan_path_value else (out_dir / "calculation-plan.json")
    if not plan_path.is_file():
        issues.append(make_issue("plan_missing", f"计算计划不存在：{_relative(plan_path, root)}"))
        return _failed_simulate_result(str(case.get("case_id")), "dps", request, issues, started)
    expected_sha = str(plan_ref.get("plan_sha256") or "")
    if expected_sha and sha256_file(plan_path) != expected_sha:
        issues.append(make_issue("plan_sha_mismatch", "plan artifact_ref 的 sha256 与文件不一致"))
        return _failed_simulate_result(str(case.get("case_id")), "dps", request, issues, started)
    out_dir.mkdir(parents=True, exist_ok=True)
    target_plan = out_dir / "calculation-plan.json"
    if plan_path.resolve() != target_plan.resolve():
        target_plan.write_bytes(plan_path.read_bytes())
    if (out_dir / "result.json").is_file():
        issues.append(make_issue("output_dir_not_empty", "输出目录已含 result.json，拒绝覆盖历史结果"))
        return _failed_simulate_result(str(case.get("case_id")), "dps", request, issues, started)
    plan_document = json.loads(plan_path.read_text(encoding="utf-8"))
    stage_plan_ref = {
        "path": _relative(target_plan, root),
        "plan_sha256": str(plan_document.get("plan_sha256") or ""),
    }
    simulated = unified.simulate_stage(
        case,
        root=root,
        run_dir=out_dir,
        plan_ref=stage_plan_ref,
        scenario=request.get("scenario"),
    )
    artifacts = dict(simulated.get("artifacts") or {})
    output_refs = []
    for name, rel in artifacts.items():
        absolute = root / rel
        if absolute.is_file():
            output_refs.append(
                make_artifact_ref(
                    absolute,
                    root=root,
                    artifact_id=f"dps:{name}",
                    schema_version="le-dps-result-v3" if name == "result.json" else None,
                )
            )
    summary = dict(simulated.get("metrics", {}).get("summary") or {})
    return {
        "schema_version": SCHEMA_SIMULATE_RESULT,
        "case_id": str(case.get("case_id")),
        "run_id": request.get("run_id"),
        "model_id": "dps",
        "output_dir": _relative(out_dir, root),
        "output_refs": output_refs,
        "artifacts": artifacts,
        "summary": summary,
        "consistency": {"all_pass": True},
        "readiness": contract.READY
        if simulated.get("readiness") == "ready"
        else contract.PARTIAL,
        "issues": [],
        "warnings": ["统一路径：simulate 只执行已编译计划，不重新读取 profile/catalog。"],
        "metrics": {"elapsed_ms": _elapsed(started)},
        "input_fingerprint": _sha256_or_none(out_dir / "calculation-plan.json"),
        "implementation_fingerprints": {
            "engine": {"id": "le_calc.plan_executor", "version": _engine_version()},
            "plan": {"sha256": sha256_file(out_dir / "calculation-plan.json")},
            "executor_fingerprint": simulated.get("metrics", {}).get("executor_fingerprint"),
        },
    }


def _failed_simulate_result(case_id: str, model: str, request: Mapping[str, Any],
                            issues: Sequence[Mapping[str, Any]], started: float) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_SIMULATE_RESULT,
        "case_id": case_id,
        "run_id": request.get("run_id"),
        "model_id": model,
        "output_dir": request.get("output_dir"),
        "output_refs": [],
        "artifacts": {},
        "summary": {},
        "consistency": {"all_pass": False},
        "readiness": contract.NEEDS_INPUT,
        "issues": [dict(issue) for issue in issues],
        "warnings": [],
        "metrics": {"elapsed_ms": _elapsed(started)},
        "input_fingerprint": None,
        "implementation_fingerprints": {"engine": {"id": "le_calc", "version": _engine_version()}},
    }


def _engine_version() -> str:
    try:  # pragma: no cover - depends on package init
        from .. import __version__ as version

        return str(version)
    except Exception:  # noqa: BLE001
        return "0.1.0"


def _engine_module_fingerprints(model: str) -> dict[str, str]:
    modules = (
        ["le_calc.report", "le_calc.damage", "le_calc.ignite", "le_calc.cooldown",
         "le_calc.simulate", "le_calc.ruleset"]
        if model == MODEL_BLACK_HOLE
        else ["le_calc.hit_report", "le_calc.hit_damage", "le_calc.hit_simulate",
              "le_calc.hit_buffs", "le_calc.hit_ruleset"]
    )
    out: dict[str, str] = {}
    for name in modules:
        try:  # pragma: no cover - import resolution depends on env
            import importlib

            module = importlib.import_module(name)
            path = Path(getattr(module, "__file__", ""))
            if path.is_file():
                out[name] = sha256_file(path)
        except Exception:  # noqa: BLE001
            continue
    return out


def _override(overrides: Mapping[str, Any], key: str, case: Mapping[str, Any],
              dotted: str) -> str:
    value = overrides.get(key)
    if isinstance(value, Mapping):
        value = value.get("path")
    if value:
        return str(value)
    fallback = _case_path(case, dotted)
    if not fallback:
        raise ContractError(f"case has no {dotted} and no inputs.{key} override")
    return fallback


def _simulate_black_hole(case, root, request, overrides, out_dir, run_id,
                         generated_at, render_html):
    from ..model import Snapshot
    from ..report import load_and_build, write_run

    snapshot_rel = _override(overrides, "snapshot_ref", case, "inputs.snapshot")
    ruleset_rel = _override(overrides, "ruleset_ref", case, "ruleset.path")
    calc_rel = _override(overrides, "calc_input_ref", case, "inputs.calculation_input")
    build_rel = _override(overrides, "build_ref", case, "inputs.bd")
    scenario_override = overrides.get("scenario_ref")
    registry_rel = overrides.get("conditional_registry_ref") or _case_path(
        case, "conditional_registry.path"
    )
    snapshot_path = _resolve_path(snapshot_rel, root)
    snapshot = Snapshot.from_dict(_read_json(snapshot_path))
    out_display = str(request.get("output_dir_display") or _relative(out_dir, root))
    repro = " ".join(
        [
            "$env:PYTHONPATH='tools/phase3/src';",
            '& ".venv\\Scripts\\python.exe" -m le_calc run',
            f"--snapshot {snapshot_rel}",
            f"--ruleset {ruleset_rel}",
            f"--monster-level {int(case.get('monster_level') or 100)}",
            f"--calc-input {calc_rel}",
            f"--build {build_rel}",
            f'--out "{out_display}"',
            f"--run-id {run_id}",
        ]
    )
    result = load_and_build(
        snapshot,
        run_id=run_id,
        generated_at=generated_at,
        ruleset_path=ruleset_rel,
        out_dir=out_display,
        repro_command=repro,
        monster_level=int(case.get("monster_level") or 100),
        conditional_registry=registry_rel,
        calc_input_path=calc_rel,
        build_path=build_rel,
        conditional_scenario=str(scenario_override) if scenario_override else None,
        enable_conditional=True,
        enable_recompute=True,
    )
    written = write_run(result, out_dir, render_html=render_html)
    return result, written


def _simulate_hit(case, root, request, overrides, out_dir, run_id,
                  generated_at, render_html):
    from .. import model
    from ..hit_buffs import screen_buffs
    from ..hit_damage import compute_single_hit
    from ..hit_report import build_hit_result, ruleset_to_document, write_hit_run
    from ..hit_ruleset import load_ruleset as load_hit_ruleset
    from ..hit_simulate import simulate as hit_simulate

    snapshot_rel = _override(overrides, "snapshot_ref", case, "inputs.snapshot")
    ruleset_rel = _override(overrides, "ruleset_ref", case, "ruleset.path")
    scenario_rel = overrides.get("scenario_ref") or _case_path(case, "inputs.scenario")
    snapshot_raw = _read_json(_resolve_path(snapshot_rel, root))
    ruleset = load_hit_ruleset(str(_resolve_path(ruleset_rel, root)))
    snapshot = model.Snapshot.from_dict(snapshot_raw)
    scenario = _read_json(_resolve_path(str(scenario_rel), root)) if scenario_rel else None
    single_hit = compute_single_hit(snapshot, ruleset, scenario)
    simulation = hit_simulate(snapshot, ruleset, scenario)
    screening = screen_buffs(snapshot, ruleset, scenario)
    baselines: dict[str, Any] = {}
    sensitivity: list[dict[str, Any]] = []
    try:
        profiles = (scenario or {}).get("profiles") or {}
        for profile_id in profiles:
            profile_scenario = _deep_copy(scenario)
            profile_scenario["baseline_profile"] = profile_id
            baselines[profile_id] = {
                "single_hit": compute_single_hit(snapshot, ruleset, profile_scenario),
                "simulate": hit_simulate(snapshot, ruleset, profile_scenario),
            }
        for variant_id, label, states in HIT_SENSITIVITY_VARIANTS:
            variant_scenario = _scenario_with_states(scenario, states)
            sensitivity.append(
                {
                    "id": variant_id,
                    "label": label,
                    "states": dict(states),
                    "single_hit": compute_single_hit(snapshot, ruleset, variant_scenario),
                    "simulate": hit_simulate(snapshot, ruleset, variant_scenario),
                }
            )
    except Exception:  # noqa: BLE001 - main result stays usable (mirrors the CLI)
        baselines = {}
        sensitivity = []
    out_display = str(request.get("output_dir_display") or _relative(out_dir, root))
    repro = " ".join(
        [
            "$env:PYTHONPATH='tools/phase3/src';",
            '& ".venv\\Scripts\\python.exe" -m le_calc hit-run',
            f"--case {_case_path(case, '_path') or case.get('_path')}",
            f"--snapshot {snapshot_rel}",
            f"--ruleset {ruleset_rel}",
            *([f"--scenario {scenario_rel}"] if scenario_rel else []),
            f'--out "{out_display}"',
            f"--run-id {run_id}",
        ]
    )
    result = build_hit_result(
        snapshot,
        ruleset,
        case,
        run_id=run_id,
        generated_at=generated_at,
        ruleset_path=ruleset_rel,
        out_dir=out_display,
        scenario=scenario,
        scenario_path=str(scenario_rel) if scenario_rel else None,
        engine_outputs={
            "single_hit": single_hit,
            "simulate": simulation,
            "buff_screening": screening,
            "baselines": baselines,
            "buff_sensitivity": sensitivity,
        },
        repro_command=repro,
    )
    written = write_hit_run(
        result,
        out_dir,
        snapshot_document=snapshot_raw,
        ruleset_document=ruleset_to_document(ruleset),
        screening_document=screening,
        render_html=render_html,
    )
    return result, written


def _deep_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _scenario_with_states(scenario: Optional[Mapping[str, Any]],
                          extra_states: Mapping[str, Any]) -> dict[str, Any]:
    base = _deep_copy(scenario) if scenario else {}
    for section in ("single_hit", "dps"):
        block = base.setdefault(section, {})
        states = dict(block.get("states") or {})
        states.update(dict(extra_states))
        block["states"] = states
    return base


def _simulate_summary(model: str, result: Mapping[str, Any]) -> dict[str, Any]:
    run = result.get("run") or {}
    base = {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "ruleset_id": (run.get("ruleset") or {}).get("id"),
        "ruleset_sha256": (run.get("ruleset") or {}).get("sha256"),
        "consistency_all_pass": bool((result.get("consistency") or {}).get("all_pass")),
        "issues_count": len(result.get("issues") or []),
    }
    if model == MODEL_BLACK_HOLE:
        totals = ((result.get("single_cast") or {}).get("totals") or {})
        base["single_cast"] = {
            "total": totals.get("single_cast_total"),
            "body": totals.get("body_total"),
            "ignite": totals.get("ignite_total"),
            "ignite_tail": totals.get("ignite_tail_damage"),
            "expected_stacks": totals.get("expected_total_stacks_applied"),
            "observation_end_s": totals.get("observation_end_s"),
        }
        loops = []
        for loop in result.get("loops") or []:
            loops.append(
                {
                    "policy_id": loop.get("policy_id"),
                    "cycle_s": (loop.get("totals") or {}).get("cycle_s"),
                    "windows": [
                        {
                            "window_s": window.get("window_s"),
                            "dps": window.get("dps"),
                            "total_damage": window.get("total_damage"),
                            "cast_count": window.get("cast_count"),
                        }
                        for window in loop.get("windows") or []
                    ],
                }
            )
        base["loops"] = loops
        conditional = result.get("conditional") or {}
        base["conditional"] = {
            "status": conditional.get("status"),
            "recompute_status": (conditional.get("recompute") or {}).get("status"),
            "baseline_profile": (conditional.get("recompute") or {}).get("baseline_profile"),
        }
    else:
        summary = _hit_summary(result)
        base.update(summary)
    return base


def _hit_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    try:  # pragma: no cover - depends on hit report availability
        from ..hit_report import result_summary

        return dict(result_summary(result))
    except Exception:  # noqa: BLE001
        return {
            "single_hit": ((result.get("single_hit") or {}).get("totals") or {}).get("total"),
            "dps": result.get("dps"),
        }


# ---------------------------------------------------------------------------
# validate_result
# ---------------------------------------------------------------------------
def _semantic_calc_input(document: Mapping[str, Any]) -> dict[str, Any]:
    """Projection of a calculation input that must be identical across reproduce runs.

    Mirrors ``scripts/le_commands._semantic_calc_input`` so the #18 fix can verify
    that the reproduce run's ``steps/04-calculation-input.corrected.json`` is a
    corrections-derived equivalent of the shipped case calculation input.
    """

    mods = []
    for mod in document.get("mods") or []:
        mods.append(
            {
                key: mod.get(key)
                for key in (
                    "mod_id", "item_instance_id", "source", "channel", "kind",
                    "property", "tags", "special_tag", "modifier_type",
                    "value_numeric", "display_value", "value_status", "value_source",
                )
            }
            | {"stat_name": (mod.get("stat") or {}).get("name")}
        )
    items = [
        {
            "instance_id": item.get("instance_id"),
            "role": item.get("role"),
            "slot": item.get("slot"),
            "mods": [mod.get("mod_id") for mod in item.get("mods") or []],
        }
        for item in document.get("items") or []
    ]
    return {
        "character": document.get("character"),
        "passive_tree": {
            "points_total": (document.get("passive_tree") or {}).get("points_total"),
            "nodes": [
                {"node_id": node.get("node_id"), "points": node.get("points")}
                for node in (document.get("passive_tree") or {}).get("nodes") or []
            ],
        },
        "specializations": [
            {
                "tree": (spec.get("tree") or {}).get("entity_id"),
                "points_total": spec.get("points_total"),
                "nodes": [
                    {"node_id": node.get("node_id"), "points": node.get("points")}
                    for node in spec.get("nodes") or []
                ],
            }
            for spec in (document.get("skills") or {}).get("specializations") or []
        ],
        "mods": mods,
        "items": items,
    }


def _reproduce_calc_input_chain(run_dir: Optional[Path]) -> Optional[dict[str, Path]]:
    """Return the reproduce steps that derive the bound calculation input, if present."""

    if run_dir is None:
        return None
    steps = run_dir / "steps"
    if not steps.is_dir():
        return None
    files = {
        "export": steps / "02-calculation-input.json",
        "affix_db": steps / "03-calculation-input.affix-db.json",
        "corrections_report": steps / "04-save-build-corrections.json",
        "corrected": steps / "04-calculation-input.corrected.json",
    }
    if not files["corrected"].is_file():
        return None
    return files


def _calc_input_binding(run_dir: Optional[Path], case_calc_path: Path) -> dict[str, Any]:
    """Resolve accepted bound hashes and the reproduce derivation state (#18).

    * ``simulate`` runs bind the case calculation input directly (strict sha).
    * ``reproduce`` runs bind ``steps/04-calculation-input.corrected.json``; the
      corrected file is accepted **only** when the 02→03→04 chain exists and its
      semantic projection equals the shipped case calculation input.
    """

    case_sha = sha256_file(case_calc_path)
    accepted = {case_sha}
    info: dict[str, Any] = {"case_sha256": case_sha, "derived": None, "accepted": accepted}
    chain = _reproduce_calc_input_chain(run_dir)
    if chain is None:
        return info
    corrected_sha = sha256_file(chain["corrected"])
    accepted.add(corrected_sha)
    try:
        semantic_equal = _semantic_calc_input(_read_json(chain["corrected"])) == _semantic_calc_input(
            _read_json(case_calc_path)
        )
    except ContractError:
        semantic_equal = False
    derived = {
        "corrected_sha256": corrected_sha,
        "semantic_equals_case_input": semantic_equal,
        "chain_complete": all(
            chain[key].is_file() for key in ("export", "affix_db", "corrections_report", "corrected")
        ),
    }
    info["derived"] = derived
    return info


def _json_pointer(document: Any, pointer: str) -> Any:
    node = document
    for raw in [part for part in str(pointer).split("/") if part]:
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            node = node[int(part)]
        else:
            node = node[part]
    return node


def _rel_error(actual: float, expected: float) -> float:
    if expected == 0:
        return abs(actual - expected)
    return abs(actual - expected) / abs(expected)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _status_ok(checks: Sequence[Mapping[str, Any]]) -> bool:
    return all(check.get("status") in ("PASS", "SKIP", "INFO") for check in checks)


def _make_check(check_id: str, ok: bool, *, expected: Any = None, actual: Any = None,
                tolerance: Optional[float] = None, detail: str = "") -> dict[str, Any]:
    return {
        "id": check_id,
        "status": "PASS" if ok else "FAIL",
        "expected": expected,
        "actual": actual,
        "tolerance": tolerance,
        "detail": detail,
    }


CheckSelector = Optional[Sequence[str]]


def _selected(check_id: str, selectors: CheckSelector) -> bool:
    if not selectors:
        return True
    return any(check_id == item or check_id.startswith(item + ".") for item in selectors)


def validate_result(request: Mapping[str, Any], *, context: Any = None,
                    root: Optional[str | Path] = None) -> contract.ValidationResult:
    """Validate a produced run against its case bindings and expected summary.

    ``request``::

        {
          "case_id": "black-hole",
          "case_ref": "examples/<case>/case.json",
          "run_dir": "reports/<case>/<run-id>",   # or result_ref/expected_ref
          "result_ref": null, "expected_ref": null,
          "checks": ["structure", "binding", "consistency", "expected"],  # optional
          "tolerance_rel": 1e-9                                          # optional
        }

    Semantics mirror ``scripts/le_commands.py::_check_expected*`` (numeric
    comparison uses relative tolerance; strings/bools compare exactly) but the
    implementation lives in ``le_calc`` and never imports ``scripts/``.
    Artifacts are only read; nothing is modified.
    """

    root_path = _root(context, root)
    case_ref = request.get("case_ref")
    if not case_ref:
        raise ContractError("case_ref is required (repository-relative case.json)")
    case = _load_case(case_ref, root_path)
    case_id = str(request.get("case_id") or case.get("case_id"))
    if case_id != case.get("case_id"):
        raise ContractError(f"case_id mismatch: request {case_id!r} vs case {case.get('case_id')!r}")
    model = _case_model(case)
    constructs = request.get("checks")
    if constructs is not None:
        selects = [str(item) for item in constructs]
    run_dir_value = request.get("run_dir")
    if run_dir_value:
        candidate_dir = _resolve_path(str(run_dir_value), root_path)
        if (candidate_dir / "calculation-plan.json").is_file() and (
            candidate_dir / "result.json"
        ).is_file():
            from .. import calculation_route  # noqa: PLC0415
            from . import unified  # noqa: PLC0415

            route = calculation_route.resolve_calculation_route(case, root=root_path)
            if route.get("engine") == calculation_route.ENGINE_UNIFIED:
                checked = unified.validate_stage(case, root=root_path, run_dir=candidate_dir)
                payload = checked.get("payload") or {}
                checks = [
                    {
                        "id": item.get("id"),
                        "status": item.get("status"),
                        "detail": item.get("detail"),
                    }
                    for item in payload.get("checks") or []
                ]
                errors = [
                    {
                        "id": item.get("id"),
                        "code": "check_failed",
                        "message": item.get("message"),
                    }
                    for item in payload.get("errors") or []
                ]
                return contract.ValidationResult(
                    kind="result",
                    passed=bool(checked.get("passed")),
                    accepted=list(payload.get("accepted") or []),
                    errors=errors,
                    checks=checks,
                    next_actions=[]
                    if checked.get("passed")
                    else ["修正计划/场景/结果/账本绑定后重新运行；不要改写已发布 run。"],
                    blocking=list(errors),
                )
    selects = constructs

    run_dir_value = request.get("run_dir")
    if not run_dir_value and not request.get("result_ref"):
        raise ContractError("run_dir or result_ref is required")
    run_dir = _resolve_path(str(run_dir_value), root_path) if run_dir_value else None
    result_path = (
        _resolve_path(str(request["result_ref"]), root_path)
        if request.get("result_ref")
        else (run_dir / "result.json")  # type: ignore[operator]
    )
    if not result_path.is_file():
        return contract.ValidationResult(
            kind="result",
            passed=False,
            errors=[
                {
                    "id": "structure.result_json",
                    "code": "result_missing",
                    "message": f"结果文件不存在：{_relative(result_path, root_path)}",
                }
            ],
            checks=[],
            next_actions=["先运行 simulate 生成新的 run 目录；不要手工改写历史结果。"],
        )
    result = _read_json(result_path)
    expected_ref = request.get("expected_ref") or _case_path(case, "expected")
    expected = _read_json(_resolve_path(str(expected_ref), root_path)) if expected_ref else {}
    tolerance = float(
        request.get("tolerance_rel")
        if request.get("tolerance_rel") is not None
        else (expected.get("tolerance_rel") or 0.0)
    )

    checks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    accepted: list[str] = []
    next_actions: list[str] = []

    def add(check: dict[str, Any], *, blocking: bool = True) -> None:
        checks.append(check)
        if check["status"] == "PASS":
            accepted.append(str(check["id"]))
        elif blocking:
            errors.append(
                {
                    "id": check["id"],
                    "code": "check_failed",
                    "message": check.get("detail") or check["id"],
                    "expected": check.get("expected"),
                    "actual": check.get("actual"),
                    "tolerance": check.get("tolerance"),
                }
            )

    # ---- structure -----------------------------------------------------
    if _selected("structure", selects):
        expected_schema = MODEL_RESULT_SCHEMAS[model]
        add(_make_check(
            "structure.result_schema",
            result.get("schema") == expected_schema,
            expected=expected_schema,
            actual=result.get("schema"),
            detail="result.json schema 与模型一致",
        ))
        missing = [key for key in RESULT_REQUIRED_KEYS[model] if key not in result]
        add(_make_check(
            "structure.result_required_keys",
            not missing,
            expected=list(RESULT_REQUIRED_KEYS[model]),
            actual=missing,
            detail=f"缺少字段：{missing}" if missing else "必需字段齐全",
        ))
        manifest_path = run_dir / "run-manifest.json" if run_dir else None
        if manifest_path and manifest_path.is_file():
            manifest = _read_json(manifest_path)
            files_ok = True
            mismatched: list[str] = []
            for name, digest in (manifest.get("files") or {}).items():
                file_path = run_dir / name  # type: ignore[operator]
                if not file_path.is_file() or sha256_file(file_path) != digest:
                    files_ok = False
                    mismatched.append(name)
            add(_make_check(
                "structure.run_manifest",
                files_ok,
                expected="所有产物 sha256 与 run-manifest 一致",
                actual=mismatched,
                detail="run-manifest 指纹一致" if files_ok else f"指纹不一致：{mismatched}",
            ))
        else:
            add(_make_check(
                "structure.run_manifest",
                False,
                expected="run-manifest.json",
                actual=None,
                detail="run-manifest.json 缺失（历史 run 也必须保留清单）",
            ))

    # ---- binding -------------------------------------------------------
    ruleset_rel = _case_path(case, "ruleset.path")
    calc_rel = _case_path(case, "inputs.calculation_input")
    snapshot_rel = _case_path(case, "inputs.snapshot")
    build_rel = _case_path(case, "build.build_spec")
    input_blocks = result.get("input") or {}
    fingerprints = input_blocks.get("fingerprints") or {}

    def case_sha(*keys: str) -> Optional[str]:
        node: Any = case
        for key in keys:
            if not isinstance(node, Mapping):
                return None
            node = node.get(key)
        return str(node) if isinstance(node, str) and node else None

    if _selected("binding", selects):
        if ruleset_rel:
            declared = case_sha("ruleset", "sha256")
            actual = sha256_file(_resolve_path(ruleset_rel, root_path))
            add(_make_check(
                "binding.ruleset_file",
                bool(declared) and actual == declared,
                expected=declared, actual=actual,
                detail="case 声明的 ruleset sha256 与文件一致",
            ))
            run_ruleset = ((result.get("run") or {}).get("ruleset") or {})
            add(_make_check(
                "binding.ruleset_result",
                run_ruleset.get("sha256") == declared and run_ruleset.get("id") == case.get("ruleset", {}).get("id"),
                expected={"id": case.get("ruleset", {}).get("id"), "sha256": declared},
                actual={"id": run_ruleset.get("id"), "sha256": run_ruleset.get("sha256")},
                detail="结果中的 ruleset 绑定与 case 一致",
            ))
        calc_binding: dict[str, Any] = {}
        if calc_rel:
            calculated_path = _resolve_path(calc_rel, root_path)
            declared = case_sha("input_sha256", "calculation_input")
            actual = sha256_file(calculated_path)
            result_calc = input_blocks.get("calc_input") or {}
            calc_binding = _calc_input_binding(run_dir, calculated_path)
            accepted_calc = calc_binding["accepted"]
            add(_make_check(
                "binding.calc_input",
                bool(declared) and actual == declared and result_calc.get("sha256") in accepted_calc,
                expected=declared,
                actual={
                    "case_file": actual,
                    "result": result_calc.get("sha256"),
                    "accepted_bound": sorted(accepted_calc),
                },
                detail="计算输入 sha256 链（case → 文件 → result.input）",
            ))
            derived = calc_binding.get("derived")
            if derived:
                derived_ok = bool(derived["semantic_equals_case_input"]) and bool(
                    derived["chain_complete"]
                ) and result_calc.get("sha256") == derived["corrected_sha256"]
                add(_make_check(
                    "binding.calc_input_derived",
                    derived_ok,
                    expected={
                        "bound": "steps/04-calculation-input.corrected.json",
                        "semantic_equals_case_input": True,
                    },
                    actual=derived,
                    detail=(
                        "reproduce 绑定 corrected-input 派生链：02 导出 → 03 affix-db → "
                        "04 corrections，且 corrected 语义与案例计算输入一致"
                    ),
                ))
        if build_rel:
            build_declared = case_sha("build", "build_spec_sha256")
            build_actual = sha256_file(_resolve_path(build_rel, root_path))
            result_build = input_blocks.get("build") or {}
            content_declared = case_sha("build", "content_sha256")
            add(_make_check(
                "binding.build_spec",
                bool(build_declared) and build_actual == build_declared,
                expected=build_declared, actual=build_actual,
                detail="BuildSpec sha256 与 case 一致",
            ))
            add(_make_check(
                "binding.build_content",
                bool(content_declared) and result_build.get("content_sha256") == content_declared,
                expected=content_declared,
                actual=result_build.get("content_sha256"),
                detail="构筑 content_sha256 与 result.input 一致",
            ))
        if snapshot_rel:
            snap_declared = case_sha("input_sha256", "snapshot")
            snap_actual = sha256_file(_resolve_path(snapshot_rel, root_path))
            snap_build = ((result.get("snapshot") or {}).get("build_meta") or {})
            add(_make_check(
                "binding.snapshot_file",
                bool(snap_declared) and snap_actual == snap_declared,
                expected=snap_declared, actual=snap_actual,
                detail="快照文件 sha256 与 case 一致",
            ))
            snap_calc = snap_build.get("calc_input") or {}
            if calc_rel:
                accepted_calc = calc_binding.get("accepted") or {
                    sha256_file(_resolve_path(calc_rel, root_path))
                }
                add(_make_check(
                    "binding.snapshot_calc_input",
                    snap_calc.get("sha256") in accepted_calc,
                    expected=sorted(accepted_calc),
                    actual=snap_calc.get("sha256"),
                    detail="快照 build_meta.calc_input 与绑定的计算输入一致（simulate/reproduce 两种绑定）",
                ))
        # dataset binding (data/current.json + database file)
        dataset_expected = case.get("dataset_expected") or {}
        current_rel = case.get("dataset")
        if isinstance(current_rel, str) and (root_path / current_rel).is_file():
            current = _read_json(root_path / current_rel)
            add(_make_check(
                "binding.dataset",
                current.get("dataset_id") == dataset_expected.get("dataset_id")
                and current.get("logical_content_sha256") == dataset_expected.get("logical_content_sha256"),
                expected=dataset_expected.get("dataset_id"),
                actual=current.get("dataset_id"),
                detail="data/current.json 指向的 dataset 与 case 声明一致",
            ))
            database = current.get("database") or current.get("path")
            if isinstance(database, str):
                db_path = _resolve_path(database, root_path)
                if db_path.is_file():
                    add(_make_check(
                        "binding.dataset_file",
                        sha256_file(db_path) == current.get("file_sha256"),
                        expected=current.get("file_sha256"),
                        actual=sha256_file(db_path),
                        detail="dataset 文件 sha256 与 current.json 一致",
                    ))

    # ---- consistency ---------------------------------------------------
    if _selected("consistency", selects):
        result_consistency = result.get("consistency") or {}
        add(_make_check(
            "consistency.all_pass",
            bool(result_consistency.get("all_pass")),
            expected=True, actual=result_consistency.get("all_pass"),
            detail="result.consistency.all_pass",
        ))
        if run_dir and (run_dir / "run-manifest.json").is_file():
            manifest = _read_json(run_dir / "run-manifest.json")
            manifest_consistency = manifest.get("consistency") or {}
            add(_make_check(
                "consistency.run_manifest",
                bool(manifest_consistency.get("all_pass")),
                expected=True, actual=manifest_consistency.get("all_pass"),
                detail="run-manifest.consistency.all_pass",
            ))

    # ---- expected summary (shared implementation with le_commands) ----
    if _selected("expected", selects) and expected:
        add(_make_check(
            "expected.schema",
            expected.get("schema") == "le-case-expected-v1",
            expected="le-case-expected-v1", actual=expected.get("schema"),
            detail="expected summary schema",
        ))
        add(_make_check(
            "expected.case_id",
            expected.get("case_id") == case.get("case_id"),
            expected=case.get("case_id"), actual=expected.get("case_id"),
            detail="expected case_id",
        ))
        compare_dir = run_dir if run_dir is not None else result_path.parent
        try:
            shared_checks, _shared_ok = expected_check.compare_expected(
                case, compare_dir, root=root_path, expected=expected
            )
        except expected_check.ExpectedCheckError as exc:
            add(_make_check(
                "expected.summary", False, expected="可比较的 run 目录与 expected",
                actual=None, detail=str(exc),
            ))
            shared_checks = []
        for shared in shared_checks:
            scope = shared.get("scope")
            name = str(shared.get("name"))
            if scope == "run_expectation":
                # keep the historical ``expected.run.<item>`` ids; item names that
                # already start with ``run.`` keep a single prefix (e.g. run.status).
                check_id = f"expected.{name}" if name.startswith("run.") else f"expected.run.{name}"
            elif scope == "conditional_scenario":
                check_id = "expected.conditional_scenario"
            else:
                check_id = f"expected.check.{name}"
            if not _selected(check_id, selects):
                continue
            add(_make_check(
                check_id,
                str(shared.get("status")) == "PASS",
                expected=shared.get("expected"),
                actual=shared.get("actual"),
                tolerance=shared.get("tolerance", tolerance),
                detail=str(shared.get("detail") or ""),
            ))

    if errors:
        next_actions = [
            "不要改写历史 run/expected；用 simulate 生成新的 run-id 后重跑 validate_result。",
            "若绑定指纹不一致，先确认 case 的 input_sha256/build/ruleset/dataset 声明与当前文件一致。",
            "若 expected 数值不一致，区分『模型数值变化』与『case 输入变化』，由维护者裁定后再改 expected。",
        ]
    passed = not errors
    return contract.ValidationResult(
        kind="result",
        passed=passed,
        accepted=accepted,
        errors=errors,
        checks=checks,
        next_actions=next_actions if errors else [],
        outcome_effective=None,
    )


__all__ = [
    "DEFAULT_INDEX_PATH",
    "DEFAULT_KNOWLEDGE_DIR",
    "DEFAULT_ROOT",
    "HIT_SENSITIVITY_VARIANTS",
    "MODEL_BLACK_HOLE",
    "MODEL_HIT",
    "MODEL_IDS",
    "MODEL_RESULT_SCHEMAS",
    "resolve_build",
    "simulate",
    "validate_result",
]
