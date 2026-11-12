"""Discovery: turn the real build + registry into the conditional catalog.

Contract references: ``docs/contracts/conditional-interface.md`` sections 6 and
8.2.  The discovery layer is deliberately a *cross-check* of what the current
build actually contains:

* denominators are enumerated from real data (calc input items/mods, skill
  specializations, passive nodes, snapshot contributions, excluded sources);
* registry effects are matched against concrete build instances (node points,
  equipped mods) and their declarative values are resolved to actuals;
* heuristic cues (bilingual regex + catalog buff/config flags) produce
  ``unconfirmed`` candidates for conditional sources the registry does not cover
  yet, so a sparse registry cannot silently hide a real conditional effect;
* every examined object that is neither a candidate nor a registered effect
  becomes a ``checked_records`` entry with a reason (reusing the snapshot's
  existing exclusion reasons when available);
* data gaps are explicit (missing calc input, hash mismatch, unknown node,
  unparsable points, auto-state derivation failure, denominator mismatch, ...).

Output is deterministic and JSON-safe; no timestamps or absolute paths.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from . import constants as C
from .registry import Registry, resolve_actuals

try:  # pragma: no cover - import is only for typing / tag decoding
    from .. import model as _model
except Exception:  # pragma: no cover
    _model = None  # type: ignore

try:  # optional catalog access; discovery degrades when unavailable
    from ..catalog import Catalog as _Catalog
    from ..catalog import CatalogError as _CatalogError
except Exception:  # pragma: no cover
    _Catalog = None  # type: ignore
    _CatalogError = Exception  # type: ignore


# ---------------------------------------------------------------------------
# Display / categorisation helpers
# ---------------------------------------------------------------------------

SLOT_LABELS = {
    "head": "头盔",
    "chest": "胸甲",
    "weapon1": "武器",
    "hands": "手套",
    "waist": "腰带",
    "feet": "鞋子",
    "ring1": "戒指 1",
    "ring2": "戒指 2",
    "amulet": "项链",
    "relic": "遗物",
    "idol_altar": "神像祭坛",
}

#: fixed cue vocabulary used by the heuristic candidate scan (contract §6.2)
TEXT_CUES: tuple[tuple[str, str], ...] = (
    ("conditional", r"\bwhile\b|\bwhen\b|\bif\b|当|如果|若|期间"),
    ("buff", r"\bbuff\b|增益"),
    ("duration", r"\bduration\b|\bfor\s+\d+(?:\.\d+)?\s*seconds?\b|\bseconds?\b|持续|秒"),
    ("stack", r"\bper\s+stack\b|\bstacks?\b|每层|层数|叠层"),
    ("chance", r"\bchance\s+(?:to|of|for)\b|概率"),
    ("hit", r"\bon\s+hit\b|\bhit\b|击中|命中"),
    ("trigger", r"\btrigger|\bgrants?\b|\bgain\b|触发|获得|施放"),
    ("next", r"\bnext\b|下一次|下次"),
    ("per", r"\bper\s+\d|\bper\b|每"),
    ("active", r"\bactive\b|\bwhile\s+active\b|激活|生效"),
    ("against", r"\bagainst\b|\bto\s+enemies\b|对冰缓|对冻结|对敌人"),
    ("aura", r"\baura\b|光环"),
    ("threshold", r"\b300\b|\bthreshold\b|门槛|不少于|以上"),
)

_CUE_ORDER = {name: index for index, (name, _pattern) in enumerate(TEXT_CUES)}
_COMPILED_CUES = tuple((name, re.compile(pattern, re.IGNORECASE)) for name, pattern in TEXT_CUES)

#: cues strong enough on their own to make an examined source a *candidate*.
#: ``duration`` / ``per`` / ``aura`` alone are weak (a plain "+X% duration" or
#: "ward per second" stat is not conditional), but are kept in the display cue
#: list when a strong cue is also present.
STRONG_CUES = frozenset(
    {"conditional", "buff", "chance", "trigger", "next", "active", "against", "hit", "threshold", "stack"}
)

#: defensive wording used only to downgrade already-cued defensive mods into
#: checked records instead of "待确认" candidates (a supplementary filter; the
#: candidate decision itself is driven by the conditional cues above).
DEFENSIVE_WORDS = (
    "ward",
    "armor",
    "health",
    "endurance",
    "dodge",
    "block",
    "resist",
    "potion",
    "movement speed",
    "stun",
    "leech",
    "regenerat",
    "decay",
    "receive a critical",
)
OFFENSIVE_WORDS = (
    "damage",
    "ignite",
    "ailment",
    "fire",
    "cold",
    "lightning",
    "necrotic",
    "void",
    "physical",
    "penetration",
    "cast",
    "cooldown",
    "duration",
    "spell",
    "shred",
    "more ",
    "increased ",
)

_CATEGORY_SORT = {name: index for index, name in enumerate(C.SORT_CATEGORY_ORDER)}

ITEM_ROLE_TO_GRANULAR = {
    "equipment": "equipment",
    "idol": "idol",
    "blessing": "blessing",
}

CHECKED_SUMMARY_CATEGORIES = (
    ("equipment", "装备"),
    ("idol", "神像"),
    ("blessing", "祝福"),
    ("passive_node", "职业被动"),
    ("skill_node", "技能节点"),
)

_CALC_INPUT_REF = "<case>/inputs/calculation-input.affix-db.json"


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


def scan_cues(text: str) -> list[str]:
    """Return the ordered list of condition-cue names found in ``text``."""
    if not text:
        return []
    found: list[str] = []
    for name, pattern in _COMPILED_CUES:
        if pattern.search(text):
            found.append(name)
    found.sort(key=lambda name: _CUE_ORDER.get(name, 99))
    return found


def _looks_defensive(text: str) -> bool:
    """True for a cued source whose effect is purely defensive/survival text."""
    lowered = (text or "").casefold()
    if not any(word in lowered for word in DEFENSIVE_WORDS):
        return False
    return not any(word in lowered for word in OFFENSIVE_WORDS)


def item_granular_category(item: dict[str, Any]) -> str:
    instance_id = _text(item.get("instance_id"))
    if instance_id == "equipment.idol_altar":
        return "idol_altar"
    role = _text(item.get("role"))
    return ITEM_ROLE_TO_GRANULAR.get(role, "equipment")


def item_page_category(item: dict[str, Any]) -> str:
    granular = item_granular_category(item)
    if granular in ("idol", "idol_altar"):
        return "idol"
    return "equipment"


def _checked_item_category(item: dict[str, Any]) -> str:
    """Granular category used by the page's checked-scope summary rows."""
    granular = item_granular_category(item)
    if granular in ("equipment", "idol_altar"):
        return "equipment"
    return granular


def _contribution_node_key(contribution_id: str) -> Optional[str]:
    """Derive a skill/passive node key from a snapshot contribution id."""
    cleaned = _text(contribution_id)
    if cleaned.startswith("skill:"):
        parts = cleaned.split(":")
        if len(parts) >= 3:
            return f"skill:{parts[1]}:{parts[2]}"
    if cleaned.startswith("passive:"):
        parts = cleaned.split(":")
        if len(parts) >= 3:
            return f"passive:{parts[1]}:{parts[2]}"
    return None


def source_kind_page_category(source_kind: str, effect_category: str | None = None) -> str:
    if source_kind in ("idol", "idol_altar"):
        return "idol"
    if source_kind == "blessing":
        return "equipment"
    if source_kind in ("skill_node", "main_skill_node", "passive_node", "equipment"):
        return str(effect_category or ("passive" if source_kind == "passive_node" else "equipment"))
    category = str(effect_category or "equipment")
    return C.CATEGORY_ALIASES.get(category, category)


def effect_page_category(effect: dict[str, Any]) -> str:
    source = effect.get("source") or {}
    source_kind = _text(source.get("kind"))
    category = _text(effect.get("category"))
    if source_kind in ("idol", "idol_altar"):
        return "idol"
    if source_kind == "blessing":
        return "equipment"
    if category == "idol":
        return "idol"
    return C.CATEGORY_ALIASES.get(category, category or "equipment")


def effect_sort_key(effect: dict[str, Any]) -> tuple:
    category = effect_page_category(effect)
    category_index = _CATEGORY_SORT.get(category, len(_CATEGORY_SORT))
    return (category_index, str(effect.get("effect_id") or ""))


def item_label(item: dict[str, Any]) -> str:
    display = _text(item.get("display_name") or item.get("base_name")) or "装备"
    instance_id = _text(item.get("instance_id"))
    if instance_id.startswith("idols."):
        index = instance_id.rsplit(".", 1)[-1]
        try:
            number = int(index) + 1
        except ValueError:
            number = index
        return f"神像 {number} · {display}"
    if instance_id.startswith("blessings."):
        return f"祝福 · {display}"
    if instance_id == "equipment.idol_altar":
        return f"神像祭坛 · {display}"
    slot = _text(item.get("slot"))
    slot_label = SLOT_LABELS.get(slot, slot or "装备")
    return f"{display}（{slot_label}）"


def contribution_entry(contribution: Any) -> dict[str, Any]:
    """Trim one snapshot contribution into a JSON-safe existing-handling row."""
    return {
        "contribution_id": _text(getattr(contribution, "contribution_id", "")),
        "included": bool(getattr(contribution, "included", False)),
        "condition_code": getattr(contribution, "condition_code", None),
        "condition_status": _text(getattr(contribution, "condition_status", "always")),
        "stat_key": _text(getattr(contribution, "stat_key", "")),
        "operation": _text(getattr(contribution, "operation", "")),
        "value": float(getattr(contribution, "value", 0.0) or 0.0),
        "unit": _text(getattr(contribution, "unit", "")),
        "target": getattr(contribution, "target", None),
        "source_label": _text(getattr(contribution, "source_label", "")),
        "exclusion_reason": getattr(contribution, "exclusion_reason", None),
    }


def _is_active_contribution(contribution: Any) -> bool:
    included = bool(getattr(contribution, "included", False))
    status = _text(getattr(contribution, "condition_status", "always"))
    return included and status in ("always", "satisfied")


# ---------------------------------------------------------------------------
# Text/summary helpers shared with the view
# ---------------------------------------------------------------------------


def effect_value_lines(effect: dict[str, Any], actuals: dict[str, Any]) -> list[dict[str, Any]]:
    """Human value lines for one effect (``label``/``value_text``/``detail``)."""
    lines: list[dict[str, Any]] = []
    values = effect.get("values") or []
    for value in values:
        if not isinstance(value, dict):
            continue
        key = str(value.get("key") or "")
        entry = actuals.get(key) or {}
        unit = _text(value.get("unit"))
        label = _text(value.get("label")) or key
        per_stack = entry.get("per_stack")
        if entry.get("value") is not None:
            value_text = _text(entry.get("display_actual")) or C.format_actual(entry.get("value"), unit)
            detail = _text(entry.get("basis"))
        elif per_stack is not None:
            value_text = f"{C.format_actual(per_stack, unit)}/层"
            detail = "层数由配置设置"
        elif entry.get("per_point") is not None:
            value_text = f"{C.format_actual(entry.get('per_point'), unit)}/点"
            detail = "实际点数未知"
        else:
            value_text = _text(value.get("display")) or "—"
            detail = ""
        lines.append({"label": label, "value_text": value_text, "detail": detail})
    return lines


def effect_summary(effect: dict[str, Any], actuals: dict[str, Any]) -> str:
    """Compact one-line summary, e.g. ``+75% 火焰穿透 · +15% 施法速度 · 6 秒 Buff``."""
    parts: list[str] = []
    for line in effect_value_lines(effect, actuals):
        if line["value_text"] == "—":
            continue
        parts.append(f"{line['value_text']} {line['label']}".strip())
    activation = effect.get("activation") or {}
    duration = activation.get("duration_s")
    if duration is not None and not any("持续" in line["label"] for line in effect_value_lines(effect, actuals)):
        parts.append(f"{C.format_actual(float(duration), 'seconds')} Buff")
    name = _text(effect.get("name"))
    if name and not parts:
        parts.append(name)
    return " · ".join(parts) if parts else name


def _condition_text(effect: dict[str, Any], registry: Registry) -> str:
    conditions = effect.get("conditions") or {}
    note = _text(conditions.get("note"))
    if note:
        return note
    labels: list[str] = []
    for group in ("all", "any"):
        for entry in conditions.get(group) or []:
            if not isinstance(entry, dict):
                continue
            state = registry.state(_text(entry.get("state")))
            if state is None:
                continue
            expected = entry.get("equals")
            label = _text(state.get("label")) or _text(entry.get("state"))
            if isinstance(expected, bool):
                labels.append(label if expected else f"非 {label}")
            else:
                labels.append(f"{label} = {expected}")
    if not labels:
        return ""
    return "；".join(labels)


def activation_text(effect: dict[str, Any]) -> str:
    activation = effect.get("activation") or {}
    kind = _text(activation.get("kind"))
    pieces: list[str] = []
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
    elif refresh == "no_refresh":
        pieces.append("不刷新")
    description = _text(activation.get("description"))
    if description:
        pieces.append(description)
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
    if kind in kind_labels:
        pieces.insert(0, kind_labels[kind])
    return "；".join(pieces)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class _Discovery:
    def __init__(
        self,
        snapshot: Any,
        registry: Registry,
        *,
        calc_input: Optional[dict[str, Any]] = None,
        build: Optional[dict[str, Any]] = None,
        catalog: Any = None,
        dps_scenario: Optional[dict[str, Any]] = None,
        binding: Optional[dict[str, Any]] = None,
    ) -> None:
        self.snapshot = snapshot
        self.registry = registry
        self.calc_input = calc_input if isinstance(calc_input, dict) else None
        self.build = build if isinstance(build, dict) else None
        self.db = catalog
        self.dps_scenario = dps_scenario if isinstance(dps_scenario, dict) else {}
        self.build_meta = dict(getattr(snapshot, "build_meta", {}) or {})
        self.binding = dict(binding or {})

        self.items: list[dict[str, Any]] = []
        self.mods: list[dict[str, Any]] = []
        self.specializations: list[dict[str, Any]] = []
        self.passive_nodes: list[dict[str, Any]] = []
        self.passive_tree: dict[str, Any] = {}
        self.contributions: list[Any] = list(getattr(snapshot, "contributions", []) or [])
        self.excluded_sources: list[dict[str, Any]] = list(getattr(snapshot, "excluded_sources", []) or [])
        self.contribution_by_id: dict[str, Any] = {
            str(getattr(contribution, "contribution_id", "")): contribution
            for contribution in self.contributions
        }
        self.item_source_paths: dict[str, str] = {}

        self.data_gaps: dict[str, dict[str, Any]] = {}
        self.effects: list[dict[str, Any]] = []
        self.registered_candidates: list[dict[str, Any]] = []
        self.heuristic_candidates: list[dict[str, Any]] = []
        self.checked_records: list[dict[str, Any]] = []
        self.auto_states: list[dict[str, Any]] = []
        self.auto_values: dict[str, Any] = {}
        self.baseline_states: list[dict[str, Any]] = []
        self.catalog_infos: dict[str, Any] = {}
        self.affected_by_state: dict[str, list[str]] = {}

        self._covered_mod_ids: set[str] = set()
        self._covered_node_keys: set[str] = set()
        self._covered_contribution_ids: set[str] = set()
        self._matched_instances: dict[str, str] = {}
        self._candidate_ids_used: set[str] = set()
        self._registry_covered_node_keys: set[str] = set()

    def _unique_candidate_id(self, base: str, effect_id: str) -> str:
        candidate_id = base
        if candidate_id in self._candidate_ids_used:
            candidate_id = f"{base}:{effect_id}" if effect_id else f"{base}:2"
        suffix = 2
        while candidate_id in self._candidate_ids_used:
            candidate_id = f"{base}:{effect_id or 'dup'}:{suffix}"
            suffix += 1
        self._candidate_ids_used.add(candidate_id)
        return candidate_id

    # -- utilities ---------------------------------------------------------
    def _add_gap(
        self,
        gap_id: str,
        *,
        severity: str,
        area: str,
        message: str,
        probe: str = "",
    ) -> None:
        if gap_id in self.data_gaps:
            return
        self.data_gaps[gap_id] = {
            "id": gap_id,
            "severity": severity,
            "area": area,
            "message": message,
            "probe": probe,
        }

    def _parse_inputs(self) -> None:
        if not self.calc_input:
            self._add_gap(
                "gap:calc-input-missing",
                severity="error",
                area="输入",
                message="计算输入（calc input）缺失，无法枚举物品/词缀/技能节点/被动节点来源。",
                probe="用快照 build_meta.calc_input.path 提供 --calc-input，或重新生成计算输入。",
            )
            return
        items = self.calc_input.get("items")
        if isinstance(items, list):
            self.items = [item for item in items if isinstance(item, dict)]
        mods = self.calc_input.get("mods")
        if isinstance(mods, list):
            self.mods = [mod for mod in mods if isinstance(mod, dict)]
        skills = self.calc_input.get("skills") or {}
        specializations = skills.get("specializations") if isinstance(skills, dict) else None
        if isinstance(specializations, list):
            self.specializations = [spec for spec in specializations if isinstance(spec, dict)]
        passive_tree = self.calc_input.get("passive_tree") or {}
        if isinstance(passive_tree, dict):
            self.passive_tree = passive_tree
            nodes = passive_tree.get("nodes")
            if isinstance(nodes, list):
                self.passive_nodes = [node for node in nodes if isinstance(node, dict)]
        for item in self.items:
            instance_id = _text(item.get("instance_id"))
            if not instance_id:
                continue
            if instance_id.startswith("idols."):
                self.item_source_paths[instance_id] = f"data.idols.entries.{instance_id.split('.')[-1]}"
            elif instance_id.startswith("blessings."):
                self.item_source_paths[instance_id] = f"data.blessings.entries.{instance_id.split('.')[-1]}"
            else:
                self.item_source_paths[instance_id] = f"data.{instance_id}"

    # -- registry matching -------------------------------------------------
    def _find_specialization(self, match: dict[str, Any]) -> Optional[dict[str, Any]]:
        target_tree = _text(match.get("tree"))
        target_entity = _text(match.get("tree_entity_id"))
        for spec in self.specializations:
            tree = spec.get("tree") or {}
            if not isinstance(tree, dict):
                tree = {}
            entity_id = _text(tree.get("entity_id"))
            name = _text(tree.get("name"))
            short = entity_id.rsplit(":", 1)[-1] if entity_id else ""
            if target_entity and entity_id == target_entity:
                return spec
            if target_tree and (
                name.casefold() == target_tree.casefold()
                or short.casefold() == target_tree.casefold()
                or entity_id.casefold() == target_tree.casefold()
            ):
                return spec
        return None

    def _find_node(self, spec: dict[str, Any], node_id: Any) -> tuple[Optional[dict[str, Any]], int]:
        nodes = spec.get("nodes") if isinstance(spec.get("nodes"), list) else []
        wanted = _text(node_id)
        for index, node in enumerate(nodes):
            if isinstance(node, dict) and _text(node.get("node_id")) == wanted:
                return node, index
        return None, -1

    def _find_passive_node(self, match: dict[str, Any]) -> Optional[dict[str, Any]]:
        tree = self.passive_tree.get("tree") if isinstance(self.passive_tree, dict) else None
        if isinstance(tree, dict):
            target_tree = _text(match.get("tree"))
            target_entity = _text(match.get("tree_entity_id"))
            entity_id = _text(tree.get("entity_id"))
            name = _text(tree.get("name"))
            short = entity_id.rsplit(":", 1)[-1] if entity_id else ""
            tree_matches = False
            if target_entity and entity_id == target_entity:
                tree_matches = True
            elif not target_entity and not target_tree:
                tree_matches = True
            elif target_tree and (
                name.casefold() == target_tree.casefold()
                or short.casefold() == target_tree.casefold()
                or entity_id.casefold() == target_tree.casefold()
            ):
                tree_matches = True
            if not tree_matches:
                return None
        wanted = _text(match.get("node_id"))
        for node in self.passive_nodes:
            if _text(node.get("node_id")) == wanted:
                return node
        return None

    def _match_effect(self, effect: dict[str, Any]) -> dict[str, Any]:
        match = effect.get("match") or {}
        match_type = _text(match.get("type"))
        effect_id = _text(effect.get("effect_id"))
        result: dict[str, Any] = {
            "type": match_type,
            "available": False,
            "points": None,
            "max_points": None,
            "reason": None,
            "matched_mod_ids": [],
            "instance_id": _text(match.get("instance_id")) or None,
            "tree_name": None,
            "tree_entity_id": None,
            "node_id": match.get("node_id"),
            "node_entry": None,
            "catalog_info": {},
            "texts": [],
            "locators": [],
        }
        if match_type == "skill_node":
            spec = self._find_specialization(match)
            if spec is None:
                result["reason"] = "当前构筑的 5 个技能专精中没有匹配的技能树"
                self._add_gap(
                    f"gap:source-absent:{effect_id}",
                    severity="warning",
                    area="发现",
                    message=f"注册效果 {effect_id} 的技能树来源在当前构筑中不存在。",
                    probe="核对 registry match.tree 是否与 calc input 的技能树名称一致。",
                )
                return result
            tree = spec.get("tree") or {}
            result["tree_name"] = _text(tree.get("name"))
            result["tree_entity_id"] = _text(tree.get("entity_id"))
            node, node_index = self._find_node(spec, match.get("node_id"))
            if node is None:
                result["reason"] = f"技能树 {result['tree_name']} 中没有已分配节点 {match.get('node_id')}"
                self._add_gap(
                    f"gap:source-absent:{effect_id}",
                    severity="warning",
                    area="发现",
                    message=result["reason"],
                    probe="核对节点 id 与 calc input specializations 是否一致。",
                )
                return result
            points = _as_int(node.get("points")) or 0
            max_points = _as_int(node.get("max_points"))
            min_points = _as_int(match.get("min_points")) or 1
            result["points"] = points
            result["max_points"] = max_points

            spec_index = self.specializations.index(spec)
            result["locators"].append(
                f"{_CALC_INPUT_REF}#/skills/specializations/{spec_index}/nodes/{node_index}"
            )
            if points < min_points:
                result["reason"] = f"已分配 {points} 点，少于要求的 {min_points} 点"
                return result
            result["available"] = True
            result["node_entry"] = node
            result["catalog_info"] = self._enrich_node(result["tree_entity_id"], _text(node.get("node_id")))
            info = result["catalog_info"]
            texts = [_text(tree.get("name")), _text(node.get("name")), _text(info.get("node_description"))]
            texts += [line.get("raw_text") or "" for line in info.get("node_effects") or []]
            result["texts"] = [text for text in texts if text]
            return result
        if match_type == "passive_node":
            node = self._find_passive_node(match)
            tree = self.passive_tree.get("tree") if isinstance(self.passive_tree, dict) else {}
            if node is None:
                result["reason"] = "职业被动树中没有匹配的已分配节点"
                self._add_gap(
                    f"gap:source-absent:{effect_id}",
                    severity="warning",
                    area="发现",
                    message=f"注册效果 {effect_id} 的被动节点来源在当前构筑中不存在。",
                    probe="核对 registry match.tree/node_id 与 calc input passive_tree 是否一致。",
                )
                return result
            points = _as_int(node.get("points")) or 0
            max_points = _as_int(node.get("max_points"))
            min_points = _as_int(match.get("min_points")) or 1
            result["points"] = points
            result["max_points"] = max_points
            tree = tree if isinstance(tree, dict) else {}
            result["tree_name"] = _text(tree.get("name"))
            result["tree_entity_id"] = _text(tree.get("entity_id"))
            result["node_entry"] = node
            node_index = self.passive_nodes.index(node)
            result["locators"].append(f"{_CALC_INPUT_REF}#/passive_tree/nodes/{node_index}")
            if points < min_points:
                result["reason"] = f"已分配 {points} 点，少于要求的 {min_points} 点"
                return result
            result["available"] = True
            result["catalog_info"] = self._enrich_node(result["tree_entity_id"], _text(node.get("node_id")))
            info = result["catalog_info"]
            texts = [_text(node.get("name")), _text(info.get("node_description"))]
            texts += [line.get("raw_text") or "" for line in info.get("node_effects") or []]
            result["texts"] = [text for text in texts if text]
            return result

        # equipment / mod id matches --------------------------------------
        instance_id = _text(match.get("instance_id"))
        mod_id_spec = _text(match.get("mod_id"))
        name_contains = _text(match.get("name_contains")).casefold()
        matched_mods: list[dict[str, Any]] = []
        for mod in self.mods:
            mod_id = _text(mod.get("mod_id"))
            if instance_id and _text(mod.get("item_instance_id")) != instance_id:
                continue
            if mod_id_spec:
                if not (mod_id == mod_id_spec or mod_id.startswith(mod_id_spec)):
                    continue
            if name_contains:
                haystack = " ".join(
                    (
                        _text(mod.get("readable_text")),
                        _text((mod.get("stat") or {}).get("name")),
                        _text(mod.get("display_value")),
                    )
                ).casefold()
                if name_contains not in haystack:
                    continue
            matched_mods.append(mod)
        item = None
        for candidate in self.items:
            if _text(candidate.get("instance_id")) == instance_id:
                item = candidate
                break
        if instance_id and item is None and not matched_mods:
            result["reason"] = f"当前构筑没有装备/神像实例 {instance_id}"
            self._add_gap(
                f"gap:source-absent:{effect_id}",
                severity="warning",
                area="发现",
                message=result["reason"],
                probe="核对 registry match.instance_id 与 calc input items 是否一致。",
            )
            return result
        if not matched_mods and not instance_id:
            result["reason"] = "注册效果的 match 无法定位到当前构筑来源"
            self._add_gap(
                f"gap:source-absent:{effect_id}",
                severity="warning",
                area="发现",
                message=f"{result['reason']}（effect_id={effect_id}）。",
                probe="核对 registry match 的 instance_id/mod_id/name_contains。",
            )
            return result
        result["available"] = True
        result["matched_mod_ids"] = [_text(mod.get("mod_id")) for mod in matched_mods if mod.get("mod_id")]
        if item is not None:
            result["instance_id"] = instance_id
        texts: list[str] = []
        if item is not None:
            texts.append(_text(item.get("display_name")))
        for mod in matched_mods:
            texts.extend(
                (
                    _text(mod.get("readable_text")),
                    _text((mod.get("stat") or {}).get("name")),
                    _text(mod.get("display_value")),
                )
            )
        result["texts"] = [text for text in texts if text]
        return result

    def _enrich_node(self, tree_entity_id: str, node_id: str) -> dict[str, Any]:
        info: dict[str, Any] = {
            "node_description": None,
            "node_effects": [],
            "extensions": {},
            "buff_names": [],
            "cue_texts": [],
            "stat_lines": [],
        }
        if self.db is None or not tree_entity_id:
            return info
        try:
            node = self.db.node_by_index(tree_entity_id, int(node_id))
        except (Exception, _CatalogError) as exc:  # noqa: BLE001 - degrade on any catalog failure
            self._add_gap(
                f"gap:catalog-node:{tree_entity_id}:{node_id}",
                severity="warning",
                area="目录",
                message=f"catalog 无法读取节点 {tree_entity_id}:{node_id}（{exc}）。",
                probe="核对 data/current.json 指向的数据集版本。",
            )
            return info
        info["node_description"] = node.get("description")
        extensions = node.get("extensions") if isinstance(node.get("extensions"), dict) else {}
        info["extensions"] = {
            key: extensions.get(key)
            for key in (
                "noScalingType",
                "noScalingPointThreshold",
                "node_size",
                "mastery",
                "masteryRequirement",
            )
            if key in extensions
        }
        entity_id = _text(node.get("entity_id"))
        if entity_id:
            try:
                effects = self.db.effects_for_owner(entity_id)
            except (Exception, _CatalogError) as exc:  # noqa: BLE001
                self._add_gap(
                    f"gap:catalog-effects:{entity_id}",
                    severity="warning",
                    area="目录",
                    message=f"catalog 无法读取节点效果 {entity_id}（{exc}）。",
                    probe="核对数据集完整性。",
                )
                effects = []
            text_parts: list[str] = []
            for effect in effects[:24]:
                ext = effect.get("extensions") if isinstance(effect.get("extensions"), dict) else {}
                line = {
                    "effect_id": _text(effect.get("effect_id")),
                    "category": _text(effect.get("effect_category")),
                    "stat_name": _text(ext.get("stat_name")),
                    "value_text": _text(ext.get("value_text")),
                    "raw_text": _text(effect.get("raw_text")),
                    "buff_name": _text(ext.get("buff_name")),
                    "parameter": _text(ext.get("parameter")),
                    "no_scaling": bool(ext.get("noScaling")),
                    "property": ext.get("property"),
                }
                info["node_effects"].append(line)
                if line["buff_name"]:
                    info["buff_names"].append(line["buff_name"])
                stat_line = " ".join(part for part in (line["stat_name"], line["value_text"]) if part).strip()
                if stat_line:
                    info["stat_lines"].append(stat_line)
                text_parts.extend((line["raw_text"], line["stat_name"], line["value_text"]))
            info["cue_texts"] = [text for text in text_parts if text]
        return info

    # -- existing handling -------------------------------------------------
    def _contribution_ids_for_match(self, effect: dict[str, Any], match: dict[str, Any]) -> list[str]:
        ids: list[str] = []
        explicit = effect.get("existing_contribution_ids")
        if isinstance(explicit, list):
            ids.extend(str(item) for item in explicit if item)
        codes = effect.get("existing_condition_codes")
        if isinstance(codes, list):
            for contribution in self.contributions:
                if getattr(contribution, "condition_code", None) in codes:
                    ids.append(str(getattr(contribution, "contribution_id", "")))
        for mod_id in match.get("matched_mod_ids") or []:
            ids.append(f"mod:{mod_id}")
        match_type = _text(match.get("type"))
        tree_short = _text(match.get("tree_entity_id")).rsplit(":", 1)[-1]
        if match_type == "skill_node" and tree_short:
            prefix = f"skill:{tree_short}:{_text(match.get('node_id'))}:"
            ids.extend(
                str(getattr(contribution, "contribution_id", ""))
                for contribution in self.contributions
                if str(getattr(contribution, "contribution_id", "")).startswith(prefix)
            )
        elif match_type == "passive_node":
            prefix = f"passive:{tree_short}:{_text(match.get('node_id'))}:"
            ids.extend(
                str(getattr(contribution, "contribution_id", ""))
                for contribution in self.contributions
                if str(getattr(contribution, "contribution_id", "")).startswith(prefix)
            )
        elif match_type in ("equipment_mod", "mod_id") and match.get("instance_id") and not match.get(
            "matched_mod_ids"
        ):
            source_path = self.item_source_paths.get(_text(match.get("instance_id")))
            if source_path:
                ids.extend(
                    str(getattr(contribution, "contribution_id", ""))
                    for contribution in self.contributions
                    if _text(getattr(contribution, "source_path", "")) == source_path
                )
        cleaned: list[str] = []
        for contribution_id in ids:
            if contribution_id and contribution_id not in cleaned:
                cleaned.append(contribution_id)
        return sorted(cleaned)

    def _existing_handling(self, effect: dict[str, Any], match: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for contribution_id in self._contribution_ids_for_match(effect, match):
            contribution = self.contribution_by_id.get(contribution_id)
            if contribution is not None:
                rows.append(contribution_entry(contribution))
        return rows

    # -- registry effects --------------------------------------------------
    def _resolve_effect(self, effect: dict[str, Any]) -> dict[str, Any]:
        match = self._match_effect(effect)
        points = match.get("points")
        binding = self.registry.control_binding(effect)
        stacks = None
        if binding.get("kind") == "stacks":
            stacks = None  # actual per-stack values are resolved at evaluation time
        resolved = resolve_actuals(effect, points=points, stacks=stacks)
        actuals = resolved["values"]
        binding_kind = binding.get("kind") if isinstance(binding, dict) else None
        for key in resolved["unresolved"]:
            value = None
            for raw in effect.get("values") or []:
                if isinstance(raw, dict) and str(raw.get("key")) == key:
                    value = raw
                    break
            if (
                binding_kind == "stacks"
                and isinstance(value, dict)
                and value.get("per_stack") is not None
                and value.get("constant") is None
                and value.get("per_point") is None
            ):
                # per-stack values are resolved at evaluation time from the
                # scenario's stack count; not a discovery data gap.
                continue
            reason = "缺少实际点数/层数，无法解析数值"
            self._add_gap(
                f"gap:actual-unresolved:{_text(effect.get('effect_id'))}:{key}",
                severity="warning",
                area="发现",
                message=f"效果 {effect.get('effect_id')} 的数值 {key} 无法解析（{reason}）。",
                probe="核对 registry values 使用了 constant 还是 per_point；装备类效果应使用 constant/roll。",
            )
        existing_handling = self._existing_handling(effect, match)
        baseline_relation = _text((effect.get("relevance") or {}).get("baseline_relation"))
        if baseline_relation == "already_in_baseline":
            active = [
                row
                for row in existing_handling
                if row["included"] and row["condition_status"] in ("always", "satisfied")
            ]
            if not active:
                self._add_gap(
                    f"gap:baseline-relation:{_text(effect.get('effect_id'))}",
                    severity="warning",
                    area="发现",
                    message=(
                        f"效果 {effect.get('effect_id')} 标记为 already_in_baseline，"
                        "但在当前来源账中找不到生效的既有贡献。"
                    ),
                    probe="核对 relevance.baseline_relation 与 snapshot contributions。",
                )
        active_ids = [
            row["contribution_id"]
            for row in existing_handling
            if row["included"] and row["condition_status"] in ("always", "satisfied")
        ]
        source_id = active_ids[0] if active_ids else (existing_handling[0]["contribution_id"] if existing_handling else None)
        # mark coverage so heuristics do not report the same source again
        self._covered_mod_ids.update(match.get("matched_mod_ids") or [])
        if match.get("type") == "skill_node":
            short = _text(match.get("tree_entity_id")).rsplit(":", 1)[-1]
            self._covered_node_keys.add(f"skill:{short}:{_text(match.get('node_id'))}")
        elif match.get("type") == "passive_node":
            short = _text(match.get("tree_entity_id")).rsplit(":", 1)[-1]
            self._covered_node_keys.add(f"passive:{short}:{_text(match.get('node_id'))}")
        self._covered_contribution_ids.update(row["contribution_id"] for row in existing_handling)
        if match.get("instance_id"):
            self._matched_instances[_text(match.get("instance_id"))] = _text(effect.get("effect_id"))
        entry = {
            "effect_id": _text(effect.get("effect_id")),
            "effect": effect,
            "actuals": actuals,
            "actuals_unresolved": list(resolved["unresolved"]),
            "qualification": {
                "available": bool(match.get("available")),
                "points": match.get("points"),
                "max_points": match.get("max_points"),
                "reason": match.get("reason"),
            },
            "existing_handling": existing_handling,
            "source_id": source_id,
            "source_detail": self._source_detail(effect, match),
            "catalog_info": match.get("catalog_info") or {},
            "control": binding,
            "category": effect_page_category(effect),
            "value_lines": effect_value_lines(effect, actuals),
            "summary": effect_summary(effect, actuals),
        }
        self.effects.append(entry)
        cues = scan_cues(" ".join(match.get("texts") or []))
        activation = effect.get("activation") or {}
        if not cues:
            if activation.get("kind") in (
                "persistent_buff",
                "stacks_buff",
                "conditional_passive",
                "numeric_threshold",
                "cast_state",
            ):
                cues = ["conditional"]
            if (effect.get("control") or {}).get("kind") in ("toggle", "stacks"):
                cues = sorted(set(cues) | {"conditional"}, key=lambda name: _CUE_ORDER.get(name, 99))
        self.registered_candidates.append(
            {
                "candidate_id": self._unique_candidate_id(
                    self._registered_candidate_id(effect, match),
                    _text(effect.get("effect_id")),
                ),
                "effect_id": _text(effect.get("effect_id")),
                "source_label": _text((effect.get("source") or {}).get("display")),
                "category": effect_page_category(effect),
                "relevance_status": _text((effect.get("relevance") or {}).get("status")),
                "condition_cues": cues,
                "effect_summary": entry["summary"],
                "support_level": _text((effect.get("support") or {}).get("level")),
                "disposition": "registered",
                "available": bool(match.get("available")),
                "points": match.get("points"),
                "source_id": source_id,
                "source_kind": _text((effect.get("source") or {}).get("kind")),
                "instance_id": match.get("instance_id"),
                "node_key": self._match_node_key(match),
            }
        )
        return entry

    @staticmethod
    def _match_node_key(match: dict[str, Any]) -> Optional[str]:
        match_type = _text(match.get("type"))
        short = _text(match.get("tree_entity_id")).rsplit(":", 1)[-1]
        if match_type == "skill_node" and short:
            return f"skill:{short}:{_text(match.get('node_id'))}"
        if match_type == "passive_node" and short:
            return f"passive:{short}:{_text(match.get('node_id'))}"
        return None

    @staticmethod
    def _registered_candidate_id(effect: dict[str, Any], match: dict[str, Any]) -> str:
        effect_id = _text(effect.get("effect_id"))
        match_type = _text(match.get("type"))
        short = _text(match.get("tree_entity_id")).rsplit(":", 1)[-1]
        if match_type == "skill_node" and short:
            return f"cand:skill:{short}:{_text(match.get('node_id'))}"
        if match_type == "passive_node" and short:
            return f"cand:passive:{short}:{_text(match.get('node_id'))}"
        if match.get("matched_mod_ids"):
            return f"cand:mod:{match['matched_mod_ids'][0]}"
        if match.get("instance_id"):
            return f"cand:item:{match['instance_id']}"
        return f"cand:effect:{effect_id}"

    def _source_detail(self, effect: dict[str, Any], match: dict[str, Any]) -> dict[str, Any]:
        source = effect.get("source") or {}
        detail = {
            "kind": _text(source.get("kind")),
            "tree": source.get("tree") or match.get("tree_name"),
            "node_id": source.get("node_id") if source.get("node_id") is not None else match.get("node_id"),
            "points": match.get("points"),
            "max_points": match.get("max_points"),
            "slot": source.get("slot"),
            "item": source.get("item_display_name"),
            "locator": source.get("locator"),
        }
        if not detail["locator"] and match.get("locators"):
            detail["locator"] = match["locators"][0]
        return detail

    # -- heuristic candidates ---------------------------------------------
    def _registry_candidate_records(self) -> list[dict[str, Any]]:
        """Convert registry ``checked_records`` with disposition=candidate to candidates.

        Registry research already curates "checked but not (yet) modelled"
        entries; surfacing those as unconfirmed candidates keeps the page's
        "待确认" area complete without re-deriving them heuristically.
        """
        self._registry_covered_node_keys: set[str] = set()
        converted: list[dict[str, Any]] = []
        for record in self.registry.checked_records:
            if not isinstance(record, dict):
                continue
            record_id = _text(record.get("record_id"))
            match = re.match(r"^checked:(skill|passive):([^:]+):([^:]+)$", record_id)
            node_key = f"{match.group(1)}:{match.group(2)}:{match.group(3)}" if match else None
            instance_match = re.match(r"^checked:(?:item|blessing):([^:]+)$", record_id)
            instance_id = instance_match.group(1) if instance_match else None
            disposition = _text(record.get("disposition"))
            if node_key:
                self._registry_covered_node_keys.add(node_key)
            if disposition != "candidate":
                continue
            if node_key and node_key in self._covered_node_keys:
                # already represented by a registered effect + registered candidate
                continue
            category = _text(record.get("category")) or "other_skill"
            page_category = {
                "main_skill": "main_skill",
                "other_skill": "other_skill",
                "passive": "passive",
                "equipment": "equipment",
                "target": "target",
                "action": "action",
                "scope": "equipment",
            }.get(category, category)
            summary = _text(record.get("effect_summary"))
            converted.append(
                {
                    "candidate_id": f"cand:record:{record_id}",
                    "effect_id": None,
                    "source_label": _text(record.get("source_label")),
                    "category": page_category,
                    "relevance_status": "unconfirmed",
                    "condition_cues": scan_cues(summary) or ["conditional"],
                    "effect_summary": summary,
                    "support_level": "mechanism_unresolved",
                    "disposition": "checked_candidate",
                    "available": True,
                    "points": None,
                    "source_id": record.get("related_effect_id"),
                    "source_kind": (
                        "skill_node"
                        if node_key and node_key.startswith("skill:")
                        else (
                            "passive_node"
                            if node_key and node_key.startswith("passive:")
                            else "equipment"
                        )
                    ),
                    "instance_id": instance_id,
                    "node_key": node_key,
                }
            )
        return converted

    def _heuristic_candidates(self) -> None:
        # item mods ---------------------------------------------------------
        item_by_instance = {_text(item.get("instance_id")): item for item in self.items}
        for mod in self.mods:
            mod_id = _text(mod.get("mod_id"))
            if not mod_id or mod_id in self._covered_mod_ids:
                continue
            stat = mod.get("stat") or {}
            texts = [
                _text(mod.get("readable_text")),
                _text(stat.get("name")),
                _text(mod.get("display_value")),
            ]
            instance_id = _text(mod.get("item_instance_id"))
            item = item_by_instance.get(instance_id) or {}
            stat_name = _text((mod.get("stat") or {}).get("name"))
            if instance_id in self._matched_instances and _text(mod.get("kind")) == "description":
                # tooltip description lines of an item whose unique effect is
                # already modelled by a registered effect are documentation,
                # not separate candidates.
                continue
            if "level of" in stat_name.casefold() and "skill" in stat_name.casefold():
                # "+X to Level of Skills" lines are structural/informational in
                # v1, not conditional damage effects.
                continue
            if item:
                texts.append(_text(item.get("display_name")))
            text = " ".join(text for text in texts if text)
            cues = scan_cues(text)
            strong = [cue for cue in cues if cue in STRONG_CUES]
            conditions = mod.get("conditions")
            has_conditions = isinstance(conditions, (list, dict)) and bool(conditions)
            if strong and not _looks_defensive(text):
                display_cues = sorted(set(strong) | {"conditional"}, key=lambda name: _CUE_ORDER.get(name, 99))
                self.heuristic_candidates.append(
                    {
                        "candidate_id": f"cand:mod:{mod_id}",
                        "effect_id": None,
                        "source_label": f"{_text(item.get('display_name')) or instance_id} · {_text(stat.get('name')) or _text(mod.get('readable_text'))[:60]}",
                        "category": item_page_category(item) if item else "equipment",
                        "relevance_status": "unconfirmed",
                        "condition_cues": display_cues,
                        "effect_summary": _text(mod.get("readable_text")) or text[:160],
                        "support_level": "mechanism_unresolved",
                        "disposition": "unregistered",
                        "available": True,
                        "points": None,
                        "source_id": f"mod:{mod_id}",
                        "source_kind": (
                            "idol"
                            if instance_id.startswith("idols.")
                            else (
                                "idol_altar"
                                if instance_id == "equipment.idol_altar"
                                else ("blessing" if instance_id.startswith("blessings.") else "equipment")
                            )
                        ),
                        "instance_id": instance_id or None,
                        "node_key": None,
                    }
                )
                continue
            if has_conditions and not _looks_defensive(text):
                self.heuristic_candidates.append(
                    {
                        "candidate_id": f"cand:mod:{mod_id}",
                        "effect_id": None,
                        "source_label": f"{_text(item.get('display_name')) or instance_id} · {_text(stat.get('name')) or mod_id}",
                        "category": item_page_category(item) if item else "equipment",
                        "relevance_status": "unconfirmed",
                        "condition_cues": ["conditional"],
                        "effect_summary": _text(mod.get("readable_text")) or text[:160],
                        "support_level": "mechanism_unresolved",
                        "disposition": "unregistered",
                        "available": True,
                        "points": None,
                        "source_id": f"mod:{mod_id}",
                        "source_kind": "equipment",
                        "instance_id": instance_id or None,
                        "node_key": None,
                    }
                )
                continue
            # checked record for non-candidate mods is generated later

        # skill nodes -------------------------------------------------------
        if self.specializations:
            for spec in self.specializations:
                tree = spec.get("tree") or {}
                tree_entity_id = _text(tree.get("entity_id"))
                tree_short = tree_entity_id.rsplit(":", 1)[-1] if tree_entity_id else ""
                tree_name = _text(tree.get("name"))
                nodes = spec.get("nodes") if isinstance(spec.get("nodes"), list) else []
                for index, node in enumerate(nodes):
                    if not isinstance(node, dict):
                        continue
                    node_key = f"skill:{tree_short}:{_text(node.get('node_id'))}"
                    if node_key in self._covered_node_keys or node_key in self._registry_covered_node_keys:
                        continue
                    info = self._enrich_node(tree_entity_id, _text(node.get("node_id")))
                    self.catalog_infos[node_key] = info
                    texts = [_text(tree_name), _text(node.get("name")), _text(info.get("node_description"))]
                    texts += list(info.get("cue_texts") or [])
                    cues = scan_cues(" ".join(text for text in texts if text))
                    strong = [cue for cue in cues if cue in STRONG_CUES]
                    extensions = info.get("extensions") or {}
                    threshold = extensions.get("noScalingPointThreshold") or 0
                    if threshold or extensions.get("noScalingType"):
                        strong = sorted(set(strong) | {"conditional"}, key=lambda name: _CUE_ORDER.get(name, 99))
                    if info.get("node_effects") and any(
                        line.get("category") in ("buff_param_candidate", "buff_mod_config", "config_mod")
                        for line in info.get("node_effects") or []
                    ):
                        strong = sorted(set(strong) | {"buff"}, key=lambda name: _CUE_ORDER.get(name, 99))
                    if not strong:
                        continue
                    category = "main_skill" if "black hole" in tree_name.casefold() else "other_skill"
                    self.heuristic_candidates.append(
                        {
                            "candidate_id": f"cand:skill:{tree_short}:{_text(node.get('node_id'))}",
                            "effect_id": None,
                            "source_label": f"{tree_name} · {_text(node.get('name'))} · {_as_int(node.get('points')) or 0} 点",
                            "category": category,
                            "relevance_status": "unconfirmed",
                            "condition_cues": strong,
                            "effect_summary": _text(info.get("node_description"))
                            or "、".join(info.get("stat_lines") or [])[:200],
                            "support_level": "mechanism_unresolved",
                            "disposition": "unregistered",
                            "available": True,
                            "points": _as_int(node.get("points")),
                            "source_id": None,
                            "source_kind": "skill_node",
                            "instance_id": None,
                            "node_key": f"skill:{tree_short}:{_text(node.get('node_id'))}",
                        }
                    )

        # passive nodes -----------------------------------------------------
        passive_tree = self.passive_tree.get("tree") if isinstance(self.passive_tree, dict) else {}
        tree_entity_id = _text((passive_tree or {}).get("entity_id"))
        tree_short = tree_entity_id.rsplit(":", 1)[-1] if tree_entity_id else "mg-1"
        for node in self.passive_nodes:
            node_key = f"passive:{tree_short}:{_text(node.get('node_id'))}"
            if node_key in self._covered_node_keys or node_key in self._registry_covered_node_keys:
                continue
            info = self._enrich_node(tree_entity_id, _text(node.get("node_id")))
            self.catalog_infos[node_key] = info
            texts = [_text(node.get("name")), _text(info.get("node_description"))]
            texts += list(info.get("cue_texts") or [])
            cues = scan_cues(" ".join(text for text in texts if text))
            strong = [cue for cue in cues if cue in STRONG_CUES]
            extensions = info.get("extensions") or {}
            if extensions.get("noScalingPointThreshold") or extensions.get("noScalingType"):
                strong = sorted(set(strong) | {"conditional"}, key=lambda name: _CUE_ORDER.get(name, 99))
            if not strong:
                continue
            self.heuristic_candidates.append(
                {
                    "candidate_id": f"cand:passive:{tree_short}:{_text(node.get('node_id'))}",
                    "effect_id": None,
                    "source_label": f"{_text((passive_tree or {}).get('name')) or '职业被动'} · {_text(node.get('name'))} · {_as_int(node.get('points')) or 0} 点",
                    "category": "passive",
                    "relevance_status": "unconfirmed",
                    "condition_cues": strong,
                    "effect_summary": _text(info.get("node_description"))
                    or "、".join(info.get("stat_lines") or [])[:200],
                    "support_level": "mechanism_unresolved",
                    "disposition": "unregistered",
                    "available": True,
                    "points": _as_int(node.get("points")),
                    "source_id": None,
                    "source_kind": "passive_node",
                    "instance_id": None,
                    "node_key": f"passive:{tree_short}:{_text(node.get('node_id'))}",
                }
            )

        # conditional contributions not covered by a registered effect ------
        for contribution in self.contributions:
            contribution_id = _text(getattr(contribution, "contribution_id", ""))
            if not contribution_id or contribution_id in self._covered_contribution_ids:
                continue
            condition_code = getattr(contribution, "condition_code", None)
            condition_status = _text(getattr(contribution, "condition_status", "always"))
            if not condition_code and condition_status == "always":
                continue
            if contribution_id.startswith("mod:"):
                base_mod_id = contribution_id[len("mod:") :].split(":", 1)[0]
                if base_mod_id in self._covered_mod_ids:
                    continue
            node_key = _contribution_node_key(contribution_id)
            if node_key and node_key in self._covered_node_keys:
                continue
            source_kind = _text(getattr(contribution, "source_kind", "system"))
            category = {
                "passive_node": "passive",
                "skill_node": "other_skill",
                "equipment": "equipment",
                "idol": "idol",
                "blessing": "equipment",
                "idol_altar": "equipment",
            }.get(source_kind, "equipment")
            label = _text(getattr(contribution, "source_label", "")) or contribution_id
            if _looks_defensive(f"{label} {_text(getattr(contribution, 'stat_key', ''))} {condition_code or ''}"):
                continue
            self.heuristic_candidates.append(
                {
                    "candidate_id": f"cand:contribution:{contribution_id}",
                    "effect_id": None,
                    "source_label": label,
                    "category": category,
                    "relevance_status": "unconfirmed",
                    "condition_cues": ["conditional"],
                    "effect_summary": (
                        f"条件贡献 {condition_code or condition_status}："
                        f"{_text(getattr(contribution, 'stat_key', ''))} "
                        f"{float(getattr(contribution, 'value', 0.0) or 0.0):g}"
                    ),
                    "support_level": "mechanism_unresolved",
                    "disposition": "unregistered",
                    "available": True,
                    "points": None,
                    "source_id": None,
                    "source_kind": source_kind,
                    "instance_id": None,
                    "node_key": _contribution_node_key(contribution_id),
                }
            )

    # -- checked records ---------------------------------------------------
    def _registry_checked_records(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for record in self.registry.checked_records:
            if not isinstance(record, dict):
                continue
            disposition = _text(record.get("disposition"))
            relevance = _text(record.get("relevance"))
            if relevance not in C.RELEVANCE_STATUSES:
                if disposition == "excluded":
                    relevance = "not_relevant_current_scenario"
                elif disposition == "baseline_included":
                    relevance = "relevant"
                else:
                    relevance = "unconfirmed"
            out.append(
                {
                    "record_id": _text(record.get("record_id")),
                    "category": _text(record.get("category")) or "scope",
                    "source_label": _text(record.get("source_label")),
                    "effect_summary": _text(record.get("effect_summary")),
                    "disposition": disposition,
                    "disposition_label": C.DISPOSITION_LABELS.get(disposition, disposition),
                    "reason": _text(record.get("reason")),
                    "relevance": relevance,
                    "related_effect_id": record.get("related_effect_id"),
                    "evidence": C.evidence_entries(record.get("evidence")),
                }
            )
        return out

    def _generate_checked_records(self) -> list[dict[str, Any]]:
        generated: list[dict[str, Any]] = []
        candidate_mod_ids = {
            candidate["candidate_id"].split("cand:mod:", 1)[1]
            for candidate in self.heuristic_candidates
            if candidate["candidate_id"].startswith("cand:mod:")
        }
        registered_mod_ids = set(self._covered_mod_ids)
        item_by_instance = {_text(item.get("instance_id")): item for item in self.items}
        for mod in self.mods:
            mod_id = _text(mod.get("mod_id"))
            if not mod_id or mod_id in candidate_mod_ids or mod_id in registered_mod_ids:
                continue
            contribution = self.contribution_by_id.get(f"mod:{mod_id}")
            instance_id = _text(mod.get("item_instance_id"))
            item = item_by_instance.get(instance_id) or {}
            stat = mod.get("stat") or {}
            if contribution is not None and _is_active_contribution(contribution):
                disposition = "baseline_included"
            else:
                disposition = "excluded"
            reason = ""
            if contribution is not None:
                reason = _text(getattr(contribution, "exclusion_reason", ""))
            if not reason:
                reason = "启发式未命中条件线索；当前场景下未发现与 Black Hole 本体/点燃/施法/CD/持续的关联。"
            generated.append(
                {
                    "record_id": f"checked:mod:{mod_id}",
                    "category": item_page_category(item) if item else "equipment",
                    "source_label": f"{_text(item.get('display_name')) or instance_id} · {_text(stat.get('name')) or mod_id}",
                    "effect_summary": _text(mod.get("readable_text"))
                    or f"{_text(stat.get('name'))} {_text(mod.get('display_value'))}".strip(),
                    "disposition": disposition,
                    "reason": reason,
                    "relevance": (
                        "relevant" if disposition == "baseline_included" else "not_relevant_current_scenario"
                    ),
                    "related_effect_id": None,
                }
            )
        # skill nodes -------------------------------------------------------
        for spec in self.specializations:
            tree = spec.get("tree") or {}
            tree_entity_id = _text(tree.get("entity_id"))
            tree_short = tree_entity_id.rsplit(":", 1)[-1] if tree_entity_id else ""
            tree_name = _text(tree.get("name"))
            category = "main_skill" if "black hole" in tree_name.casefold() else "other_skill"
            nodes = spec.get("nodes") if isinstance(spec.get("nodes"), list) else []
            for index, node in enumerate(nodes):
                if not isinstance(node, dict):
                    continue
                node_key = f"skill:{tree_short}:{_text(node.get('node_id'))}"
                if node_key in self._covered_node_keys:
                    continue
                if any(
                    candidate["candidate_id"] == f"cand:skill:{tree_short}:{_text(node.get('node_id'))}"
                    for candidate in self.heuristic_candidates
                ):
                    continue
                info = self.catalog_infos.get(node_key) or {}
                prefix = f"skill:{tree_short}:{_text(node.get('node_id'))}:"
                active = any(
                    str(getattr(contribution, "contribution_id", "")).startswith(prefix)
                    and _is_active_contribution(contribution)
                    for contribution in self.contributions
                )
                disposition = "baseline_included" if active else "excluded"
                generated.append(
                    {
                        "record_id": f"checked:skill:{tree_short}:{_text(node.get('node_id'))}",
                        "category": category,
                        "source_label": f"{tree_name} · {_text(node.get('name'))} · {_as_int(node.get('points')) or 0} 点",
                        "effect_summary": _text(info.get("node_description"))
                        or "、".join(info.get("stat_lines") or [])[:180],
                        "disposition": disposition,
                        "reason": "Black Hole 规则包/来源账已计入该节点效果，不重复配置。"
                        if active
                        else "未发现与 Black Hole 输出相关的未计入增量。",
                        "relevance": "relevant" if active else "not_relevant_current_scenario",
                        "related_effect_id": None,
                    }
                )
        # passive nodes -----------------------------------------------------
        passive_tree = self.passive_tree.get("tree") if isinstance(self.passive_tree, dict) else {}
        tree_short = _text((passive_tree or {}).get("entity_id")).rsplit(":", 1)[-1] or "mg-1"
        for node in self.passive_nodes:
            node_key = f"passive:{tree_short}:{_text(node.get('node_id'))}"
            if node_key in self._covered_node_keys:
                continue
            if any(
                candidate["candidate_id"] == f"cand:passive:{tree_short}:{_text(node.get('node_id'))}"
                for candidate in self.heuristic_candidates
            ):
                continue
            prefix = f"passive:{tree_short}:{_text(node.get('node_id'))}:"
            active = any(
                str(getattr(contribution, "contribution_id", "")).startswith(prefix)
                and _is_active_contribution(contribution)
                for contribution in self.contributions
            )
            info = self.catalog_infos.get(node_key) or {}
            disposition = "baseline_included" if active else "excluded"
            generated.append(
                {
                    "record_id": f"checked:passive:{tree_short}:{_text(node.get('node_id'))}",
                    "category": "passive",
                    "source_label": f"职业被动 · {_text(node.get('name'))} · {_as_int(node.get('points')) or 0} 点",
                    "effect_summary": _text(info.get("node_description"))
                    or "、".join(info.get("stat_lines") or [])[:180],
                    "disposition": disposition,
                    "reason": "常驻被动效果已计入基础属性/来源账，不重复配置。"
                    if active
                    else "未发现与 Black Hole 输出相关的未计入增量。",
                    "relevance": "relevant" if active else "not_relevant_current_scenario",
                    "related_effect_id": None,
                }
            )
        return generated

    def _merge_checked_records(self) -> list[dict[str, Any]]:
        registry_records = self._registry_checked_records()
        seen_ids = {record["record_id"] for record in registry_records if record.get("record_id")}
        seen_labels = {
            (record.get("category"), record.get("source_label"))
            for record in registry_records
            if record.get("source_label")
        }
        merged = list(registry_records)
        for record in self._generate_checked_records():
            if record["record_id"] in seen_ids:
                continue
            label_key = (record.get("category"), record.get("source_label"))
            if label_key in seen_labels:
                continue
            seen_ids.add(record["record_id"])
            seen_labels.add(label_key)
            merged.append(record)
        merged.sort(
            key=lambda record: (
                C.PAGE_CATEGORY_ORDER.index(record.get("category"))
                if record.get("category") in C.PAGE_CATEGORY_ORDER
                else len(C.PAGE_CATEGORY_ORDER),
                str(record.get("record_id")),
            )
        )
        return merged

    # -- source instances / denominators ----------------------------------
    def _candidate_scope_maps(
        self, candidates: list[dict[str, Any]]
    ) -> tuple[
        dict[str, list[str]],
        dict[str, list[str]],
        dict[tuple[str, str], list[str]],
        set[tuple[str, str]],
        set[tuple[str, str]],
    ]:
        item_map: dict[str, list[str]] = {}
        spec_map: dict[str, list[str]] = {}
        passive_map: dict[tuple[str, str], list[str]] = {}
        skill_nodes: set[tuple[str, str]] = set()
        passive_nodes: set[tuple[str, str]] = set()
        for candidate in candidates:
            candidate_id = str(candidate.get("candidate_id") or "")
            value = candidate.get("effect_id") or candidate_id
            node_key = _text(candidate.get("node_key"))
            instance_id = _text(candidate.get("instance_id"))
            mapped = False
            if node_key.startswith("skill:"):
                parts = node_key.split(":")
                if len(parts) >= 3:
                    spec_map.setdefault(parts[1], []).append(value)
                    skill_nodes.add((parts[1], parts[2]))
                mapped = True
            elif node_key.startswith("passive:"):
                parts = node_key.split(":")
                if len(parts) >= 3:
                    passive_map.setdefault((parts[1], parts[2]), []).append(value)
                    passive_nodes.add((parts[1], parts[2]))
                mapped = True
            if instance_id:
                item_map.setdefault(instance_id, []).append(value)
                mapped = True
            if mapped:
                continue
            if candidate_id.startswith("cand:mod:"):
                instance = candidate_id[len("cand:mod:") :].split("#", 1)[0]
                item_map.setdefault(instance, []).append(value)
            elif candidate_id.startswith("cand:item:"):
                instance = candidate_id[len("cand:item:") :]
                item_map.setdefault(instance, []).append(value)
            elif candidate_id.startswith("cand:skill:"):
                parts = candidate_id.split(":")
                if len(parts) >= 4:
                    short, node = parts[2], parts[3]
                    spec_map.setdefault(short, []).append(value)
                    skill_nodes.add((short, node))
            elif candidate_id.startswith("cand:passive:"):
                parts = candidate_id.split(":")
                if len(parts) >= 4:
                    passive_map.setdefault((parts[2], parts[3]), []).append(value)
                    passive_nodes.add((parts[2], parts[3]))
            elif candidate_id.startswith("cand:contribution:"):
                node_key = _contribution_node_key(candidate_id[len("cand:contribution:") :])
                if node_key and node_key.startswith("skill:"):
                    parts = node_key.split(":")
                    if len(parts) >= 3:
                        skill_nodes.add((parts[1], parts[2]))
                elif node_key and node_key.startswith("passive:"):
                    parts = node_key.split(":")
                    if len(parts) >= 3:
                        passive_nodes.add((parts[1], parts[2]))
        return item_map, spec_map, passive_map, skill_nodes, passive_nodes

    def _source_instances(
        self, candidates: list[dict[str, Any]], records: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        item_map, spec_map, passive_map, _skill_nodes, _passive_nodes = self._candidate_scope_maps(candidates)
        instances: list[dict[str, Any]] = []
        for item in self.items:
            instance_id = _text(item.get("instance_id"))
            if not instance_id:
                continue
            granular = item_granular_category(item)
            mods = [mod for mod in self.mods if _text(mod.get("item_instance_id")) == instance_id]
            candidate_ids = item_map.get(instance_id, [])
            source_path = self.item_source_paths.get(instance_id)
            active = any(
                _is_active_contribution(contribution)
                for contribution in self.contributions
                if source_path and _text(getattr(contribution, "source_path", "")) == source_path
            )
            disposition = "candidate" if candidate_ids else ("baseline" if active else "excluded")
            instances.append(
                {
                    "instance_id": instance_id,
                    "instance_kind": "item",
                    "label": item_label(item),
                    "category": granular,
                    "page_category": item_page_category(item),
                    "mods": len(mods),
                    "points": None,
                    "disposition": disposition,
                    "reason": None if disposition != "excluded" else "未发现未计入的 Black Hole 相关条件效果。",
                    "candidate_effect_ids": sorted(set(candidate_ids)),
                }
            )
        for spec in self.specializations:
            tree = spec.get("tree") or {}
            tree_entity_id = _text(tree.get("entity_id"))
            tree_short = tree_entity_id.rsplit(":", 1)[-1] if tree_entity_id else ""
            tree_name = _text(tree.get("name"))
            category = "main_skill" if "black hole" in tree_name.casefold() else "other_skill"
            candidate_ids = spec_map.get(tree_short, [])
            prefix = f"skill:{tree_short}:"
            active = any(
                str(getattr(contribution, "contribution_id", "")).startswith(prefix)
                and _is_active_contribution(contribution)
                for contribution in self.contributions
            )
            disposition = "candidate" if candidate_ids else ("baseline" if active else "excluded")
            instances.append(
                {
                    "instance_id": f"skill_tree:{tree_short}",
                    "instance_kind": "specialization",
                    "label": f"{tree_name} 专精 · {_as_int(spec.get('points_total')) or 0} 点",
                    "category": category,
                    "page_category": category,
                    "mods": len(spec.get("nodes") or []),
                    "points": _as_int(spec.get("points_total")),
                    "disposition": disposition,
                    "reason": None if disposition != "excluded" else "该技能树效果均在范围外或已检查无输出关联。",
                    "candidate_effect_ids": sorted(set(candidate_ids)),
                }
            )
        passive_tree = self.passive_tree.get("tree") if isinstance(self.passive_tree, dict) else {}
        tree_short = _text((passive_tree or {}).get("entity_id")).rsplit(":", 1)[-1] or "mg-1"
        for node in self.passive_nodes:
            node_id = _text(node.get("node_id"))
            candidate_ids = passive_map.get((tree_short, node_id), [])
            prefix = f"passive:{tree_short}:{node_id}:"
            active = any(
                str(getattr(contribution, "contribution_id", "")).startswith(prefix)
                and _is_active_contribution(contribution)
                for contribution in self.contributions
            )
            disposition = "candidate" if candidate_ids else ("baseline" if active else "excluded")
            instances.append(
                {
                    "instance_id": f"passive:{tree_short}:{node_id}",
                    "instance_kind": "passive_node",
                    "label": f"职业被动 · {_text(node.get('name'))} · {_as_int(node.get('points')) or 0} 点",
                    "category": "passive",
                    "page_category": "passive",
                    "mods": 0,
                    "points": _as_int(node.get("points")),
                    "disposition": disposition,
                    "reason": None if disposition != "excluded" else "未发现未计入的 Black Hole 相关条件效果。",
                    "candidate_effect_ids": sorted(set(candidate_ids)),
                }
            )
        instances.sort(key=lambda entry: (entry["instance_kind"], entry["instance_id"]))
        return instances

    def _scope_categories(
        self, instances: list[dict[str, Any]], candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        item_map, spec_map, passive_map, skill_nodes, passive_nodes = self._candidate_scope_maps(
            candidates
        )
        item_instances = {
            entry["instance_id"]: entry
            for entry in instances
            if entry.get("instance_kind") == "item"
        }
        categories: list[dict[str, Any]] = []
        for category_id, label in CHECKED_SUMMARY_CATEGORIES:
            if category_id in ("equipment", "idol", "blessing"):
                selected_items = [
                    item for item in self.items if _checked_item_category(item) == category_id
                ]
                examined = len(selected_items)
                mods = sum(len(item.get("mods") or []) for item in selected_items)
                dispositions = {"candidate": 0, "baseline": 0, "excluded": 0}
                for item in selected_items:
                    entry = item_instances.get(_text(item.get("instance_id")))
                    disposition = (entry or {}).get("disposition") or "excluded"
                    if disposition not in dispositions:
                        disposition = "excluded"
                    dispositions[disposition] += 1
                candidate_count = dispositions["candidate"]
                baseline_count = dispositions["baseline"]
            elif category_id == "passive_node":
                examined = len(self.passive_nodes)
                mods = 0
                passive_tree = self.passive_tree.get("tree") if isinstance(self.passive_tree, dict) else {}
                tree_short = _text((passive_tree or {}).get("entity_id")).rsplit(":", 1)[-1] or "mg-1"
                candidate_count = sum(
                    1
                    for node in self.passive_nodes
                    if (tree_short, _text(node.get("node_id"))) in passive_nodes
                )
                baseline_count = 0
                for node in self.passive_nodes:
                    node_id = _text(node.get("node_id"))
                    if (tree_short, node_id) in passive_nodes:
                        continue
                    prefix = f"passive:{tree_short}:{node_id}:"
                    if any(
                        str(getattr(contribution, "contribution_id", "")).startswith(prefix)
                        and _is_active_contribution(contribution)
                        for contribution in self.contributions
                    ):
                        baseline_count += 1
            else:  # skill_node
                examined = sum(
                    len(spec.get("nodes") or [])
                    for spec in self.specializations
                    if isinstance(spec.get("nodes"), list)
                )
                mods = 0
                candidate_count = 0
                baseline_count = 0
                for spec in self.specializations:
                    tree = spec.get("tree") or {}
                    tree_short = _text(tree.get("entity_id")).rsplit(":", 1)[-1]
                    for node in spec.get("nodes") or []:
                        if not isinstance(node, dict):
                            continue
                        node_id = _text(node.get("node_id"))
                        if (tree_short, node_id) in skill_nodes:
                            candidate_count += 1
                            continue
                        prefix = f"skill:{tree_short}:{node_id}:"
                        if any(
                            str(getattr(contribution, "contribution_id", "")).startswith(prefix)
                            and _is_active_contribution(contribution)
                            for contribution in self.contributions
                        ):
                            baseline_count += 1
            excluded_count = max(0, examined - candidate_count - baseline_count)
            categories.append(
                {
                    "id": category_id,
                    "label": label,
                    "examined": examined,
                    "mods": mods,
                    "candidates": candidate_count,
                    "baseline": baseline_count,
                    "excluded": excluded_count,
                }
            )
        return categories

    # -- auto states -------------------------------------------------------
    def _dps_bool(self, key: str) -> Optional[bool]:
        scenario = self.dps_scenario
        target = scenario.get("target") if isinstance(scenario.get("target"), dict) else {}
        coverage = scenario.get("coverage") if isinstance(scenario.get("coverage"), dict) else {}
        if key == "boss_tag":
            value = target.get("boss_tag")
            return bool(value) if isinstance(value, bool) else None
        if key == "center_condition_met":
            value = coverage.get("center_condition_met")
            if isinstance(value, bool):
                return value
            conditions = scenario.get("target_conditions")
            if isinstance(conditions, dict) and isinstance(conditions.get("target_within_center_1m"), bool):
                return bool(conditions["target_within_center_1m"])
            return None
        if key == "both_stars_cover_target":
            value = coverage.get("both_stars_cover_target")
            return bool(value) if isinstance(value, bool) else None
        return None

    def _snapshot_lookup(self, key: str) -> Any:
        virtual = {
            "target.boss_tag": lambda: self._dps_bool("boss_tag"),
            "dps.target.boss_tag": lambda: self._dps_bool("boss_tag"),
            "target_is_boss_or_rare": lambda: self._dps_bool("boss_tag"),
            "target_conditions.target_is_boss_or_rare": lambda: self._dps_bool("boss_tag"),
            "scenario.target_conditions.target_is_boss_or_rare": lambda: self._dps_bool("boss_tag"),
            "target.within_center_1m": lambda: self._dps_bool("center_condition_met"),
            "target_within_center_1m": lambda: self._dps_bool("center_condition_met"),
            "target_conditions.target_within_center_1m": lambda: self._dps_bool("center_condition_met"),
            "scenario.target_conditions.target_within_center_1m": lambda: self._dps_bool("center_condition_met"),
            "coverage.center_condition_met": lambda: self._dps_bool("center_condition_met"),
            "coverage.both_stars_cover_target": lambda: self._dps_bool("both_stars_cover_target"),
            "target_conditions.both_stars_cover": lambda: self._dps_bool("both_stars_cover_target"),
            "both_stars_cover_target": lambda: self._dps_bool("both_stars_cover_target"),
        }
        if key in virtual:
            return virtual[key]()
        cleaned = key if isinstance(key, str) else str(key)
        if cleaned.startswith("snapshot."):
            cleaned = cleaned[len("snapshot.") :]
        roots: dict[str, Any] = {
            "derived": dict(getattr(self.snapshot, "derived", {}) or {}),
            "attributes": dict(getattr(self.snapshot, "attributes", {}) or {}),
            "resistances": dict(getattr(self.snapshot, "resistances", {}) or {}),
            "character": dict(getattr(self.snapshot, "character", {}) or {}),
            "build_meta": self.build_meta,
            "level": getattr(self.snapshot, "character", {}).get("level") if hasattr(self.snapshot, "character") else None,
        }
        parts = cleaned.split(".")
        if parts and parts[0] in roots:
            current = roots[parts[0]]
            for part in parts[1:]:
                if isinstance(current, dict) and part in current:
                    current = current.get(part)
                else:
                    return None
            return current
        for root in ("derived", "attributes", "character", "build_meta"):
            current = roots.get(root)
            if isinstance(current, dict) and cleaned in current:
                return current.get(cleaned)
        return None

    def _derive_numeric_auto(self, state: dict[str, Any]) -> tuple[Any, Optional[str]]:
        state_id = _text(state.get("id"))
        candidates: list[str] = []
        derive = state.get("derive")
        if isinstance(derive, dict):
            for key in ("key", "ref", "derived_from", "source_key", "path"):
                value = derive.get(key)
                if isinstance(value, str) and value:
                    candidates.append(value)
        for key in ("derived_from", "source_key", "ref"):
            value = state.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)
        candidates.extend(C.AUTO_STATE_ALIASES.get(state_id, ()))
        for key in candidates:
            value = self._snapshot_lookup(key)
            if value is not None:
                return value, key
        return None, None

    @staticmethod
    def _normalise_comparison(raw: Any, *, fallback_state_id: str = "") -> Optional[dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        ref = _text(raw.get("ref") or raw.get("source") or raw.get("base"))
        op = _text(raw.get("op") or ">=")
        threshold = raw.get("threshold", raw.get("value"))
        state_id = _text(raw.get("state") or raw.get("state_id")) or fallback_state_id
        label = _text(raw.get("label"))
        known_ops = set(C.COMPARISON_OPS) | set(C.COMPARISON_OPS.values())
        if not state_id or op not in known_ops:
            return None
        if threshold is None and op not in ("==", "!=", "eq", "ne"):
            return None
        return {
            "state": state_id,
            "label": label,
            "op": op,
            "ref": ref,
            "threshold": threshold,
            "raw": raw,
        }

    def _derive_auto_comparisons(self) -> list[dict[str, Any]]:
        """Return comparisons for auto bool states (also fills ``auto_values``)."""
        comparisons: list[dict[str, Any]] = []
        numeric_entries = {
            entry["id"]: entry
            for entry in self.auto_states
            if entry.get("kind") == "number" and entry.get("value") is not None
        }
        for state in self.registry.auto_states():
            if state.get("kind") != "bool":
                continue
            state_id = _text(state.get("id"))
            if not state_id:
                continue
            # Direct scenario/snapshot derivation (e.g. target.boss_or_rare):
            derive = state.get("derive")
            if isinstance(derive, dict) and not derive.get("op") and derive.get("key"):
                key = _text(derive.get("key"))
                direct = self._snapshot_lookup(key)
                if direct is not None:
                    value = bool(direct)
                    self.auto_values[state_id] = value
                    self.auto_states.append(
                        {
                            "id": state_id,
                            "label": _text(state.get("label")) or state_id,
                            "kind": "bool",
                            "source": "auto",
                            "value": value,
                            "display": "是" if value else "否",
                            "comparisons": [],
                            "note": f"由 {key} 派生。",
                            "derived_from": key,
                        }
                    )
                else:
                    self._add_gap(
                        f"gap:auto-state:{state_id}",
                        severity="warning",
                        area="自动状态",
                        message=f"自动状态 {state_id} 的派生键 {key!r} 无法从当前输入读取。",
                        probe="核对 snapshot/ruleset 是否提供该派生字段。",
                    )
                continue
            raw_candidates: list[dict[str, Any]] = []
            explicit = state.get("comparisons")
            if isinstance(explicit, list):
                for item in explicit:
                    normalised = self._normalise_comparison(item)
                    if normalised:
                        raw_candidates.append(normalised)
            for key in ("comparison", "compare", "derive"):
                normalised = self._normalise_comparison(
                    state.get(key), fallback_state_id=state_id
                )
                if normalised:
                    raw_candidates.append(normalised)
            if not raw_candidates and any(
                key in state for key in ("ref", "op", "threshold")
            ):
                normalised = self._normalise_comparison(state, fallback_state_id=state_id)
                if normalised:
                    raw_candidates.append(normalised)
            if not raw_candidates:
                pattern = re.match(
                    r"^(?P<base>.+?)[._](?P<op>gte|gt|lte|lt|eq|ne|ge|le)[._](?P<num>-?\d+(?:\.\d+)?)$",
                    state_id,
                )
                if pattern:
                    op_name = pattern.group("op")
                    op_map = {"gte": ">=", "gt": ">", "lte": "<=", "lt": "<", "eq": "==", "ne": "!=", "ge": ">=", "le": "<="}
                    ref = pattern.group("base")
                    if ref not in numeric_entries:
                        synthetic_value, source_key = self._derive_numeric_auto({"id": ref})
                        if synthetic_value is None:
                            # try aliases directly
                            for alias in C.AUTO_STATE_ALIASES.get(ref, ()):
                                synthetic_value = self._snapshot_lookup(alias)
                                if synthetic_value is not None:
                                    source_key = alias
                                    break
                        if synthetic_value is not None:
                            entry = {
                                "id": ref,
                                "label": _text(state.get("label")) or ref,
                                "kind": "number",
                                "source": "auto",
                                "value": synthetic_value,
                                "display": C.fmt_number(synthetic_value),
                                "comparisons": [],
                                "note": f"由 {source_key} 派生。",
                                "derived_from": source_key,
                                "virtual": True,
                            }
                            self.auto_states.append(entry)
                            numeric_entries[ref] = entry
                    if ref in numeric_entries:
                        number = float(pattern.group("num"))
                        threshold = int(number) if float(number).is_integer() else number
                        raw_candidates.append(
                            {
                                "state": state_id,
                                "label": _text(state.get("label")),
                                "op": op_map[op_name],
                                "ref": ref,
                                "threshold": threshold,
                            }
                        )
            for candidate in raw_candidates:
                ref = candidate.get("ref") or ""
                parent = numeric_entries.get(ref)
                value = parent.get("value") if parent else None
                threshold = candidate.get("threshold")
                met = C.compare_value(candidate.get("op") or ">=", value, threshold)
                comparison = {
                    "state": candidate.get("state") or state_id,
                    "label": candidate.get("label") or _text(state.get("label")),
                    "op": C.COMPARISON_OPS.get(str(candidate.get("op")).lower(), str(candidate.get("op"))),
                    "ref": ref,
                    "threshold": threshold,
                    "met": met,
                }
                comparisons.append(comparison)
                self.auto_values[comparison["state"]] = met
                if parent is not None:
                    parent.setdefault("comparisons", []).append(comparison)
                else:
                    self._add_gap(
                        f"gap:auto-state:{state_id}",
                        severity="warning",
                        area="自动状态",
                        message=f"自动门槛状态 {state_id} 的参照值 {ref!r} 无法从当前输入派生。",
                        probe="在 registry shared_states 中补充 derive/ref 或核对快照派生属性。",
                    )
        return comparisons

    def _derive_auto_states(self) -> None:
        for state in self.registry.auto_states():
            if state.get("kind") == "bool":
                continue
            state_id = _text(state.get("id"))
            if not state_id:
                continue
            value, source_key = self._derive_numeric_auto(state)
            entry = {
                "id": state_id,
                "label": _text(state.get("label")) or state_id,
                "kind": "number",
                "source": "auto",
                "value": value,
                "display": C.fmt_number(value) if value is not None else "—",
                "comparisons": [],
                "note": "",
                "derived_from": source_key,
            }
            explicit = state.get("comparisons")
            if isinstance(explicit, list):
                for index, item in enumerate(explicit):
                    normalised = self._normalise_comparison(item)
                    if normalised is None:
                        continue
                    comparison_state = _text(normalised.get("state"))
                    if not comparison_state:
                        op_token = re.sub(r"[^a-z0-9]+", "_", str(normalised.get("op") or "gte").lower())
                        threshold_token = _text(normalised.get("threshold")).replace(".", "_")
                        comparison_state = f"{state_id}_{op_token}_{threshold_token or index}"
                    met = C.compare_value(
                        normalised.get("op") or ">=", value, normalised.get("threshold")
                    )
                    comparison = {
                        "state": comparison_state,
                        "label": normalised.get("label") or "",
                        "op": C.COMPARISON_OPS.get(str(normalised.get("op")).lower(), str(normalised.get("op"))),
                        "ref": state_id,
                        "threshold": normalised.get("threshold"),
                        "met": met,
                    }
                    entry["comparisons"].append(comparison)
                    self.auto_values[comparison["state"]] = met
            if value is None:
                entry["note"] = "当前输入缺少该派生属性，无法判定。"
                self._add_gap(
                    f"gap:auto-state:{state_id}",
                    severity="warning",
                    area="自动状态",
                    message=f"自动状态 {state_id}（{entry['label']}）无法从当前输入派生。",
                    probe="在 registry shared_states 中补充 derive/ref，或确认快照派生属性。",
                )
            else:
                self.auto_values[state_id] = value
                entry["note"] = f"由当前输入派生：{source_key}。" if source_key else ""
            self.auto_states.append(entry)
        self._derive_auto_comparisons()
        # baseline fixed states
        for state_id, label in C.BASELINE_STATE_LABELS.items():
            if state_id == "baseline.target_boss":
                value = self._dps_bool("boss_tag")
                note = "生成基线固定为稀有/首领目标；本页不修改这个假设。"
            elif state_id == "baseline.centered":
                value = self._dps_bool("center_condition_met")
                note = "主技能节点固定假设（中心命中）；本页不修改。"
            else:
                value = self._dps_bool("both_stars_cover_target")
                note = "双星同时覆盖目标是生成基线的理论最优假设；本页不修改。"
            if value is None:
                continue
            self.baseline_states.append(
                {
                    "id": state_id,
                    "label": label,
                    "kind": "bool",
                    "source": "baseline",
                    "value": value,
                    "note": note,
                }
            )
            self.auto_values[state_id] = value

    # -- build (bd.json) cross-check --------------------------------------
    def _cross_check_build(self) -> None:
        if not self.build:
            return
        if not self.calc_input:
            return
        # item counts
        build_counts = self.build.get("counts") if isinstance(self.build.get("counts"), dict) else {}
        equipment = self.build.get("equipment") if isinstance(self.build.get("equipment"), dict) else {}
        idols = self.build.get("idols") if isinstance(self.build.get("idols"), list) else []
        blessings = self.build.get("blessings") if isinstance(self.build.get("blessings"), dict) else {}
        expected = {
            "equipment": _as_int(build_counts.get("equipment")) if build_counts else None,
            "idols": _as_int(build_counts.get("idols")) if build_counts else None,
            "blessings": _as_int(build_counts.get("blessings")) if build_counts else None,
        }
        actual = {
            "equipment": len(equipment) if equipment else sum(1 for item in self.items if item_granular_category(item) == "equipment"),
            "idols": len(idols) if idols else sum(1 for item in self.items if item_granular_category(item) == "idol"),
            "blessings": len(blessings) if blessings else sum(1 for item in self.items if item_granular_category(item) == "blessing"),
        }
        for key, expected_value in expected.items():
            if expected_value is not None and expected_value != actual[key]:
                self._add_gap(
                    f"gap:build-cross-check:{key}",
                    severity="warning",
                    area="交叉核对",
                    message=f"bd.json 的 {key} 数量 {expected_value} 与 calc input 的 {actual[key]} 不一致。",
                    probe="核对 calc input 是否由同一 revision 生成。",
                )
        # specialization points
        build_specs = self.build.get("specializations") if isinstance(self.build.get("specializations"), list) else []
        build_spec_by_name = {
            _text((spec.get("tree") or {}).get("name") if isinstance(spec.get("tree"), dict) else spec.get("name")): spec
            for spec in build_specs
            if isinstance(spec, dict)
        }
        build_spec_by_short = {
            _text(spec.get("tree_id")): spec for spec in build_specs if isinstance(spec, dict) and spec.get("tree_id")
        }
        for spec in self.specializations:
            tree = spec.get("tree") or {}
            name = _text(tree.get("name"))
            short = _text(tree.get("entity_id")).rsplit(":", 1)[-1]
            build_spec = build_spec_by_name.get(name) or build_spec_by_short.get(short)
            if not isinstance(build_spec, dict):
                self._add_gap(
                    f"gap:build-cross-check:spec:{short}",
                    severity="info",
                    area="交叉核对",
                    message=f"bd.json 中找不到技能专精 {name}（{short}）的对应记录。",
                    probe="核对 bd.json 与 calc input 是否同一构筑。",
                )
                continue
            build_nodes = {
                _text(node.get("node_id")): _as_int(node.get("points"))
                for node in (build_spec.get("nodes") or [])
                if isinstance(node, dict)
            }
            for node in spec.get("nodes") or []:
                if not isinstance(node, dict):
                    continue
                node_id = _text(node.get("node_id"))
                points = _as_int(node.get("points"))
                build_points = build_nodes.get(node_id)
                if build_points is not None and points is not None and build_points != points:
                    self._add_gap(
                        f"gap:build-cross-check:points:{short}:{node_id}",
                        severity="warning",
                        area="交叉核对",
                        message=f"{name} 节点 {node_id} 点数：bd.json={build_points}，calc input={points}。",
                        probe="重新生成 calc input 并确认 revision 一致。",
                    )
        # passive tree points
        build_passive = self.build.get("passive_tree") if isinstance(self.build.get("passive_tree"), dict) else {}
        build_passive_nodes = {
            _text(node.get("node_id")): _as_int(node.get("points"))
            for node in (build_passive.get("nodes") or [])
            if isinstance(node, dict)
        }
        for node in self.passive_nodes:
            node_id = _text(node.get("node_id"))
            points = _as_int(node.get("points"))
            build_points = build_passive_nodes.get(node_id)
            if build_points is not None and points is not None and build_points != points:
                self._add_gap(
                    f"gap:build-cross-check:passive:{node_id}",
                    severity="warning",
                    area="交叉核对",
                    message=f"职业被动节点 {node_id} 点数：bd.json={build_points}，calc input={points}。",
                    probe="重新生成 calc input 并确认 revision 一致。",
                )

    # -- denominator cross-check ------------------------------------------
    def _check_expectations(self, counts: dict[str, Any]) -> list[dict[str, Any]]:
        mismatches: list[dict[str, Any]] = []
        expectations = self.registry.checked_scope_expectations
        mapping = {
            "equipment_items": "equipment_items",
            "idols": "idols",
            "blessings": "blessings",
            "idol_altar": "idol_altar",
            "passive_nodes": "passive_nodes",
            "specializations": "specializations",
            "snapshot_contributions": "snapshot_contributions",
            "excluded_sources": "excluded_sources",
        }
        for key, expected in expectations.items():
            if not isinstance(expected, int):
                continue
            actual_key = mapping.get(key)
            if actual_key is None:
                continue
            actual = counts.get(actual_key)
            if actual is None or actual == expected:
                continue
            mismatches.append(
                {
                    "key": key,
                    "expected": expected,
                    "actual": actual,
                    "message": f"registry checked_scope_expectations.{key}={expected}，实际枚举为 {actual}。",
                }
            )
            self._add_gap(
                f"gap:scope-expectation:{key}",
                severity="warning",
                area="检查分母",
                message=mismatches[-1]["message"],
                probe="核对构筑 revision / 数据版本，registry 的期望分母可能来自另一版构筑。",
            )
        return mismatches

    # -- main --------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        self._parse_inputs()
        # registry effects
        for effect in sorted(self.registry.effects, key=effect_sort_key):
            if isinstance(effect, dict):
                self._resolve_effect(effect)
        checked_candidates = self._registry_candidate_records()
        self._heuristic_candidates()
        candidates = sorted(
            self.registered_candidates + checked_candidates + self.heuristic_candidates,
            key=lambda candidate: str(candidate.get("candidate_id") or ""),
        )
        seen_candidate_ids: set[str] = set()
        for candidate in candidates:
            candidate_id = str(candidate.get("candidate_id") or "")
            if candidate_id in seen_candidate_ids:
                suffix = 2
                while f"{candidate_id}:{suffix}" in seen_candidate_ids:
                    suffix += 1
                candidate["candidate_id"] = f"{candidate_id}:{suffix}"
                candidate_id = candidate["candidate_id"]
            seen_candidate_ids.add(candidate_id)
        candidates.sort(key=lambda candidate: str(candidate.get("candidate_id") or ""))
        checked_records = self._merge_checked_records()
        counts = self._counts(candidates, checked_records)
        source_instances = self._source_instances(candidates, checked_records)
        categories = self._scope_categories(source_instances, candidates)
        self._derive_auto_states()
        self._cross_check_build()
        mismatches = self._check_expectations(counts)
        self._build_affects_map()
        data_gaps = [self.data_gaps[key] for key in sorted(self.data_gaps)]
        return {
            "schema": C.SCHEMA_CATALOG,
            "binding": self.binding,
            "inputs": self._inputs_block(),
            "checked_scope": {
                "counts": counts,
                "categories": categories,
                "source_instances": source_instances,
                "excluded_sources": self._excluded_sources_block(),
            },
            "original_source_coverage": self._original_source_coverage(),
            "effects": sorted(self.effects, key=lambda entry: effect_sort_key(entry.get("effect") or {})),
            "candidates": candidates,
            "checked_records": checked_records,
            "data_gaps": data_gaps,
            "auto_states": self.auto_states,
            "auto_values": self.auto_values,
            "baseline_states": self.baseline_states,
            "expectation_mismatches": mismatches,
            "affects": self.affected_by_state,
        }

    def _build_affects_map(self) -> None:
        mapping: dict[str, list[str]] = {}
        for effect in self.registry.effects:
            if not isinstance(effect, dict):
                continue
            name = _text(effect.get("name")) or _text(effect.get("effect_id"))
            conditions = effect.get("conditions") or {}
            for group in ("all", "any"):
                for entry in conditions.get(group) or []:
                    if isinstance(entry, dict):
                        state_id = _text(entry.get("state"))
                        if state_id:
                            mapping.setdefault(state_id, [])
                            if name not in mapping[state_id]:
                                mapping[state_id].append(name)
            control = effect.get("control") or {}
            for key in ("state_id", "stack_state_id"):
                state_id = _text(control.get(key))
                if state_id:
                    mapping.setdefault(state_id, [])
                    if name not in mapping[state_id]:
                        mapping[state_id].append(name)
        for state_id in mapping:
            mapping[state_id].sort()
        self.affected_by_state = mapping

    def _excluded_sources_block(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for source in self.excluded_sources:
            if not isinstance(source, dict):
                continue
            out.append(
                {
                    "id": _text(source.get("source_id")),
                    "label": _text(source.get("label")),
                    "reason": _text(source.get("reason")),
                    "category": _text(source.get("category")),
                    "points_total": source.get("points_total"),
                    "nodes": [
                        {
                            "node_id": node.get("node_id"),
                            "name": _text(node.get("name")),
                            "points": node.get("points"),
                        }
                        for node in (source.get("nodes") or [])
                        if isinstance(node, dict)
                    ],
                }
            )
        out.sort(key=lambda entry: entry["id"])
        return out

    def _inputs_block(self) -> dict[str, Any]:
        if self.calc_input:
            calc = {
                "present": True,
                "schema_version": _text(self.calc_input.get("schema_version")),
                "build_id": _text(self.calc_input.get("build_id")),
                "revision_id": _text(self.calc_input.get("revision_id")),
                "content_sha256": _text(self.calc_input.get("content_sha256")),
                "dataset_id": _text(self.calc_input.get("dataset_id")),
                "game_version": _text(self.calc_input.get("game_version")),
                "items": len(self.items),
                "mods": len(self.mods),
                "specializations": len(self.specializations),
                "passive_nodes": len(self.passive_nodes),
            }
        else:
            calc = {"present": False}
        catalog_block: dict[str, Any] = {"available": self.db is not None}
        if self.db is not None:
            try:
                catalog_block["dataset_id"] = self.db.dataset_id()
            except Exception:  # noqa: BLE001 - optional information only
                catalog_block["dataset_id"] = None
        return {
            "calc_input": calc,
            "build": {"present": self.build is not None},
            "catalog": catalog_block,
        }

    def _original_source_coverage(self) -> dict[str, Any]:
        """R6: reconcile the raw build's own sources separately from checked counts.

        The existing denominator is an internal work ledger (items + mods + nodes +
        snapshot contribution rows).  This block answers a different question:
        which sources exist in the raw build (skill bar, specializations, passive
        tree, buffs, equipment) and whether each has an explicit check direction.
        Unspecialized skills are reported as explicit unknowns rather than being
        silently included in or excluded from the conditional denominator.
        """

        calc_skills = (self.calc_input or {}).get("skills") or {}
        action_bar = calc_skills.get("action_bar") if isinstance(calc_skills, dict) else None
        active_buffs = calc_skills.get("active_buffs") if isinstance(calc_skills, dict) else None
        bd_bar = (self.build or {}).get("ability_bar") if isinstance(self.build, dict) else None
        bd_by_id = {
            str(item.get("id")): item
            for item in (bd_bar or [])
            if isinstance(item, dict) and item.get("id") is not None
        }
        specializations = {
            str(spec.get("slot_number")): spec
            for spec in self.specializations
            if isinstance(spec, dict) and spec.get("slot_number") is not None
        }
        sources: list[dict[str, Any]] = []
        unknowns: list[dict[str, Any]] = []
        specialized_count = 0
        unspecialized_count = 0
        for entry in action_bar or []:
            if not isinstance(entry, dict):
                continue
            name = _text(entry.get("name")) or _text(entry.get("entity_id"))
            entity_id = _text(entry.get("entity_id"))
            spec_slot = entry.get("specialization_slot")
            specialized = spec_slot is not None
            status = "specialized" if specialized else "unspecialized"
            if specialized:
                specialized_count += 1
            else:
                unspecialized_count += 1
            source = {
                "kind": "skill_bar",
                "slot": entry.get("slot"),
                "name": name,
                "entity_id": entity_id,
                "specialized": specialized,
                "resolution_status": _text(entry.get("status")),
                "check_status": "covered_by_specialization" if specialized else "not_enumerated",
                "reason": (
                    "技能栏技能已在专精树/节点与当前相关效果检查中核对。"
                    if specialized
                    else "未专精的技能栏技能：其基础能力文本与主技能间接影响尚未逐项扫描；"
                    "本次只记为显式未知，不据此断言有/无 DPS 影响。"
                ),
            }
            bd_entry = bd_by_id.get(entity_id.rsplit(":", 1)[-1]) if entity_id else None
            if bd_entry is not None:
                source["build_says_specialized"] = bool(bd_entry.get("specialized"))
            sources.append(source)
            if not specialized:
                unknowns.append(
                    {
                        "kind": "skill_bar_unspecialized",
                        "name": name,
                        "entity_id": entity_id,
                        "reason": source["reason"],
                    }
                )
        # specializations not present in the action bar (defensive; should not happen)
        for slot, spec in specializations.items():
            owner = _text((spec.get("owner_skill") or {}).get("name")) or _text((spec.get("tree") or {}).get("name"))
            if not any(item.get("name") == owner for item in sources):
                sources.append(
                    {
                        "kind": "specialization",
                        "slot": slot,
                        "name": owner,
                        "entity_id": _text((spec.get("owner_skill") or {}).get("entity_id")),
                        "specialized": True,
                        "check_status": "covered_by_specialization",
                        "reason": "专精树已在技能节点检查范围内。",
                    }
                )
        buff_entries = []
        if isinstance(active_buffs, dict):
            for key in ("skills", "buffs", "ailments"):
                values = active_buffs.get(key)
                if isinstance(values, list):
                    buff_entries.extend(values)
        if not buff_entries:
            unknowns.append(
                {
                    "kind": "active_buffs_empty",
                    "reason": "存档激活配置未列出常驻 Buff；条件效果按场景声明处理，不假定默认激活。",
                }
            )
        counts = {
            "action_bar_skills": len([s for s in sources if s.get("kind") == "skill_bar"]),
            "specialized_skills": specialized_count,
            "unspecialized_skills": unspecialized_count,
            "specialization_trees": len(self.specializations),
            "active_buff_entries": len(buff_entries),
            "passive_tree_nodes": len(self.passive_nodes),
            "equipment_items": sum(
                1 for item in self.items if item_granular_category(item) in ("equipment", "idol_altar")
            ),
            "idols": sum(1 for item in self.items if item_granular_category(item) == "idol"),
            "explicit_unknowns": len(unknowns),
        }
        return {
            "schema": "bh-original-source-coverage-v1",
            "counts": counts,
            "sources": sources,
            "unknowns": unknowns,
            "notes": [
                "原始来源覆盖率与内部处理记录数是两套口径：前者回答原始构筑里有哪些来源、是否已有检查去向；"
                "后者是 items/mods/nodes/contributions 等工作量计数。",
                "未专精技能栏技能只记为显式未知，不纳入条件分母，也不据此声称漏算或已排除。",
            ],
        }

    def _counts(self, candidates: list[dict[str, Any]], checked_records: list[dict[str, Any]]) -> dict[str, Any]:
        equipment_items = sum(
            1 for item in self.items if item_granular_category(item) in ("equipment", "idol_altar")
        )
        idols = sum(1 for item in self.items if item_granular_category(item) == "idol")
        blessings = sum(1 for item in self.items if item_granular_category(item) == "blessing")
        idol_altar = sum(1 for item in self.items if item_granular_category(item) == "idol_altar")
        skill_nodes_allocated = sum(
            len(spec.get("nodes") or []) for spec in self.specializations if isinstance(spec.get("nodes"), list)
        )
        denominator_breakdown = {
            "items": len(self.items),
            "item_mods": len(self.mods),
            "specializations": len(self.specializations),
            "skill_nodes_allocated": skill_nodes_allocated,
            "passive_nodes": len(self.passive_nodes),
            "snapshot_contributions": len(self.contributions),
            "excluded_sources": len(self.excluded_sources),
        }
        counts = {
            "items": len(self.items),
            "equipment_items": equipment_items,
            "idols": idols,
            "blessings": blessings,
            "idol_altar": idol_altar,
            "item_mods": len(self.mods),
            "specializations": len(self.specializations),
            "skill_nodes_allocated": skill_nodes_allocated,
            "passive_nodes": len(self.passive_nodes),
            "snapshot_contributions": len(self.contributions),
            "excluded_sources": len(self.excluded_sources),
            "effect_instances": len(self.effects),
            "registered_effects": len(self.effects),
            "candidates": len(candidates),
            "registered_candidates": len(self.registered_candidates),
            "unregistered_candidates": len(candidates) - len(self.registered_candidates),
            "checked_records": len(checked_records),
            "excluded_records": sum(
                1 for record in checked_records if record.get("disposition") == "excluded"
            ),
            "baseline_records": sum(
                1 for record in checked_records if record.get("disposition") == "baseline_included"
            ),
            "denominator_breakdown": denominator_breakdown,
        }
        return counts


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def build_catalog(
    snapshot: Any,
    *,
    registry: Registry,
    calc_input: Optional[dict[str, Any]] = None,
    build: Optional[dict[str, Any]] = None,
    catalog: Any = None,
    dps_scenario: Optional[dict[str, Any]] = None,
    binding: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build the discovery catalog (contract §8.2).

    Parameters are intentionally tolerant: missing calc input / catalog / build
    data degrades into explicit ``data_gaps`` instead of raising.
    """
    discovery = _Discovery(
        snapshot,
        registry,
        calc_input=calc_input,
        build=build,
        catalog=catalog,
        dps_scenario=dps_scenario,
        binding=binding,
    )
    return discovery.run()


__all__ = [
    "CHECKED_SUMMARY_CATEGORIES",
    "build_catalog",
    "effect_page_category",
    "effect_summary",
    "effect_value_lines",
    "effect_sort_key",
    "item_granular_category",
    "item_label",
    "item_page_category",
    "scan_cues",
    "source_kind_page_category",
]
