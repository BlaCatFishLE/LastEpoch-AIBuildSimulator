"""来源绑定与受限规则求值（统一 DPS 第一步 · S3/S4）。

职责边界：

* **装备事实**来自 ``calculation-input-v1.items`` + 目录基底定义（不使用
  profile 的类型列表，也不从显示名猜）。
* **来源绑定**要求“规格身份”和“当前实例”同时成立：技能节点要 tree/node/
  points 与目录数值，装备/神像/祝福要定义 ID + 实际词条语义，外部固定场景
  必须显式声明为场景假设。
* **受限规则**只实现当前构筑需要的固定处理器（移速转伤、状态缩放、目标叠层、
  on-crit、每第 N 次命中、属性转换/缩放、异常施加、冷却节点）；没有通用表达式
  执行器，未知处理器形成明确缺口而不是静默跳过。

本模块不读文件系统（规则文件由调用方传入路径后显式加载），不做 DPS 数值装配；
数值装配见 ``dps_components``，流水编排见 ``dps``。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from . import model
from . import contribution_mapping as mapping
from .catalog import Catalog

# ---------------------------------------------------------------------------
# 绑定状态词表
# ---------------------------------------------------------------------------

BINDING_VERIFIED = "verified"
BINDING_SOURCE_ABSENT = "source_absent"
BINDING_IDENTITY_MISMATCH = "identity_mismatch"
BINDING_SEMANTICS_MISMATCH = "semantics_mismatch"
BINDING_CONDITION_NOT_SATISFIED = "condition_not_satisfied"
BINDING_UNVERIFIED = "unverified"
BINDING_UNRESOLVED = "unresolved"
BINDING_APPLIED = "applied"
BINDING_NOT_APPLICABLE = "not_applicable"
BINDING_DUPLICATE = "duplicate"

BINDING_STATUSES = (
    BINDING_VERIFIED, BINDING_SOURCE_ABSENT, BINDING_IDENTITY_MISMATCH,
    BINDING_SEMANTICS_MISMATCH, BINDING_CONDITION_NOT_SATISFIED,
    BINDING_UNVERIFIED, BINDING_UNRESOLVED, BINDING_APPLIED,
    BINDING_NOT_APPLICABLE, BINDING_DUPLICATE,
)

#: 绑定失败时是否仍允许数值生效：只有 condition_not_satisfied 是“合法但不生效”
_NO_VALUE_STATUSES = frozenset(
    {BINDING_SOURCE_ABSENT, BINDING_IDENTITY_MISMATCH, BINDING_SEMANTICS_MISMATCH, BINDING_UNRESOLVED}
)

#: 技能/装备/规则中会出现的武器基底类型名（用于双手判定）
_TWO_HANDED_MARKERS = ("two-handed", "two handed")


class BindingError(ValueError):
    """profile 或输入不满足已声明规则（非法必需输入拒绝计算）。"""


# ---------------------------------------------------------------------------
# 装备事实
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WeaponFact:
    instance_id: str
    slot: str
    display_name: str
    base_name: str
    item_type: str
    base_entity_id: Optional[str]
    definition_entity_id: Optional[str]
    weapon_type_id: Optional[int]
    two_handed: Optional[bool]
    extended: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "slot": self.slot,
            "display_name": self.display_name,
            "base_name": self.base_name,
            "item_type": self.item_type,
            "base_entity_id": self.base_entity_id,
            "definition_entity_id": self.definition_entity_id,
            "weapon_type_id": self.weapon_type_id,
            "two_handed": self.two_handed,
        }


@dataclass(frozen=True)
class EquipmentFacts:
    weapons: tuple[WeaponFact, ...]
    main_hand: Optional[WeaponFact]
    off_hand_weapon: Optional[WeaponFact]
    off_hand_item: Optional[dict[str, Any]]
    shield: Optional[dict[str, Any]]
    dual_wield: bool
    weapon_type_ids: frozenset[int]
    weapon_type_unknown: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "weapons": [weapon.to_dict() for weapon in self.weapons],
            "main_hand": self.main_hand.to_dict() if self.main_hand else None,
            "off_hand_weapon": self.off_hand_weapon.to_dict() if self.off_hand_weapon else None,
            "off_hand_item": self.off_hand_item,
            "shield": self.shield,
            "dual_wield": self.dual_wield,
            "weapon_type_ids": sorted(self.weapon_type_ids),
            "weapon_type_unknown": self.weapon_type_unknown,
        }


def _item_by_slot(calc: dict[str, Any], slot: str) -> Optional[dict[str, Any]]:
    for item in calc.get("items") or []:
        if str(item.get("slot") or "") == slot:
            return item
    return None


def _weapon_type_from_catalog(catalog: Optional[Catalog], item: dict[str, Any]) -> tuple[Optional[int], Optional[bool], Optional[str]]:
    """从基底定义读取 (weapon_type_id, two_handed, item_type_name)；缺失返回 None。"""
    base_entity_id = item.get("base_entity_id")
    base_row = catalog.entity(base_entity_id) if catalog is not None and base_entity_id else None
    extensions = (base_row or {}).get("extensions") or {}
    weapon_type_id: Optional[int] = None
    raw_base_type = extensions.get("base_type")
    if raw_base_type is not None:
        try:
            weapon_type_id = int(raw_base_type)
        except (TypeError, ValueError):
            weapon_type_id = None
    if weapon_type_id is None and base_entity_id:
        # 基底实体 id 形式：item:letools:itemList.equippable_<type>:<sub>
        token = str(base_entity_id).rsplit(":", 2)
        if len(token) == 3 and "_" in token[1]:
            try:
                weapon_type_id = int(token[1].rsplit("_", 1)[-1])
            except (TypeError, ValueError):
                weapon_type_id = None
    semantics = extensions.get("gear_semantics") or {}
    item_type_name = semantics.get("item_type_name") or item.get("item_type")
    two_handed: Optional[bool] = None
    if item_type_name:
        lowered = str(item_type_name).lower()
        two_handed = any(marker in lowered for marker in _TWO_HANDED_MARKERS)
    return weapon_type_id, two_handed, item_type_name


def equipment_facts(calc: dict[str, Any], catalog: Optional[Catalog] = None) -> EquipmentFacts:
    """从当前计算输入与目录解析武器/盾/副手事实；缺字段返回 unknown 而不是猜。"""
    weapons: list[WeaponFact] = []
    off_hand_weapon: Optional[WeaponFact] = None
    off_hand_item: Optional[dict[str, Any]] = None
    shield: Optional[dict[str, Any]] = None
    weapon_type_ids: set[int] = set()
    weapon_type_unknown = False
    for slot in ("weapon1", "weapon2"):
        item = _item_by_slot(calc, slot)
        if item is None:
            continue
        item_type = str(item.get("item_type") or "")
        weapon_type_id, two_handed, item_type_name = _weapon_type_from_catalog(catalog, item)
        is_weapon = "weapon" in item_type.lower() or "axe" in item_type.lower() or "sword" in item_type.lower() or "mace" in item_type.lower() or "spear" in item_type.lower() or "dagger" in item_type.lower() or "bow" in item_type.lower() or "wand" in item_type.lower() or "sceptre" in item_type.lower()
        if not is_weapon:
            if "shield" in item_type.lower():
                shield = item
            else:
                off_hand_item = item
            continue
        fact = WeaponFact(
            instance_id=str(item.get("instance_id") or slot),
            slot=slot,
            display_name=str(item.get("display_name") or ""),
            base_name=str(item.get("base_name") or ""),
            item_type=item_type_name or item_type,
            base_entity_id=item.get("base_entity_id"),
            definition_entity_id=item.get("definition_entity_id"),
            weapon_type_id=weapon_type_id,
            two_handed=two_handed,
        )
        weapons.append(fact)
        if slot == "weapon1":
            pass
        else:
            off_hand_weapon = fact
        if weapon_type_id is None:
            weapon_type_unknown = True
        else:
            weapon_type_ids.add(weapon_type_id)
    main_hand = next((weapon for weapon in weapons if weapon.slot == "weapon1"), None)
    if main_hand is None and weapons:
        main_hand = weapons[0]
    off_hand_weapon = next((weapon for weapon in weapons if weapon.slot == "weapon2"), None)
    dual_wield = bool(
        main_hand is not None
        and off_hand_weapon is not None
        and main_hand.two_handed is False
        and off_hand_weapon.two_handed is False
    )
    return EquipmentFacts(
        weapons=tuple(weapons),
        main_hand=main_hand,
        off_hand_weapon=off_hand_weapon,
        off_hand_item=off_hand_item,
        shield=shield,
        dual_wield=dual_wield,
        weapon_type_ids=frozenset(weapon_type_ids),
        weapon_type_unknown=weapon_type_unknown,
    )


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def evaluate_planner_condition(
    condition: dict[str, Any],
    facts: EquipmentFacts,
    *,
    mana_cost: Optional[float] = None,
) -> tuple[Optional[bool], Optional[str]]:
    """planner 被动/装备条件求值：返回 (结果, 事实代码)；未知条件返回 None。"""
    if not isinstance(condition, dict):
        return None, f"unsupported_condition:{condition!r}"
    code: Optional[str] = None
    if "weapon" in condition:
        wanted = condition["weapon"]
        wanted_ids = [wanted] if isinstance(wanted, (int, str)) else list(wanted)
        wanted_ids = [value for value in (_int_or_none(item) for item in wanted_ids) if value is not None]
        code = f"weapon:{wanted_ids}:equipped={bool(condition.get('equipped', True))}"
        if facts.weapon_type_unknown:
            return None, code
        matched = bool(set(wanted_ids) & set(facts.weapon_type_ids))
        return matched == bool(condition.get("equipped", True)), code
    if "dualWield" in condition:
        code = f"dual_wield={bool(condition['dualWield'])}"
        return facts.dual_wield == bool(condition["dualWield"]), code
    if "meleeAbilityManaCost" in condition:
        code = f"melee_ability_mana_cost>={condition['meleeAbilityManaCost']}"
        if mana_cost is None:
            return None, code
        try:
            threshold = float(condition["meleeAbilityManaCost"])
        except (TypeError, ValueError):
            return None, code
        return mana_cost >= threshold, code
    return None, f"unsupported_condition:{sorted(condition)}"


def passive_condition_evaluator(
    facts: EquipmentFacts,
    *,
    mana_cost: Optional[float] = None,
) -> Callable[[dict[str, Any]], tuple[Optional[bool], Optional[str]]]:
    return lambda condition: evaluate_planner_condition(condition, facts, mana_cost=mana_cost)


# ---------------------------------------------------------------------------
# 来源绑定校验
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BindingCheck:
    status: str
    detail: str
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == BINDING_VERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "detail": self.detail, "facts": dict(self.facts)}


def _tag_mask(names: Iterable[str]) -> int:
    mask = 0
    for name in names:
        bit = model.AT_BITS.get(name)
        if bit:
            mask |= bit
    return mask


def verify_mod(
    calc: dict[str, Any],
    *,
    mod_id: str,
    item_instance_id: Optional[str] = None,
    expected_property: Any = None,
    expected_special_tag: Any = None,
    expected_tags: Optional[list[str]] = None,
    expected_definition_entity_id: Optional[str] = None,
    expected_base_entity_id: Optional[str] = None,
    expected_readable_text_contains: Optional[str] = None,
    expected_stat_name_contains: Optional[str] = None,
    require_identity: bool = True,
) -> BindingCheck:
    """校验“定义身份 + 当前实例词条语义”双重绑定。

    ``require_identity=True`` 时，必须声明 ``expected_definition_entity_id`` 或
    ``expected_base_entity_id``；只有 property/tags 等语义声明不足以视为已验证
    （同一 property 可出现在不同定义上）。
    """
    """校验“定义身份 + 当前实例词条语义”双重绑定。"""
    mods = {str(mod.get("mod_id")): mod for mod in (calc.get("mods") or [])}
    mod = mods.get(mod_id)
    if mod is None:
        return BindingCheck(BINDING_SOURCE_ABSENT, f"词条 {mod_id} 不在当前计算输入中")
    facts: dict[str, Any] = {"mod_id": mod_id}
    instance = str(mod.get("item_instance_id") or "")
    facts["item_instance_id"] = instance
    if item_instance_id is not None and instance != item_instance_id:
        return BindingCheck(
            BINDING_IDENTITY_MISMATCH,
            f"词条 {mod_id} 属于实例 {instance}，绑定声明为 {item_instance_id}",
            facts,
        )
    item = next(
        (entry for entry in (calc.get("items") or []) if str(entry.get("instance_id")) == instance),
        None,
    )
    if item is None:
        return BindingCheck(BINDING_SOURCE_ABSENT, f"实例 {instance} 不在当前计算输入中", facts)
    facts["definition_entity_id"] = item.get("definition_entity_id")
    facts["base_entity_id"] = item.get("base_entity_id")
    facts["item_type"] = item.get("item_type")
    if expected_definition_entity_id is not None and item.get("definition_entity_id") != expected_definition_entity_id:
        return BindingCheck(
            BINDING_IDENTITY_MISMATCH,
            f"实例 {instance} 的定义为 {item.get('definition_entity_id')}，绑定声明为 {expected_definition_entity_id}",
            facts,
        )
    if expected_base_entity_id is not None and item.get("base_entity_id") != expected_base_entity_id:
        return BindingCheck(
            BINDING_IDENTITY_MISMATCH,
            f"实例 {instance} 的基底为 {item.get('base_entity_id')}，绑定声明为 {expected_base_entity_id}",
            facts,
        )
    declared = False
    if expected_property is not None:
        declared = True
        if _int_or_none(mod.get("property")) != _int_or_none(expected_property):
            return BindingCheck(
                BINDING_SEMANTICS_MISMATCH,
                f"词条 {mod_id} 的 property={mod.get('property')!r}，绑定声明为 {expected_property!r}",
                facts,
            )
    if expected_special_tag is not None:
        declared = True
        if _int_or_none(mod.get("special_tag")) != _int_or_none(expected_special_tag):
            return BindingCheck(
                BINDING_SEMANTICS_MISMATCH,
                f"词条 {mod_id} 的 special_tag={mod.get('special_tag')!r}，绑定声明为 {expected_special_tag!r}",
                facts,
            )
    if expected_tags:
        declared = True
        actual_mask = _int_or_none(mod.get("tags")) or 0
        wanted_mask = _tag_mask(expected_tags)
        if actual_mask & wanted_mask != wanted_mask:
            return BindingCheck(
                BINDING_SEMANTICS_MISMATCH,
                f"词条 {mod_id} 的 tags={model.decode_tags(actual_mask)}，绑定要求包含 {sorted(expected_tags)}",
                facts,
            )
    if expected_readable_text_contains:
        declared = True
        text = str(mod.get("readable_text") or "")
        if expected_readable_text_contains.lower() not in text.lower():
            return BindingCheck(
                BINDING_SEMANTICS_MISMATCH,
                f"词条 {mod_id} 的说明文字未包含 {expected_readable_text_contains!r}",
                facts,
            )
    if expected_stat_name_contains:
        declared = True
        stat_name = str(((mod.get("stat") or {}).get("name")) or "")
        facts["stat_name"] = stat_name
        if expected_stat_name_contains.lower() not in stat_name.lower():
            return BindingCheck(
                BINDING_SEMANTICS_MISMATCH,
                f"词条 {mod_id} 的 stat 名称未包含 {expected_stat_name_contains!r}",
                facts,
            )
    facts["value_numeric"] = mod.get("value_numeric")
    facts["readable_text"] = mod.get("readable_text")
    facts["tags"] = model.decode_tags(_int_or_none(mod.get("tags")) or 0)
    identity_declared = (
        expected_definition_entity_id is not None or expected_base_entity_id is not None
    )
    facts["identity_declared"] = identity_declared
    facts["semantics_declared"] = declared
    if require_identity and not identity_declared:
        return BindingCheck(
            BINDING_UNVERIFIED,
            f"词条 {mod_id} 缺少稳定定义身份锚点（definition/base entity id），"
            "只有语义声明不足以视为已验证",
            facts,
        )
    if not declared:
        return BindingCheck(
            BINDING_UNVERIFIED,
            f"词条 {mod_id} 只校验了存在性/身份；缺少效果语义声明，不能视为已验证",
            facts,
        )
    return BindingCheck(BINDING_VERIFIED, f"词条 {mod_id} 身份与语义绑定通过", facts)


def verify_unique_description(
    calc: dict[str, Any],
    *,
    item_instance_id: str,
    description_index: int,
    expected_text_contains: str,
    expected_definition_entity_id: Optional[str] = None,
) -> BindingCheck:
    """校验 unique 说明行的实例身份与文本语义（说明行不是可计算词条）。

    说明行的稳定身份至少需要“实例 + 定义 ID”，语义需要非空文本匹配；
    空文本或只有实例都不足以判定为已验证。
    """
    prefix = f"{item_instance_id}#unique_description#{description_index}"
    if not str(expected_text_contains or "").strip():
        return BindingCheck(BINDING_UNVERIFIED, f"说明行 {prefix} 缺少非空文本语义锚点")
    if not expected_definition_entity_id:
        return BindingCheck(
            BINDING_UNVERIFIED, f"说明行 {prefix} 缺少稳定定义身份（definition entity id）"
        )
    mods = {str(mod.get("mod_id")): mod for mod in (calc.get("mods") or [])}
    mod = mods.get(prefix)
    if mod is None:
        return BindingCheck(BINDING_SOURCE_ABSENT, f"说明行 {prefix} 不在当前计算输入中")
    item = next(
        (entry for entry in (calc.get("items") or []) if str(entry.get("instance_id")) == item_instance_id),
        None,
    )
    if item is None:
        return BindingCheck(BINDING_SOURCE_ABSENT, f"实例 {item_instance_id} 不在当前计算输入中")
    text = str(mod.get("readable_text") or "")
    facts = {
        "mod_id": prefix,
        "item_instance_id": item_instance_id,
        "definition_entity_id": item.get("definition_entity_id"),
        "readable_text": text,
        "identity_declared": True,
    }
    if item.get("definition_entity_id") != expected_definition_entity_id:
        return BindingCheck(
            BINDING_IDENTITY_MISMATCH,
            f"实例 {item_instance_id} 的定义为 {item.get('definition_entity_id')}，"
            f"绑定声明为 {expected_definition_entity_id}",
            facts,
        )
    if expected_text_contains.lower() not in text.lower():
        return BindingCheck(
            BINDING_SEMANTICS_MISMATCH,
            f"说明行 {prefix} 的文字未包含 {expected_text_contains!r}",
            facts,
        )
    return BindingCheck(BINDING_VERIFIED, f"说明行 {prefix} 身份与文本语义绑定通过", facts)


# ---------------------------------------------------------------------------
# 技能节点绑定
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeBindingResult:
    rule_id: str
    tree_id: str
    node_id: str
    points: int
    status: str
    value: Optional[float]
    reason: Optional[str]
    contribution: Optional[model.Contribution]
    facts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "tree_id": self.tree_id,
            "node_id": self.node_id,
            "points": self.points,
            "status": self.status,
            "value": self.value,
            "reason": self.reason,
            "contribution_id": self.contribution.contribution_id if self.contribution else None,
            "facts": dict(self.facts),
        }


def _specialization_for(calc: dict[str, Any], tree_id: str) -> Optional[dict[str, Any]]:
    for spec in (calc.get("skills") or {}).get("specializations") or []:
        entity = str((spec.get("tree") or {}).get("entity_id") or "")
        if entity.endswith(":" + tree_id):
            return spec
    return None


def bind_node_value(
    calc: dict[str, Any],
    catalog: Catalog,
    *,
    tree_id: str,
    node_id: str,
    stat_index: int,
) -> tuple[Optional[float], int, BindingCheck]:
    """读取技能树节点当前点数与目录每点数值；返回 (value*points, points, check)。

    状态语义区分：
    * 未专精/未分配 → ``not_applicable``（构筑选择，合法缺席，不是覆盖缺口）；
    * 目录缺节点/缺数值 → ``unresolved``（数据缺口，进入覆盖缺口）。
    """
    spec = _specialization_for(calc, tree_id)
    if spec is None:
        return None, 0, BindingCheck(BINDING_NOT_APPLICABLE, f"技能树 {tree_id} 不在当前专精中（未选择）")
    selected = {str(node.get("node_id")): node for node in spec.get("nodes") or []}
    entry = selected.get(str(node_id))
    points = int((entry or {}).get("points") or 0)
    if entry is None or points <= 0:
        return None, points, BindingCheck(
            BINDING_NOT_APPLICABLE, f"技能树 {tree_id} 节点 {node_id} 当前未分配（points={points}）"
        )
    tree = mapping.tree_by_source_id(catalog, tree_id)
    nodes = mapping.nodes_by_index(catalog, tree["entity_id"])
    node = nodes.get(str(node_id))
    if node is None:
        return None, points, BindingCheck(BINDING_UNRESOLVED, f"目录缺少 {tree_id} 节点 {node_id}")
    grouped = mapping.skill_node_effect_values(catalog, node["entity_id"])
    values = grouped.get(int(stat_index)) or []
    numeric = [_num(row.get("value")) for row in values if row.get("value") is not None]
    if not numeric:
        return None, points, BindingCheck(
            BINDING_UNRESOLVED,
            f"节点 {tree_id}:{node_id} 的 stat_index={stat_index} 没有目录数值",
        )
    return float(numeric[0]) * points, points, BindingCheck(
        BINDING_VERIFIED,
        f"节点 {tree_id}:{node_id} points={points}, stat_index={stat_index} 每点 {numeric[0]:g}",
        {"points": points, "per_point": numeric[0], "stat_index": int(stat_index)},
    )


# ---------------------------------------------------------------------------
# 属性转换（复用既有算子，不复制公式）
# ---------------------------------------------------------------------------


def load_json_document(root: Path, relative_path: str) -> dict[str, Any]:
    path = (root / relative_path).resolve()
    return json.loads(path.read_text(encoding="utf-8"))


def _expected_identity_error(expected: Any) -> Optional[str]:
    """校验“期望身份锚点”对象；返回错误说明，合法时返回 ``None``。

    属性转换在加载阶段与运行时都必须走这里：缺失、空对象、空字符串、未知字段
    或缺少稳定定义身份都视为无效锚点，不得返回 ``applied``。
    """
    if expected is None:
        return "缺少 expected 身份锚点"
    if not isinstance(expected, dict) or not expected:
        return "expected 必须是非空对象"
    allowed = {"item_instance_id", "definition_entity_id", "base_entity_id"}
    unknown = set(expected) - allowed
    if unknown:
        return f"expected 含未支持的字段：{sorted(unknown)}"
    for key in sorted(allowed):
        if key not in expected:
            continue
        value = expected.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"expected.{key} 必须是非空字符串"
    if not (
        str(expected.get("definition_entity_id") or "").strip()
        or str(expected.get("base_entity_id") or "").strip()
    ):
        return "expected 缺少稳定定义身份（definition_entity_id / base_entity_id）"
    return None


def bind_attribute_conversion(
    calc: dict[str, Any],
    catalog: Catalog,
    profile: dict[str, Any],
    *,
    attributes: dict[str, float],
    hit_component_tags: list[str],
    root: Path,
) -> dict[str, Any]:
    """执行属性转换规则（复用 ``mechanics.attribute_conversion.resolve_brutality``）。

    返回 ``{status, contribution, attributes_update, conversion_info, source_mod_ids, rule_sha256, evidence}``；
    未声明规则时返回 ``status=not_applicable`` 且不改动属性。

    运行时独立校验 ``expected`` 身份锚点：即使调用方跳过 ``validate_profile``，
    缺失/空对象/无有效身份字段也不会返回 ``applied``。
    """
    if "attribute_conversion" not in profile or profile.get("attribute_conversion") is None:
        return {"status": BINDING_NOT_APPLICABLE, "contribution": None, "conversion_info": None, "source_mod_ids": [], "evidence": []}
    declared = profile.get("attribute_conversion")
    if not isinstance(declared, dict) or not declared:
        return {
            "status": BINDING_UNVERIFIED,
            "contribution": None,
            "conversion_info": None,
            "source_mod_ids": [],
            "evidence": [],
            "reason": "attribute_conversion 已声明但为空对象，不能视为已验证",
        }
    identity_error = _expected_identity_error(declared.get("expected"))
    if identity_error:
        return {
            "status": BINDING_UNVERIFIED,
            "contribution": None,
            "conversion_info": None,
            "source_mod_ids": [],
            "evidence": [],
            "reason": f"attribute_conversion.expected 身份锚点无效：{identity_error}",
        }
    rules_path = declared.get("rules")
    if not isinstance(rules_path, str) or not rules_path:
        raise BindingError("attribute_conversion 缺少 rules 路径")
    rules = load_json_document(root, rules_path)
    rule = rules.get("brutality")
    if not isinstance(rule, dict) or not isinstance(rule.get("selector"), dict):
        raise BindingError("属性转换规则缺少 brutality.selector")
    from .mechanics.attribute_conversion import resolve_brutality

    selector = rule["selector"]
    matched = [mod for mod in calc.get("mods") or [] if all(mod.get(key) == value for key, value in selector.items())]
    evidence = [
        model.Evidence(
            kind="rule_data",
            detail="attribute conversion rule",
            locator=rules_path,
        )
    ]
    for entry in declared.get("evidence") or []:
        evidence.append(model.Evidence(kind=str(entry.get("kind") or "user_rule"), detail=str(entry.get("detail") or ""), locator=entry.get("locator")))
    if not matched:
        return {
            "status": BINDING_SOURCE_ABSENT,
            "contribution": None,
            "conversion_info": None,
            "source_mod_ids": [],
            "evidence": evidence,
            "reason": "当前输入没有命中属性转换 selector 的词条",
        }
    expected_identity = declared.get("expected") or {}
    if expected_identity:
        for mod in matched:
            instance = str(mod.get("item_instance_id") or "")
            item = next(
                (
                    entry
                    for entry in (calc.get("items") or [])
                    if str(entry.get("instance_id")) == instance
                ),
                None,
            )
            if item is None:
                return {
                    "status": BINDING_SOURCE_ABSENT,
                    "contribution": None,
                    "conversion_info": None,
                    "source_mod_ids": [],
                    "evidence": evidence,
                    "reason": f"属性转换词条实例 {instance} 不在当前输入中",
                }
            expected_instance = expected_identity.get("item_instance_id")
            expected_definition = expected_identity.get("definition_entity_id")
            expected_base = expected_identity.get("base_entity_id")
            mismatch = None
            if expected_instance and instance != expected_instance:
                mismatch = f"属性转换词条所属实例为 {instance}，绑定声明为 {expected_instance}"
            elif expected_definition and item.get("definition_entity_id") != expected_definition:
                mismatch = (
                    f"实例 {instance} 的定义为 {item.get('definition_entity_id')}，"
                    f"绑定声明为 {expected_definition}"
                )
            elif expected_base and item.get("base_entity_id") != expected_base:
                mismatch = (
                    f"实例 {instance} 的基底为 {item.get('base_entity_id')}，绑定声明为 {expected_base}"
                )
            if mismatch:
                return {
                    "status": BINDING_IDENTITY_MISMATCH,
                    "contribution": None,
                    "conversion_info": None,
                    "source_mod_ids": [],
                    "evidence": evidence,
                    "reason": mismatch,
                }
    skill_name = str(((profile.get("skill") or {}).get("dataset_skill_name")) or "")
    if not skill_name:
        spec = _specialization_for(calc, str(profile.get("ability_id") or ""))
        if spec is None:
            raise BindingError("无法解析主技能专精以读取基础法力消耗")
        skill_name = str((spec.get("owner_skill") or {}).get("name") or "")
    skill_fact = catalog.skill_by_name(skill_name)
    base_cost = float(skill_fact.get("mana_cost") or 0.0)
    flat_cost = 0.0
    inc_cost = 0.0
    more_cost = 1.0
    from .mechanics.tag_match import tags_match, TagPolicy
    from .mechanics.tag_match import UNKNOWN_RESTRICT

    policy = TagPolicy(ignored_tags=frozenset({"Elemental"}), known_tags=frozenset(), unknown_tag_policy=UNKNOWN_RESTRICT, name="dps-component-closure")
    for mod in calc.get("mods") or []:
        if _int_or_none(mod.get("property")) != mapping.PROP_MANA_COST:
            continue
        if _int_or_none(mod.get("special_tag")) not in (0, None):
            continue
        if not tags_match(model.decode_tags(_int_or_none(mod.get("tags")) or 0), hit_component_tags, policy):
            continue
        value = float(mod.get("value_numeric") or 0.0)
        modifier_type = _int_or_none(mod.get("modifier_type"))
        if modifier_type == 0:
            flat_cost += value
        elif modifier_type == 1:
            inc_cost += value
        elif modifier_type == 2:
            more_cost *= 1.0 + value
    cost = max(0.0, (base_cost + flat_cost) * (1.0 + inc_cost) * more_cost)
    conversion_fraction = sum(float(mod.get("value_numeric") or 0.0) for mod in matched)
    conversion = resolve_brutality(attributes.get("strength", 0.0), conversion_fraction, cost, rule)
    conversion.update(
        base_mana_cost=base_cost,
        flat_mana_cost=flat_cost,
        increased_mana_cost=inc_cost,
        more_mana_cost=more_cost,
        rule_sha256=_sha256_file(root, rules_path),
        source_mod_ids=[mod.get("mod_id") for mod in matched],
    )
    contribution = mapping.make_contribution(
        contribution_id=f"attribute_conversion:{matched[0].get('mod_id')}",
        source_kind="derived",
        source_label="Strength 转为 Brutality",
        source_path="attribute_conversion",
        stat_name="more damage",
        stat_key="damage",
        operation="more",
        value=float(conversion["more_damage"]),
        unit="percent",
        damage_tags=list(rule.get("damage_tags") or []),
        group="attribute:brutality",
        value_source="derived",
        status=model.STATUS_DERIVED,
        evidence_rows=evidence,
        notes="汇总属性后转换；技能、节点及力量门槛使用等效力量。",
        raw={"conversion": {key: conversion[key] for key in ("strength_before", "conversion_fraction", "strength", "brutality", "effective_strength", "mana_cost", "capped_mana_cost")}, "rule_sha256": conversion["rule_sha256"]},
    )
    return {
        "status": BINDING_APPLIED,
        "contribution": contribution,
        "conversion_info": conversion,
        "source_mod_ids": conversion["source_mod_ids"],
        "attributes_update": {
            "strength": conversion["strength"],
            "brutality": conversion["brutality"],
            "effective_strength": conversion["effective_strength"],
        },
        "scaling_strength": conversion["effective_strength"],
        "evidence": evidence,
        "rule_sha256": conversion["rule_sha256"],
        "matched_mod_ids": [mod.get("mod_id") for mod in matched],
    }


def _sha256_file(root: Path, relative_path: str) -> str:
    import hashlib

    return hashlib.sha256((root / relative_path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 效果 / 规则绑定
# ---------------------------------------------------------------------------


@dataclass
class EffectResult:
    effect_id: str
    label: str
    status: str
    reason: Optional[str]
    contribution: Optional[model.Contribution]
    source: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    evidence: list[model.Evidence] = field(default_factory=list)
    check: Optional[BindingCheck] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_id": self.effect_id,
            "label": self.label,
            "status": self.status,
            "reason": self.reason,
            "contribution_id": self.contribution.contribution_id if self.contribution else None,
            "source": dict(self.source),
            "payload": dict(self.payload),
            "check": self.check.to_dict() if self.check else None,
        }


STATE_SOURCE = "state_source"
SKILL_NODE = "skill_node"
EQUIPMENT_MOD = "equipment_mod"
UNIQUE_DESCRIPTION = "unique_description"
SCENARIO_ASSUMPTION = "scenario_assumption"

EFFECT_SOURCE_KINDS = (STATE_SOURCE, SKILL_NODE, EQUIPMENT_MOD, UNIQUE_DESCRIPTION, SCENARIO_ASSUMPTION)


def _state_source_check(
    calc: dict[str, Any],
    profile: dict[str, Any],
    control: str,
) -> BindingCheck:
    sources = profile.get("state_sources") or {}
    declared = sources.get(control)
    if declared is None:
        return BindingCheck(BINDING_UNVERIFIED, f"控制项 {control} 未声明状态来源绑定")
    kind = declared.get("kind")
    if kind == EQUIPMENT_MOD:
        return verify_mod(
            calc,
            mod_id=str(declared.get("mod_id") or ""),
            item_instance_id=declared.get("item_instance_id"),
            expected_property=declared.get("expected_property"),
            expected_special_tag=declared.get("expected_special_tag"),
            expected_tags=declared.get("expected_tags"),
            expected_definition_entity_id=declared.get("expected_definition_entity_id"),
            expected_base_entity_id=declared.get("expected_base_entity_id"),
            expected_readable_text_contains=declared.get("expected_readable_text_contains"),
            expected_stat_name_contains=declared.get("expected_stat_name_contains"),
        )
    if kind == UNIQUE_DESCRIPTION:
        return verify_unique_description(
            calc,
            item_instance_id=str(declared.get("item_instance_id") or ""),
            description_index=int(declared.get("description_index") or 0),
            expected_text_contains=str(declared.get("expected_text_contains") or ""),
            expected_definition_entity_id=declared.get("expected_definition_entity_id"),
        )
    return BindingCheck(BINDING_UNVERIFIED, f"不支持的状态来源类型 {kind!r}")


def _resolve_effect_source(
    calc: dict[str, Any],
    catalog: Catalog,
    profile: dict[str, Any],
    declared: dict[str, Any],
) -> tuple[BindingCheck, dict[str, Any], Optional[float], dict[str, Any]]:
    """解析 effect.source；返回 (check, 来源事实, 源数值, 元数据)。"""
    kind = declared.get("kind")
    evidence: list[model.Evidence] = [
        model.Evidence(kind=str(item.get("kind") or "user_rule"), detail=str(item.get("detail") or ""), locator=item.get("locator"))
        for item in declared.get("evidence") or []
    ]
    if kind == STATE_SOURCE:
        control = str(declared.get("control") or "")
        if control not in (profile.get("controls_by_key") or {}):
            return BindingCheck(BINDING_UNRESOLVED, f"状态控制项 {control!r} 未在 controls 中声明"), {}, None, {"evidence": evidence}
        check = _state_source_check(calc, profile, control)
        state_source = (profile.get("state_sources") or {}).get(control) or {}
        if state_source.get("kind") == EQUIPMENT_MOD:
            source_ref = str(state_source.get("mod_id") or "")
        elif state_source.get("kind") == UNIQUE_DESCRIPTION:
            source_ref = (
                f"{state_source.get('item_instance_id')}#unique_description#"
                f"{int(state_source.get('description_index') or 0)}"
            )
        else:
            source_ref = f"control:{control}"
        return check, {"control": control, "state_source": state_source}, None, {
            "evidence": evidence,
            "source_ref": source_ref,
        }
    if kind == SKILL_NODE:
        tree_id = str(declared.get("tree") or profile.get("ability_id") or "")
        node_id = str(declared.get("node") or "")
        stat_index = int(declared.get("stat_index") or 0)
        value, points, check = bind_node_value(
            calc, catalog, tree_id=tree_id, node_id=node_id, stat_index=stat_index
        )
        return check, {"tree": tree_id, "node": node_id, "stat_index": stat_index, "points": points}, value, {"evidence": evidence, "source_ref": f"skill:{tree_id}:{node_id}"}
    if kind == EQUIPMENT_MOD:
        mod_id = str(declared.get("mod_id") or "")
        check = verify_mod(
            calc,
            mod_id=mod_id,
            item_instance_id=declared.get("item_instance_id"),
            expected_property=declared.get("expected_property"),
            expected_special_tag=declared.get("expected_special_tag"),
            expected_tags=declared.get("expected_tags"),
            expected_definition_entity_id=declared.get("expected_definition_entity_id"),
            expected_base_entity_id=declared.get("expected_base_entity_id"),
            expected_readable_text_contains=declared.get("expected_readable_text_contains"),
            expected_stat_name_contains=declared.get("expected_stat_name_contains"),
        )
        value = _num((check.facts or {}).get("value_numeric")) if check.ok else None
        return check, {"mod_id": mod_id, "item_instance_id": check.facts.get("item_instance_id")}, value, {"evidence": evidence, "source_ref": mod_id}
    if kind == UNIQUE_DESCRIPTION:
        item_instance_id = str(declared.get("item_instance_id") or "")
        description_index = int(declared.get("description_index") or 0)
        check = verify_unique_description(
            calc,
            item_instance_id=item_instance_id,
            description_index=description_index,
            expected_text_contains=str(declared.get("expected_text_contains") or ""),
            expected_definition_entity_id=declared.get("expected_definition_entity_id"),
        )
        source_ref = f"{item_instance_id}#unique_description#{description_index}"
        return check, {"item_instance_id": item_instance_id, "description_index": description_index}, None, {
            "evidence": evidence,
            "source_ref": source_ref,
        }
    if kind == SCENARIO_ASSUMPTION:
        control = declared.get("control")
        if control is not None and str(control) not in (profile.get("controls_by_key") or {}):
            return BindingCheck(BINDING_UNRESOLVED, f"状态控制项 {control!r} 未在 controls 中声明"), {}, None, {"evidence": evidence}
        return BindingCheck(
            BINDING_VERIFIED,
            "外部固定场景假设（不冒充装备自带效果）",
            {"control": control},
        ), {"control": control}, None, {"evidence": evidence, "source_ref": f"scenario:{control}"}
    return BindingCheck(BINDING_UNRESOLVED, f"未知来源类型 {kind!r}"), {}, None, {"evidence": evidence}


def _pool_sum(
    pool_lookup: Callable[[str, Optional[str]], float],
    scaling: Optional[dict[str, Any]],
) -> float:
    if not scaling:
        return 0.0
    stat = str(scaling.get("stat") or "")
    target = scaling.get("target")
    return float(pool_lookup(stat, None if target is None else str(target)))


def _declared_evidence(declared: dict[str, Any]) -> list[model.Evidence]:
    return [
        model.Evidence(
            kind=str(item.get("kind") or "user_rule"),
            detail=str(item.get("detail") or ""),
            locator=item.get("locator"),
        )
        for item in declared.get("evidence") or []
    ]


def bind_profile_effects(
    calc: dict[str, Any],
    catalog: Catalog,
    profile: dict[str, Any],
    *,
    states: dict[str, Any],
    target: dict[str, Any],
    pool_lookup: Callable[[str, Optional[str]], float],
) -> list[EffectResult]:
    """逐条绑定 profile.effects：来源验证 → 条件 → 数值 → 贡献。"""
    results: list[EffectResult] = []
    for declared in profile.get("effects") or []:
        effect_id = str(declared.get("id") or "")
        label = str(declared.get("label") or effect_id)
        if not effect_id:
            raise BindingError("effect 缺少 id")
        check, source_facts, source_value, meta = _resolve_effect_source(calc, catalog, profile, declared.get("source") or {})
        evidence = list(meta.get("evidence") or [])
        required_states = [str(key) for key in declared.get("states") or []]
        missing_states = [key for key in required_states if key not in (profile.get("controls_by_key") or {})]
        if missing_states:
            results.append(
                EffectResult(
                    effect_id, label, BINDING_UNRESOLVED,
                    f"效果引用了未声明的控制项 {missing_states}",
                    None, source_facts, {}, evidence, check,
                )
            )
            continue
        state_ok = all(bool(states.get(key)) for key in required_states)
        requires_target = declared.get("requires_target")
        target_ok = True if not requires_target else bool(target.get(requires_target))
        required = bool(declared.get("required", True))
        active = check.ok and state_ok and target_ok
        if active:
            status = BINDING_APPLIED
        elif not check.ok:
            if check.status == BINDING_NOT_APPLICABLE or (
                check.status == BINDING_SOURCE_ABSENT and not required
            ):
                # 可选的合法来源不存在：记录为不适用，不进入覆盖缺口
                status = BINDING_NOT_APPLICABLE
            else:
                status = check.status
        elif not state_ok:
            status = BINDING_CONDITION_NOT_SATISFIED
        else:
            status = BINDING_NOT_APPLICABLE
        reason = None
        if not check.ok:
            reason = check.detail
        elif not state_ok:
            reason = f"状态未满足：{required_states}"
        elif not target_ok:
            reason = f"目标条件未满足：{requires_target}"
        value_source_mode = declared.get("value_source") or "declared"
        if value_source_mode == "source_value":
            raw_value = source_value
            if raw_value is None:
                reason = reason or "声明从来源取值，但来源没有数值"
                status = BINDING_UNRESOLVED
        else:
            raw_value = declared.get("value")
        if raw_value is None:
            results.append(EffectResult(effect_id, label, BINDING_UNRESOLVED, reason or "缺少数值", None, source_facts, {}, evidence, check))
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            results.append(EffectResult(effect_id, label, BINDING_UNRESOLVED, f"数值无效：{raw_value!r}", None, source_facts, {}, evidence, check))
            continue
        scaling_sum = _pool_sum(pool_lookup, declared.get("scaling"))
        value *= 1.0 + scaling_sum
        stack_control = declared.get("stack_control")
        stack_count = 0
        if stack_control:
            if str(stack_control) not in (profile.get("controls_by_key") or {}):
                results.append(EffectResult(effect_id, label, BINDING_UNRESOLVED, f"叠层控制项 {stack_control!r} 未声明", None, source_facts, {}, evidence, check))
                continue
            stack_count = int(states.get(str(stack_control)) or 0)
            value *= stack_count
        stat = declared.get("stat")
        operation = declared.get("operation")
        if not stat or operation not in ("added", "increased", "more", "base", "flag"):
            results.append(EffectResult(effect_id, label, BINDING_UNRESOLVED, "effect 缺少合法 stat/operation", None, source_facts, {}, evidence, check))
            continue
        unit = str(declared.get("unit") or ("percent" if operation in ("increased", "more") else "flat"))
        contribution = mapping.make_contribution(
            contribution_id=f"{meta.get('source_ref') or 'effect:' + effect_id}:{label}",
            source_kind=str(declared.get("source_kind") or _source_kind_for_effect(declared.get("source") or {})),
            source_label=label,
            source_path=str(declared.get("source_path") or meta.get("source_ref") or effect_id),
            stat_name=str(stat),
            stat_key=str(stat),
            operation=str(operation),
            value=float(value),
            unit=unit,
            damage_tags=list(declared.get("tags") or []),
            group=effect_id,
            included=bool(active),
            exclusion_reason=None if active else (reason or "当前场景未启用"),
            value_source="assumed" if value_source_mode != "source_value" else "source",
            status=str(declared.get("status") or model.STATUS_ASSUMED),
            evidence_rows=evidence,
            notes=declared.get("notes"),
            raw={
                "effect_id": effect_id,
                "source": source_facts,
                "binding": check.to_dict(),
                **{
                    key: source_facts.get(key)
                    for key in ("mod_id", "tree", "node", "stat_index")
                    if source_facts.get(key) is not None
                },
            },
        )
        payload = {
            "stack_control": stack_control,
            "stack_count": stack_count,
            "requires_target": requires_target,
            "scaling_sum": scaling_sum,
            "declared_value": declared.get("value"),
            "source_value": source_value,
        }
        results.append(EffectResult(effect_id, label, status, reason, contribution, source_facts, payload, evidence, check))
    return results


def _source_kind_for_effect(declared: dict[str, Any]) -> str:
    kind = declared.get("kind")
    if kind == SKILL_NODE:
        return "skill_node"
    if kind == EQUIPMENT_MOD:
        instance = str(declared.get("item_instance_id") or declared.get("mod_id") or "")
        return mapping.source_kind_for_instance(instance)[0]
    if kind == UNIQUE_DESCRIPTION:
        return mapping.source_kind_for_instance(str(declared.get("item_instance_id") or ""))[0]
    if kind in (STATE_SOURCE, SCENARIO_ASSUMPTION):
        return "derived"
    return "derived"


def bind_state_scalings(
    profile: dict[str, Any],
    *,
    states: dict[str, Any],
    pool_lookup: Callable[[str, Optional[str]], float],
) -> list[EffectResult]:
    """状态集合 × 每状态数值的规则（Blood Rage、rage 移速、Haste 移速）。"""
    results: list[EffectResult] = []
    for declared in profile.get("rules", {}).get("state_scalings") or []:
        rule_id = str(declared.get("id") or "")
        label = str(declared.get("label") or rule_id)
        state_keys = [str(key) for key in declared.get("states") or []]
        missing = [key for key in state_keys if key not in (profile.get("controls_by_key") or {})]
        if not rule_id or not state_keys:
            raise BindingError(f"state_scaling 规则不完整：{rule_id!r}")
        if missing:
            results.append(EffectResult(rule_id, label, BINDING_UNRESOLVED, f"引用了未声明的控制项 {missing}", None))
            continue
        count = sum(1 for key in state_keys if bool(states.get(key)))
        per_state = float(declared.get("per_state"))
        scaling_sum = _pool_sum(pool_lookup, declared.get("scaling"))
        value = per_state * count * (1.0 + scaling_sum)
        output = declared.get("output") or {}
        stat = str(output.get("stat") or "")
        operation = str(output.get("operation") or "increased")
        if not stat:
            raise BindingError(f"state_scaling {rule_id} 缺少 output.stat")
        evidence = _declared_evidence(declared)
        contribution = mapping.make_contribution(
            contribution_id=f"rule:{rule_id}",
            source_kind="derived",
            source_label=label,
            source_path=f"rules.state_scalings.{rule_id}",
            stat_name=stat,
            stat_key=stat,
            operation=operation,
            value=float(value),
            unit="percent" if operation in ("increased", "more") else "flat",
            damage_tags=list(output.get("tags") or []),
            group=rule_id,
            value_source="assumed",
            status=model.STATUS_ASSUMED,
            evidence_rows=evidence,
            notes=declared.get("notes"),
            raw={"rule_id": rule_id, "state_count": count, "scaling_sum": scaling_sum},
        )
        results.append(
            EffectResult(
                rule_id, label, BINDING_APPLIED if count else BINDING_NOT_APPLICABLE,
                None if count else "相关状态未启用", contribution,
                {"states": state_keys}, {"state_count": count, "scaling_sum": scaling_sum}, evidence,
            )
        )
    return results


def bind_movement_conversion(
    calc: dict[str, Any],
    profile: dict[str, Any],
    *,
    movement_total: float,
) -> EffectResult:
    """移速转伤害：来源必须是真实转换装备，输入池不含自身输出。"""
    declared = profile.get("rules", {}).get("movement_conversion")
    if not declared:
        return EffectResult("movement_conversion", "移速转伤害", BINDING_NOT_APPLICABLE, "未声明移速转伤规则", None)
    source = declared.get("source") or {}
    kind = source.get("kind")
    if kind == UNIQUE_DESCRIPTION:
        check = verify_unique_description(
            calc,
            item_instance_id=str(source.get("item_instance_id") or ""),
            description_index=int(source.get("description_index") or 0),
            expected_text_contains=str(source.get("expected_text_contains") or ""),
            expected_definition_entity_id=source.get("expected_definition_entity_id"),
        )
    elif kind == EQUIPMENT_MOD:
        check = verify_mod(
            calc,
            mod_id=str(source.get("mod_id") or ""),
            item_instance_id=source.get("item_instance_id"),
            expected_property=source.get("expected_property"),
            expected_special_tag=source.get("expected_special_tag"),
            expected_definition_entity_id=source.get("expected_definition_entity_id"),
            expected_base_entity_id=source.get("expected_base_entity_id"),
            expected_readable_text_contains=source.get("expected_readable_text_contains"),
            expected_stat_name_contains=source.get("expected_stat_name_contains"),
        )
    elif kind == SCENARIO_ASSUMPTION:
        check = BindingCheck(BINDING_VERIFIED, "外部固定场景假设（不冒充装备自带效果）")
    else:
        check = BindingCheck(BINDING_UNVERIFIED, f"未知的转换来源类型 {kind!r}")
    evidence = _declared_evidence(declared)
    label = str(declared.get("label") or "Vaion's Chariot")
    output = declared.get("output") or {}
    input_stat = str(declared.get("input_stat") or mapping.STAT_MOVEMENT_SPEED)
    output_stat = str(output.get("stat") or "damage")
    if input_stat == output_stat:
        raise BindingError("movement_conversion 的输入与输出属性相同（形成自反馈）")
    ratio = float(declared.get("ratio") or 0.0)
    value = float(movement_total) * ratio
    if not check.ok:
        required = bool(declared.get("required", True))
        status = check.status
        if check.status == BINDING_NOT_APPLICABLE or (
            check.status == BINDING_SOURCE_ABSENT and not required
        ):
            status = BINDING_NOT_APPLICABLE
        return EffectResult("movement_conversion", label, status, check.detail, None, {"source": source}, {}, evidence, check)
    contribution = mapping.make_contribution(
        contribution_id=f"rule:movement_conversion:{label}",
        source_kind=mapping.source_kind_for_instance(str(source.get("item_instance_id") or ""))[0],
        source_label=label,
        source_path=f"rules.movement_conversion",
        stat_name=output_stat,
        stat_key=output_stat,
        operation=str(output.get("operation") or "increased"),
        value=value,
        unit="percent",
        damage_tags=list(output.get("tags") or []),
        group="movement_conversion",
        value_source="derived",
        status=model.STATUS_DERIVED,
        evidence_rows=evidence,
        notes=declared.get("notes"),
        raw={
            "input_stat": input_stat,
            "movement_total": movement_total,
            "ratio": ratio,
            "source": check.facts,
            **({"mod_id": check.facts.get("mod_id")} if check.facts.get("mod_id") else {}),
        },
    )
    return EffectResult(
        "movement_conversion", label, BINDING_APPLIED, None, contribution,
        {"source": source}, {"input_stat": input_stat, "movement_total": movement_total, "ratio": ratio}, evidence, check,
    )


def bind_attribute_scaling(
    profile: dict[str, Any],
    *,
    attributes: dict[str, float],
    effective_strength: float,
) -> EffectResult:
    """属性 → 增伤（每点比例来自 dataset attributeScaling 的规则声明）。"""
    declared = profile.get("rules", {}).get("attribute_scaling")
    if not declared:
        return EffectResult("attribute_scaling", "属性缩放", BINDING_NOT_APPLICABLE, "未声明属性缩放规则", None)
    ratios = declared.get("ratios") or {}
    if not isinstance(ratios, dict):
        raise BindingError("attribute_scaling.ratios 必须是对象")
    scaling_attributes = dict(attributes)
    scaling_attributes["strength"] = float(effective_strength)
    value = 0.0
    parts = []
    for key, ratio in ratios.items():
        contribution_value = float(scaling_attributes.get(str(key), 0.0)) * float(ratio)
        value += contribution_value
        parts.append({"attribute": str(key), "ratio": float(ratio), "value": contribution_value})
    evidence = _declared_evidence(declared)
    output = declared.get("output") or {}
    contribution = mapping.make_contribution(
        contribution_id="rule:attribute_scaling",
        source_kind="derived",
        source_label="属性缩放",
        source_path="rules.attribute_scaling",
        stat_name=str(output.get("stat") or "damage"),
        stat_key=str(output.get("stat") or "damage"),
        operation=str(output.get("operation") or "increased"),
        value=value,
        unit="percent",
        damage_tags=list(output.get("tags") or []),
        group="attribute_scaling",
        value_source="derived",
        status=model.STATUS_DERIVED,
        evidence_rows=evidence,
        notes=declared.get("notes"),
        raw={
            "parts": parts,
            "effective_strength": float(effective_strength),
            "declared_scope": (output.get("scope") if isinstance(output, dict) else None),
        },
    )
    return EffectResult("attribute_scaling", "属性缩放", BINDING_APPLIED, None, contribution, {}, {"parts": parts}, evidence)


def component_rule_payload(
    profile: dict[str, Any],
    *,
    controls_by_key: dict[str, Any],
) -> dict[str, Any]:
    """校验并返回组件/频率规则静态载荷（不涉及输入实例）。"""
    rules = profile.get("rules") or {}
    crit = rules.get("crit") or {}
    payload: dict[str, Any] = {"crit": {}, "every_nth_hit": None, "ailments": [], "damage_to_penetration": None, "cooldown_reduction": None}
    for key in ("deadly", "target_stack_chance"):
        declared = crit.get(key)
        if declared is None:
            continue
        control = str(declared.get("control") or "")
        if control not in controls_by_key:
            raise BindingError(f"rules.crit.{key} 引用未声明控制项 {control!r}")
        entry = dict(declared)
        entry["control"] = control
        for numeric in ("chance", "added_multiplier", "per_stack"):
            if numeric in entry:
                entry[numeric] = _finite(entry[numeric], f"rules.crit.{key}.{numeric}")
        payload["crit"][key] = entry
    every_nth = rules.get("every_nth_hit")
    if every_nth:
        payload["every_nth_hit"] = _validate_every_nth_hit(every_nth)
    for declared in profile.get("ailments") or []:
        entry = dict(declared)
        entry["base_damage"] = _finite(entry.get("base_damage"), "ailments.base_damage")
        entry["duration_s"] = _finite(entry.get("duration_s"), "ailments.duration_s")
        if entry["duration_s"] <= 0:
            raise BindingError(" ailments.duration_s 必须 > 0")
        chance = entry.get("chance") or {}
        if not isinstance(chance, dict):
            raise BindingError("ailments.chance 必须是对象")
        entry["chance"] = chance
        payload["ailments"].append(entry)
    penetration = rules.get("damage_to_penetration")
    if penetration:
        payload["damage_to_penetration"] = dict(penetration)
    cooldown = rules.get("cooldown_reduction")
    if cooldown:
        payload["cooldown_reduction"] = {
            "source": dict(cooldown.get("source") or {}),
            "label": str(cooldown.get("label") or "冷却缩减"),
            "evidence": list(cooldown.get("evidence") or []),
        }
    return payload


def _finite(value: Any, field_name: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        raise BindingError(f"{field_name} 必须是有限数值：{value!r}")
    if not math.isfinite(numeric):
        raise BindingError(f"{field_name} 必须是有限数值：{value!r}")
    return numeric


def _positive_int(value: Any, field_name: str) -> int:
    """正整数校验：拒绝布尔值与小数（``2.5`` 不能静默截断成 2）。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BindingError(f"{field_name} 必须是 >= 1 的整数：{value!r}")
    return int(value)


#: 每第 N 次命中规则允许的字段与受支持口径
EVERY_NTH_HIT_KEYS = frozenset({"n", "extra_more", "counting", "phase", "evidence", "notes"})
EVERY_NTH_COUNTING = ("hit",)
EVERY_NTH_PHASES = ("from_first_hit",)


def _validate_every_nth_hit(declared: dict[str, Any]) -> dict[str, Any]:
    """校验“每第 N 次命中”规则：N 为整数、计数策略与相位必须被显式支持。"""
    if not isinstance(declared, dict):
        raise BindingError("rules.every_nth_hit 必须是对象")
    unknown = set(declared) - EVERY_NTH_HIT_KEYS
    if unknown:
        raise BindingError(f"rules.every_nth_hit 含未支持的字段：{sorted(unknown)}")
    n = _positive_int(declared.get("n"), "rules.every_nth_hit.n")
    counting = str(declared.get("counting") or "hit")
    if counting not in EVERY_NTH_COUNTING:
        raise BindingError(f"不支持的计数策略 rules.every_nth_hit.counting={counting!r}")
    phase = declared.get("phase")
    if phase is not None and str(phase) not in EVERY_NTH_PHASES:
        raise BindingError(f"不支持的时间相位 rules.every_nth_hit.phase={phase!r}")
    return {
        "n": n,
        "extra_more": _finite(declared.get("extra_more", 0.0), "rules.every_nth_hit.extra_more"),
        "counting": counting,
        "phase": str(phase) if phase is not None else "from_first_hit",
    }


# ---------------------------------------------------------------------------
# 属性依赖缩放 / 技能节点绑定 / 冷却节点 / 异常额外施加
# ---------------------------------------------------------------------------


def resolve_per_attribute(
    row: model.Contribution,
    stat: dict[str, Any],
    attributes: dict[str, float],
) -> tuple[model.Contribution, Optional[str]]:
    """解析 planner ``per`` 缩放行：属性池就绪后再乘以 属性值/每点阈值。

    未声明的缩放属性不猜值：行保持未解析，并作为覆盖缺口候选。
    """
    entries = stat.get("per") or []
    if not entries:
        return row, None
    problem: Optional[str] = None
    for entry in entries:
        stat_id = _int_or_none(entry.get("stat"))
        per_value = _num(entry.get("value"))
        attribute_key = mapping.ATTRIBUTE_PROPERTIES.get(stat_id) if stat_id is not None else None
        if attribute_key and not entry.get("specialTag") and per_value > 0:
            row.value = float(row.value) * float(attributes.get(attribute_key, 0.0)) / per_value
            row.notes = (row.notes or "") + (
                f"；按 {attribute_key} 缩放（每 {per_value:g} 点）"
            )
        else:
            row.included = False
            row.status = model.STATUS_UNRESOLVED
            row.exclusion_reason = (
                f"per 缩放属性 {stat_id!r} 未声明（覆盖缺口）"
            )
            problem = row.exclusion_reason
    return row, problem


def bind_node_bindings(
    calc: dict[str, Any],
    catalog: Catalog,
    profile: dict[str, Any],
    *,
    states: dict[str, Any],
) -> list[EffectResult]:
    """技能节点绑定：tree/node/stat_index/points 校验 + 当前点数数值。"""
    results: list[EffectResult] = []
    ability_id = str(profile.get("ability_id") or "")
    controls_by_key = profile.get("controls_by_key") or {}
    target_keys = {"monster_level", "resistance", "high_health", "boss", "blinded", "slowed"}
    for index, rule in enumerate(profile.get("node_bindings") or []):
        tree_id = str(rule.get("tree") or ability_id)
        node_id = str(rule.get("node") or "")
        stat_index = int(rule.get("stat_index") or 0)
        rule_id = str(rule.get("id") or f"node:{tree_id}:{node_id}:{stat_index}")
        value, points, check = bind_node_value(
            calc, catalog, tree_id=tree_id, node_id=node_id, stat_index=stat_index
        )
        spec = _specialization_for(calc, tree_id)
        node_name = ""
        if spec is not None:
            entry = next(
                (node for node in spec.get("nodes") or [] if str(node.get("node_id")) == node_id),
                None,
            )
            node_name = str((entry or {}).get("name") or f"node {node_id}")
        evidence = [
            model.Evidence(
                kind="catalog",
                detail=f"tree {tree_id} node {node_id} stat_index {stat_index}",
                locator=f"data/releases/{calc.get('dataset_id')}/data.sqlite#trees source_tree_id={tree_id}",
            )
        ]
        condition_key = rule.get("condition")
        requires = [str(key) for key in rule.get("requires_states") or []]
        for key in ([str(condition_key)] if condition_key else []) + requires:
            if key not in controls_by_key and key not in target_keys:
                raise BindingError(f"节点绑定 {rule_id} 引用了未声明的条件键 {key!r}")
        per_stack = rule.get("per_stack")
        if per_stack is not None and str(per_stack) not in controls_by_key:
            raise BindingError(f"节点绑定 {rule_id} 的 per_stack {per_stack!r} 未声明")
        active = check.ok
        reason = None if check.ok else check.detail
        computed = value
        if active:
            if condition_key and not bool(states.get(str(condition_key))):
                active = False
                reason = f"条件未满足：{condition_key}"
            elif not all(bool(states.get(key)) for key in requires):
                active = False
                reason = f"条件未满足：{requires}"
            if per_stack is not None:
                computed = float(computed) * int(states.get(str(per_stack)) or 0)
            cap = rule.get("cap_per_point")
            if cap is not None:
                computed = min(float(computed), float(cap) * points)
        contribution = None
        if computed is not None:
            contribution = mapping.make_contribution(
                contribution_id=f"skill:{tree_id}:{node_id}:{node_name or 'node'}",
                source_kind="skill_node",
                source_label=node_name or f"{tree_id}:{node_id}",
                source_path=f"skill_tree.{tree_id}.nodes.{node_id}",
                stat_name=str(rule.get("stat_name") or rule.get("stat") or ""),
                stat_key=str(rule.get("stat") or ""),
                operation=str(rule.get("operation") or "increased"),
                value=float(computed),
                unit="percent" if rule.get("operation") in ("increased", "more") else "flat",
                damage_tags=list(rule.get("tags") or []),
                group=f"node:{tree_id}:{node_id}",
                included=bool(active),
                exclusion_reason=None if active else (reason or "当前场景未启用"),
                value_source="source",
                status=model.STATUS_DERIVED,
                evidence_rows=evidence,
                notes=rule.get("notes"),
                raw={
                    "rule_id": rule_id,
                    "tree": tree_id,
                    "node": node_id,
                    "stat_index": stat_index,
                    "points": points,
                    "per_point_value": value / points if points else None,
                    "binding": check.to_dict(),
                },
            )
        results.append(
            EffectResult(
                rule_id,
                node_name or rule_id,
                BINDING_APPLIED if active else (check.status if not check.ok else BINDING_CONDITION_NOT_SATISFIED),
                reason,
                contribution,
                {"tree": tree_id, "node": node_id, "stat_index": stat_index, "points": points},
                {"computed_value": computed, "per_stack": per_stack, "cap": rule.get("cap_per_point")},
                evidence,
                check,
            )
        )
    return results


def bind_cooldown_reduction(
    calc: dict[str, Any],
    catalog: Catalog,
    profile: dict[str, Any],
) -> dict[str, Any]:
    """冷却节点：当前点数 × 目录每点缩减比例。"""
    declared = (profile.get("rules") or {}).get("cooldown_reduction")
    if not declared:
        return {"status": BINDING_NOT_APPLICABLE, "reduction": 0.0, "points": 0, "contribution": None}
    source = declared.get("source") or {}
    tree_id = str(source.get("tree") or profile.get("ability_id") or "")
    node_id = str(source.get("node") or "")
    stat_index = int(source.get("stat_index") or 0)
    value, points, check = bind_node_value(calc, catalog, tree_id=tree_id, node_id=node_id, stat_index=stat_index)
    evidence = [
        model.Evidence(
            kind="catalog",
            detail=f"cooldown node {tree_id}:{node_id} stat_index {stat_index}",
            locator=f"data/releases/{calc.get('dataset_id')}/data.sqlite#trees source_tree_id={tree_id}",
        )
    ] + _declared_evidence(declared)
    contribution = None
    if value is not None:
        contribution = mapping.make_contribution(
            contribution_id=f"skill:{tree_id}:{node_id}:cooldown_reduction",
            source_kind="skill_node",
            source_label=str(declared.get("label") or f"{tree_id}:{node_id}"),
            source_path=f"skill_tree.{tree_id}.nodes.{node_id}",
            stat_name="cooldown_reduction",
            stat_key=model.STAT_COOLDOWN_REDUCTION,
            operation="added",
            value=float(value),
            unit="percent",
            group=f"node:{tree_id}:{node_id}",
            value_source="source",
            status=model.STATUS_DERIVED,
            evidence_rows=evidence,
            raw={"rule": "cooldown_reduction", "tree": tree_id, "node": node_id, "stat_index": stat_index, "points": points},
        )
    return {
        "status": check.status,
        "reduction": float(value or 0.0),
        "points": points,
        "contribution": contribution,
        "evidence": evidence,
        "detail": check.detail,
    }


def resolve_ailment_additional_chance(
    calc: dict[str, Any],
    catalog: Catalog,
    profile: dict[str, Any],
    *,
    states: dict[str, Any],
) -> dict[str, Any]:
    """异常施加的额外概率来源（技能节点），逐条校验节点点数。"""
    result: dict[str, Any] = {"chance": {}, "gaps": [], "contributions": []}
    for declared in profile.get("ailments") or []:
        name = str(declared.get("name") or "")
        chance_block = declared.get("chance") or {}
        total = 0.0
        for source in chance_block.get("additional") or []:
            tree_id = str(source.get("tree") or profile.get("ability_id") or "")
            node_id = str(source.get("node") or "")
            stat_index = int(source.get("stat_index") or 0)
            value, points, check = bind_node_value(
                calc, catalog, tree_id=tree_id, node_id=node_id, stat_index=stat_index
            )
            if value is None:
                if check.status != BINDING_NOT_APPLICABLE:
                    result["gaps"].append(
                        {
                            "code": "ailment_chance_source_absent",
                            "detail": f"{name} 的额外施加概率来源 {tree_id}:{node_id} 不可用：{check.detail}",
                        }
                    )
                continue
            condition = source.get("condition")
            if condition and not bool(states.get(str(condition))):
                continue
            total += float(value)
            result["contributions"].append(
                mapping.make_contribution(
                    contribution_id=f"skill:{tree_id}:{node_id}:{name}_chance",
                    source_kind="skill_node",
                    source_label=f"{name} 施加概率",
                    source_path=f"skill_tree.{tree_id}.nodes.{node_id}",
                    stat_name="ailment_chance",
                    stat_key=model.STAT_AILMENT_CHANCE,
                    operation="added",
                    value=float(value),
                    unit="percent",
                    target=str(declared.get("ailment_id")),
                    group=f"node:{tree_id}:{node_id}",
                    value_source="source",
                    status=model.STATUS_DERIVED,
                    evidence_rows=[
                        model.Evidence(
                            kind="catalog",
                            detail=f"{name} 施加概率节点 {tree_id}:{node_id}",
                            locator=f"data/releases/{calc.get('dataset_id')}/data.sqlite#trees source_tree_id={tree_id}",
                        )
                    ],
                    raw={"ailment": name, "points": points},
                )
            )
        result["chance"][name] = total
    return result


def verify_ailment_constants(profile: dict[str, Any], catalog: Catalog) -> dict[str, Any]:
    """异常条目的伤害类型/基础伤害/持续时间与 dataset 对照。"""
    differences: list[str] = []
    observed: dict[str, Any] = {}
    for declared in profile.get("ailments") or []:
        name = str(declared.get("name") or "")
        try:
            fact = catalog.ailment_by_name(name)
        except Exception:
            differences.append(f"{name}: dataset 中缺少该异常")
            continue
        observed[name] = {
            "damage_type": fact.get("damage_type"),
            "base_damage": fact.get("base_damage"),
            "duration_ms": fact.get("duration_ms"),
        }
        if str(declared.get("damage_type")) != str(fact.get("damage_type")):
            differences.append(f"{name}.damage_type")
        if abs(float(declared.get("base_damage") or 0.0) - float(fact.get("base_damage") or 0.0)) > 1e-9:
            differences.append(f"{name}.base_damage")
        duration_ms = fact.get("duration_ms")
        if duration_ms is not None and abs(
            float(declared.get("duration_s") or 0.0) - float(duration_ms) / 1000.0
        ) > 1e-9:
            differences.append(f"{name}.duration_s")
    return {
        "status": BINDING_VERIFIED if not differences else BINDING_SEMANTICS_MISMATCH,
        "reason": "" if not differences else f"异常常量与 dataset 不一致：{differences}",
        "observed": observed,
        "differences": differences,
    }


def _skill_by_entity_id(catalog: Catalog, entity_id: str) -> dict[str, Any]:
    rows = [row for row in catalog.skills() if str(row.get("entity_id")) == str(entity_id)]
    if len(rows) != 1:
        raise BindingError(f"dataset skills 中找不到唯一 skill entity：{entity_id!r}")
    return rows[0]


def verify_skill_constants(
    profile: dict[str, Any],
    catalog: Catalog,
    *,
    ability_id: str,
    skill_name: str,
    damage_source_entity_id: Optional[str] = None,
) -> dict[str, Any]:
    """把 profile 的技能常量与 dataset 伤害来源组件对照；不一致形成明确缺口。

    ``damage_source_entity_id`` 非空时（actor 装配），以声明的 dataset ability 实体为
    伤害来源（例如 ability_gen327），而不是主技能；否则沿用旧行为（主技能）。
    """
    skill = (
        _skill_by_entity_id(catalog, damage_source_entity_id)
        if damage_source_entity_id
        else catalog.skill_by_name(skill_name)
    )
    components = catalog.skill_components(skill["entity_id"])
    damage_component = next(
        (component for component in components if component.get("component_type") == "DamageEnemyOnHit"),
        None,
    )
    if damage_component is None:
        return {"status": BINDING_UNRESOLVED, "reason": "目录中没有 DamageEnemyOnHit 组件", "differences": []}
    config = (damage_component.get("config") or {}).get("baseDamageStats") or {}
    observed = {
        "dataset_base_damage": float(damage_component.get("base_damage") or 0.0),
        "dataset_damage_type": damage_component.get("damage_type"),
        "dataset_added_damage_scaling": _num(config.get("addedDamageScaling")),
        "dataset_crit_chance": _num(config.get("critChance")),
        "dataset_crit_multiplier": _num(config.get("critMultiplier")),
        "dataset_damage_tags": model.decode_tags(_int_or_none(damage_component.get("damageTags")) or 0),
        "dataset_cast_time_ms": skill.get("cast_time_ms"),
        "dataset_mana_cost": skill.get("mana_cost"),
        "dataset_attribute_scaling": (skill.get("extensions") or {}).get("attributeScaling"),
    }
    declared_skill = profile.get("skill") or {}
    differences: list[str] = []
    if abs(float(declared_skill.get("added_damage_scaling") or 0.0) - observed["dataset_added_damage_scaling"]) > 1e-9:
        differences.append("added_damage_scaling")
    if abs(float(declared_skill.get("crit_chance") or 0.0) - observed["dataset_crit_chance"]) > 1e-9:
        differences.append("crit_chance")
    if abs(float(declared_skill.get("crit_multiplier") or 0.0) - observed["dataset_crit_multiplier"]) > 1e-9:
        differences.append("crit_multiplier")
    cast_time = observed["dataset_cast_time_ms"]
    if (
        not damage_source_entity_id
        and cast_time is not None
        and abs(float(declared_skill.get("action_s") or 0.0) - float(cast_time) / 1000.0) > 1e-9
    ):
        # actor 的 damage source 是内部 ability（如 ability_gen327），其 cast_time 不是
        # 玩家施放/事件调度常量，不参与技能常量一致性判定（节点/频率由派生频率声明负责）。
        differences.append("action_s")
    declared_tags = [
        str(tag)
        for tag in ((declared_skill.get("components") or {}).get("hit") or {}).get("tags") or []
    ]
    observed_tags = set(observed["dataset_damage_tags"] or [])
    if declared_tags and observed_tags and not (set(declared_tags) & observed_tags):
        differences.append("damage_tags")
    return {
        "status": BINDING_VERIFIED if not differences else BINDING_SEMANTICS_MISMATCH,
        "reason": "" if not differences else f"profile 技能常量与目录不一致：{differences}",
        "observed": observed,
        "differences": differences,
    }


# ---------------------------------------------------------------------------
# profile 校验
# ---------------------------------------------------------------------------

KNOWN_RULE_HANDLERS = frozenset(
    {
        "crit",
        "every_nth_hit",
        "state_scalings",
        "movement_conversion",
        "damage_to_penetration",
        "cooldown_reduction",
        "attribute_scaling",
        "attribute_conversion",
        # plan-v2 扩展（FR/BH 迁移）：多命中/频率/物品效率/附加伤害换算。
        "hit_events",
        "rotation",
        "item_effectiveness",
        "added_damage_conversion",
        "row_normalizations",
        # plan-v3 扩展（BH 纯 DoT / 独立施加时钟 / 充能调度）。
        "dot_components",
        "ignite_model",
        "charge_schedule",
        "dot_target",
        "row_reclassifications",
        "overcapped_scaling",
        # 通用 actor/派生频率扩展（数据声明式）。
        "stat_transfer",
        "derived_events",
        "node_attribute_scaled",
    }
)
KNOWN_EFFECT_SOURCE_KINDS = frozenset(EFFECT_SOURCE_KINDS)

#: plan-v2 规则处理器允许的字段（未知字段拒绝；不静默放宽 v1）。
HIT_EVENTS_RULE_KEYS = frozenset(
    {
        "base_hits",
        "return_sources",
        "extra_hit_damage_multiplier",
        "damage_weights",
        "evidence",
        "notes",
    }
)
ROTATION_RULE_KEYS = frozenset(
    {"policies", "primary_policy", "windows_s", "window_semantics", "rhythm_cycle", "evidence", "notes"}
)
RHYTHM_CYCLE_KEYS = frozenset(
    {"cycle_casts", "consume_more", "status", "evidence", "notes", "label"}
)
ITEM_EFFECTIVENESS_RULE_KEYS = frozenset(
    {
        "label",
        "source",
        "target_slot",
        "require_display_rarity",
        "excluded_base_type_ids",
        "tier0_min",
        "required",
        "evidence",
        "notes",
    }
)
ADDED_DAMAGE_CONVERSION_RULE_KEYS = frozenset(
    {
        "label",
        "source_pool",
        "divisor",
        "output",
        "node",
        "status",
        "required",
        "evidence",
        "notes",
    }
)
ROW_NORMALIZATION_MATCH_KEYS = frozenset(
    {
        "contribution_id",
        "source_kind",
        "node_id",
        "index",
        "tree",
        "node",
        "stat_index",
        "stat_key",
        "operation",
        "damage_tags",
    }
)
ROW_NORMALIZATION_SET_KEYS = frozenset(
    {
        "value",
        "included",
        "condition_code",
        "condition_status",
        "exclusion_reason",
        "status",
        "note",
    }
)
ROW_NORMALIZATION_KEYS = frozenset({"id", "label", "match", "set", "status", "evidence", "notes"})
DOT_COMPONENTS_RULE_KEYS = frozenset(
    {
        "label",
        "base_duration_s",
        "duration_stat_key",
        "tick_interval_s",
        "damage_at_start",
        "remainder_policy",
        "added_damage_scaling",
        "components",
        "evidence",
        "notes",
    }
)
IGNITE_MODEL_RULE_KEYS = frozenset(
    {
        "label",
        "ailment",
        "damage_type",
        "base_damage",
        "base_duration_s",
        "scaling_tags",
        "duration_stat_key",
        "rolls_per_second",
        "first_roll_at",
        "chance_source",
        "scope",
        "evidence",
        "notes",
    }
)
CHARGE_SCHEDULE_RULE_KEYS = frozenset(
    {
        "label",
        "base_max_charges",
        "base_recharge_s",
        "max_charges_source",
        "flat_source",
        "cdr_source",
        "horizon_s",
        "windows_s",
        "action_policies",
        "primary_policy",
        "evidence",
        "notes",
    }
)
DOT_TARGET_RULE_KEYS = frozenset(
    {"label", "monster_damage_reduction_control", "monster_damage_reduction_default", "evidence", "notes"}
)
ROW_RECLASSIFICATION_MATCH_KEYS = frozenset(
    {
        "contribution_id",
        "source_kind",
        "raw_property",
        "raw_special_tag",
        "raw_stat_property",
        "raw_stat_special_tag",
        "raw_tags_contains",
        "stat_name_contains",
        "readable_text_contains",
        "item_slot",
        "stat_key",
        "operation",
        "target",
    }
)
ROW_RECLASSIFICATION_SET_KEYS = frozenset(
    {"stat_key", "operation", "target", "damage_tags", "included", "exclusion_reason", "status", "notes"}
)
ROW_RECLASSIFICATION_KEYS = frozenset(
    {"id", "label", "match", "set", "exclude", "reason", "evidence", "notes"}
)
OVERSCALED_RULE_KEYS = frozenset(
    {"id", "label", "match", "source_resistance", "threshold_percent", "cap_percent", "evidence", "notes"}
)
PLAN_V3_RULE_VERSION = "le-calculation-plan-v3"


def _finite_positive(value: Any, field_name: str) -> float:
    number = _finite(value, field_name)
    if number <= 0:
        raise BindingError(f"{field_name} 必须为正数")
    return number


def _finite_nonnegative(value: Any, field_name: str) -> float:
    number = _finite(value, field_name)
    if number < 0:
        raise BindingError(f"{field_name} 不能为负")
    return number


def validate_plan_v3_rules(rules: Mapping[str, Any]) -> None:
    """plan-v3 规则结构校验；未知规则/未知字段/未实现语义一律拒绝。"""
    if not isinstance(rules, Mapping):
        raise BindingError("rules 必须是对象")
    unknown = set(rules) - KNOWN_RULE_HANDLERS
    if unknown:
        raise BindingError(f"plan-v3 定义含未知规则：{sorted(unknown)}")
    for required in ("dot_components", "ignite_model", "charge_schedule"):
        if not isinstance(rules.get(required), dict):
            raise BindingError(f"plan-v3 定义缺少 rules.{required}")

    def declared(rule_name: str, allowed: frozenset[str]) -> dict[str, Any]:
        rule = rules[rule_name]
        extra = set(rule) - allowed
        if extra:
            raise BindingError(f"rules.{rule_name} 含未支持字段：{sorted(extra)}")
        return rule

    dot = declared("dot_components", DOT_COMPONENTS_RULE_KEYS)
    _finite_positive(dot.get("base_duration_s"), "rules.dot_components.base_duration_s")
    _finite_positive(dot.get("tick_interval_s"), "rules.dot_components.tick_interval_s")
    if dot.get("remainder_policy") != "ceil_full_ticks":
        raise BindingError("rules.dot_components.remainder_policy 只支持 ceil_full_ticks")
    if dot.get("damage_at_start") is not True:
        raise BindingError("rules.dot_components.damage_at_start 只支持 true（false 未实现）")
    _finite_nonnegative(dot.get("added_damage_scaling"), "rules.dot_components.added_damage_scaling")
    if not isinstance(dot.get("components"), list) or not dot["components"]:
        raise BindingError("rules.dot_components.components 必须是非空数组")
    for index, component in enumerate(dot["components"]):
        if not isinstance(component, dict):
            raise BindingError(f"rules.dot_components.components[{index}] 必须是对象")
        extra = set(component) - {"component_id", "damage_type", "tags", "base_source", "require_nodes", "evidence", "notes"}
        if extra:
            raise BindingError(f"rules.dot_components.components[{index}] 含未支持字段：{sorted(extra)}")
        if not isinstance(component.get("component_id"), str) or not component["component_id"]:
            raise BindingError(f"rules.dot_components.components[{index}].component_id 缺失")
        base_source = component.get("base_source")
        if not isinstance(base_source, dict) or base_source.get("kind") not in (
            "skill_base",
            "binary_converted",
            "binary_original",
        ):
            raise BindingError(f"rules.dot_components.components[{index}].base_source 非法")
        for node in component.get("require_nodes") or []:
            if not isinstance(node, dict) or not node.get("tree") or not node.get("node"):
                raise BindingError(f"rules.dot_components.components[{index}].require_nodes 非法")

    ignite = declared("ignite_model", IGNITE_MODEL_RULE_KEYS)
    _finite_positive(ignite.get("base_damage"), "rules.ignite_model.base_damage")
    _finite_positive(ignite.get("base_duration_s"), "rules.ignite_model.base_duration_s")
    if not isinstance(ignite.get("scaling_tags"), list) or not ignite["scaling_tags"]:
        raise BindingError("rules.ignite_model.scaling_tags 必须是非空数组")
    _finite_positive(ignite.get("rolls_per_second"), "rules.ignite_model.rolls_per_second")
    if ignite.get("first_roll_at") != "spawn":
        raise BindingError("rules.ignite_model.first_roll_at 只支持 spawn")
    chance = ignite.get("chance_source")
    if not isinstance(chance, dict) or not chance.get("tree") or not chance.get("node"):
        raise BindingError("rules.ignite_model.chance_source 必须是 {tree,node,stat_index}")

    schedule = declared("charge_schedule", CHARGE_SCHEDULE_RULE_KEYS)
    _positive_int(schedule.get("base_max_charges"), "rules.charge_schedule.base_max_charges")
    _finite_positive(schedule.get("base_recharge_s"), "rules.charge_schedule.base_recharge_s")
    for key in ("max_charges_source", "flat_source", "cdr_source"):
        if key in schedule and not isinstance(schedule.get(key), dict):
            raise BindingError(f"rules.charge_schedule.{key} 必须是对象")
    _finite_positive(schedule.get("horizon_s"), "rules.charge_schedule.horizon_s")
    if not isinstance(schedule.get("windows_s"), list) or not schedule["windows_s"]:
        raise BindingError("rules.charge_schedule.windows_s 必须是非空数组")
    if not isinstance(schedule.get("action_policies"), list):
        raise BindingError("rules.charge_schedule.action_policies 必须是数组")
    if schedule.get("primary_policy") not in ("eager", "patient"):
        raise BindingError("rules.charge_schedule.primary_policy 必须是 eager/patient")

    target = declared("dot_target", DOT_TARGET_RULE_KEYS)
    if not isinstance(target.get("monster_damage_reduction_control"), str):
        raise BindingError("rules.dot_target.monster_damage_reduction_control 必须是字符串")
    _finite_nonnegative(target.get("monster_damage_reduction_default", 0.0), "rules.dot_target.monster_damage_reduction_default")

    reclass = rules.get("row_reclassifications")
    if reclass is not None:
        if not isinstance(reclass, list):
            raise BindingError("rules.row_reclassifications 必须是数组")
        for index, rule in enumerate(reclass):
            if not isinstance(rule, dict):
                raise BindingError(f"rules.row_reclassifications[{index}] 必须是对象")
            extra = set(rule) - ROW_RECLASSIFICATION_KEYS
            if extra:
                raise BindingError(f"rules.row_reclassifications[{index}] 含未支持字段：{sorted(extra)}")
            match = rule.get("match")
            if not isinstance(match, dict) or not match:
                raise BindingError(f"rules.row_reclassifications[{index}].match 必须是非空对象")
            extra = set(match) - ROW_RECLASSIFICATION_MATCH_KEYS
            if extra:
                raise BindingError(f"rules.row_reclassifications[{index}].match 含未支持字段：{sorted(extra)}")
            if rule.get("exclude"):
                if not rule.get("reason"):
                    raise BindingError(f"rules.row_reclassifications[{index}] 排除规则必须给出 reason")
            else:
                settings = rule.get("set")
                if not isinstance(settings, dict) or not settings:
                    raise BindingError(f"rules.row_reclassifications[{index}].set 必须是非空对象")
                extra = set(settings) - ROW_RECLASSIFICATION_SET_KEYS
                if extra:
                    raise BindingError(f"rules.row_reclassifications[{index}].set 含未支持字段：{sorted(extra)}")

    overcap = rules.get("overcapped_scaling")
    if overcap is not None:
        if not isinstance(overcap, list):
            raise BindingError("rules.overcapped_scaling 必须是数组")
        for index, rule in enumerate(overcap):
            if not isinstance(rule, dict):
                raise BindingError(f"rules.overcapped_scaling[{index}] 必须是对象")
            extra = set(rule) - OVERSCALED_RULE_KEYS
            if extra:
                raise BindingError(f"rules.overcapped_scaling[{index}] 含未支持字段：{sorted(extra)}")
            if not isinstance(rule.get("match"), dict) or not rule["match"]:
                raise BindingError(f"rules.overcapped_scaling[{index}].match 必须是非空对象")
            if rule.get("source_resistance") not in ("fire", "cold", "lightning", "physical", "void", "necrotic", "poison"):
                raise BindingError(f"rules.overcapped_scaling[{index}].source_resistance 非法")
            _finite_positive(rule.get("threshold_percent"), f"rules.overcapped_scaling[{index}].threshold_percent")
            _finite_positive(rule.get("cap_percent"), f"rules.overcapped_scaling[{index}].cap_percent")


PLAN_V2_RULE_VERSION = "le-calculation-plan-v2"


def _plan_is_v2(payload: Mapping[str, Any]) -> bool:
    return str(payload.get("plan_version") or "") == PLAN_V2_RULE_VERSION


def _validate_hit_events_rule(declared: Any) -> None:
    if not isinstance(declared, dict):
        raise BindingError("rules.hit_events 必须是对象")
    unknown = set(declared) - HIT_EVENTS_RULE_KEYS
    if unknown:
        raise BindingError(f"rules.hit_events 含未支持的字段：{sorted(unknown)}")
    base_hits = declared.get("base_hits", 1)
    if isinstance(base_hits, bool) or not isinstance(base_hits, int) or base_hits < 1:
        raise BindingError("rules.hit_events.base_hits 必须是 >=1 的整数")
    sources = declared.get("return_sources")
    if not isinstance(sources, list) or not sources:
        raise BindingError("rules.hit_events.return_sources 必须是非空数组")
    for index, source in enumerate(sources):
        field = f"rules.hit_events.return_sources[{index}]"
        if not isinstance(source, dict):
            raise BindingError(f"{field} 必须是对象")
        unknown = set(source) - {"tree", "node", "require_points", "returns", "evidence"}
        if unknown:
            raise BindingError(f"{field} 含未支持的字段：{sorted(unknown)}")
        if not isinstance(source.get("node"), str) or not source.get("node"):
            raise BindingError(f"{field}.node 缺失")
        require_points = source.get("require_points", 1)
        if isinstance(require_points, bool) or not isinstance(require_points, int) or require_points < 1:
            raise BindingError(f"{field}.require_points 必须是 >=1 的整数")
        returns = source.get("returns")
        if isinstance(returns, bool) or not isinstance(returns, int) or returns < 1:
            raise BindingError(f"{field}.returns 必须是 >=1 的整数")
    multiplier = declared.get("extra_hit_damage_multiplier", 0.5)
    _finite(multiplier, "rules.hit_events.extra_hit_damage_multiplier")
    if not 0.0 <= float(multiplier) <= 1.0:
        raise BindingError("rules.hit_events.extra_hit_damage_multiplier 必须在 [0,1]")
    weights = declared.get("damage_weights")
    if weights is not None:
        if not isinstance(weights, list) or not weights:
            raise BindingError("rules.hit_events.damage_weights 必须是正数数组")
        for value in weights:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise BindingError("rules.hit_events.damage_weights 必须是数值数组")
            _finite(value, "rules.hit_events.damage_weights[]")
            if float(value) < 0:
                raise BindingError("rules.hit_events.damage_weights 不能为负")


def _validate_rotation_rule(declared: Any) -> None:
    if not isinstance(declared, dict):
        raise BindingError("rules.rotation 必须是对象")
    unknown = set(declared) - ROTATION_RULE_KEYS
    if unknown:
        raise BindingError(f"rules.rotation 含未支持的字段：{sorted(unknown)}")
    policies = declared.get("policies") or ["cooldown_limited"]
    if not isinstance(policies, list) or not policies:
        raise BindingError("rules.rotation.policies 必须是非空数组")
    for policy in policies:
        if policy not in ("cooldown_limited", "attack_speed_limited"):
            raise BindingError(f"rules.rotation.policies 含未支持策略：{policy!r}")
    primary = str(declared.get("primary_policy") or policies[0])
    if primary not in policies:
        raise BindingError("rules.rotation.primary_policy 必须在 policies 中")
    windows = declared.get("windows_s")
    if windows is not None:
        if not isinstance(windows, list) or not windows or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0
            for value in windows
        ):
            raise BindingError("rules.rotation.windows_s 必须是正数数组")
    semantics = str(declared.get("window_semantics") or "expected_casts")
    if semantics not in ("expected_casts", "complete_casts"):
        raise BindingError(f"rules.rotation.window_semantics 不受支持：{semantics!r}")
    cycle = declared.get("rhythm_cycle")
    if cycle is not None:
        if not isinstance(cycle, dict):
            raise BindingError("rules.rotation.rhythm_cycle 必须是对象")
        unknown = set(cycle) - RHYTHM_CYCLE_KEYS
        if unknown:
            raise BindingError(f"rules.rotation.rhythm_cycle 含未支持的字段：{sorted(unknown)}")
        casts = cycle.get("cycle_casts")
        if isinstance(casts, bool) or not isinstance(casts, int) or casts < 2:
            raise BindingError("rules.rotation.rhythm_cycle.cycle_casts 必须是 >=2 的整数")
        consume = cycle.get("consume_more")
        if not isinstance(consume, dict):
            raise BindingError("rules.rotation.rhythm_cycle.consume_more 必须是对象")
        unknown = set(consume) - {"tree", "node", "stat_index", "evidence"}
        if unknown:
            raise BindingError(
                f"rules.rotation.rhythm_cycle.consume_more 含未支持的字段：{sorted(unknown)}"
            )
        for key in ("tree", "node"):
            if not isinstance(consume.get(key), str) or not consume.get(key):
                raise BindingError(f"rules.rotation.rhythm_cycle.consume_more 缺少 {key}")
        stat_index = consume.get("stat_index", 0)
        if isinstance(stat_index, bool) or not isinstance(stat_index, int) or stat_index < 0:
            raise BindingError("rules.rotation.rhythm_cycle.consume_more.stat_index 必须是 >=0 的整数")
        status = str(cycle.get("status") or "assumed")
        if status not in ("confirmed", "derived", "assumed", "unresolved"):
            raise BindingError("rules.rotation.rhythm_cycle.status 不受支持")


def _validate_item_effectiveness_rule(declared: Any) -> None:
    if not isinstance(declared, dict):
        raise BindingError("rules.item_effectiveness 必须是对象")
    unknown = set(declared) - ITEM_EFFECTIVENESS_RULE_KEYS
    if unknown:
        raise BindingError(f"rules.item_effectiveness 含未支持的字段：{sorted(unknown)}")
    source = declared.get("source")
    if not isinstance(source, dict):
        raise BindingError("rules.item_effectiveness.source 必须是对象")
    if source.get("kind") not in (EQUIPMENT_MOD, UNIQUE_DESCRIPTION):
        raise BindingError("rules.item_effectiveness.source 只支持 equipment_mod/unique_description")
    target_slot = declared.get("target_slot")
    if not isinstance(target_slot, str) or not target_slot.startswith("equipment."):
        raise BindingError("rules.item_effectiveness.target_slot 必须是 equipment.* 槽位")
    rarity = declared.get("require_display_rarity")
    if rarity is not None and rarity not in ("exalted", "unique", "normal"):
        raise BindingError("rules.item_effectiveness.require_display_rarity 不受支持")
    excluded = declared.get("excluded_base_type_ids") or []
    if not isinstance(excluded, list) or any(
        isinstance(value, bool) or not isinstance(value, int) for value in excluded
    ):
        raise BindingError("rules.item_effectiveness.excluded_base_type_ids 必须是整数数组")
    tier = declared.get("tier0_min")
    if tier is not None and (isinstance(tier, bool) or not isinstance(tier, int) or tier < 0):
        raise BindingError("rules.item_effectiveness.tier0_min 必须是 >=0 的整数")
    if "required" in declared and not isinstance(declared.get("required"), bool):
        raise BindingError("rules.item_effectiveness.required 必须是布尔值")


def _validate_added_damage_conversion_rule(declared: Any) -> None:
    if not isinstance(declared, dict):
        raise BindingError("rules.added_damage_conversion 必须是对象")
    unknown = set(declared) - ADDED_DAMAGE_CONVERSION_RULE_KEYS
    if unknown:
        raise BindingError(f"rules.added_damage_conversion 含未支持的字段：{sorted(unknown)}")
    pool = declared.get("source_pool")
    if not isinstance(pool, dict):
        raise BindingError("rules.added_damage_conversion.source_pool 必须是对象")
    unknown = set(pool) - {"stat", "operation", "tags", "exclusive_tags"}
    if unknown:
        raise BindingError(
            f"rules.added_damage_conversion.source_pool 含未支持的字段：{sorted(unknown)}"
        )
    if pool.get("stat") != "added_damage" or pool.get("operation") != "added":
        raise BindingError("rules.added_damage_conversion.source_pool 只支持 added_damage/added")
    if not isinstance(pool.get("tags"), list):
        raise BindingError("rules.added_damage_conversion.source_pool.tags 必须是数组")
    divisor = declared.get("divisor", 1)
    if isinstance(divisor, bool) or not isinstance(divisor, (int, float)) or float(divisor) <= 0:
        raise BindingError("rules.added_damage_conversion.divisor 必须是正数")
    output = declared.get("output")
    if not isinstance(output, dict):
        raise BindingError("rules.added_damage_conversion.output 必须是对象")
    if output.get("stat") != "added_damage" or output.get("operation") != "added":
        raise BindingError("rules.added_damage_conversion.output 只支持 added_damage/added")
    if not isinstance(output.get("tags"), list):
        raise BindingError("rules.added_damage_conversion.output.tags 必须是数组")
    node = declared.get("node")
    if not isinstance(node, dict):
        raise BindingError("rules.added_damage_conversion.node 必须是对象")
    for key in ("tree", "node"):
        if not isinstance(node.get(key), str) or not node.get(key):
            raise BindingError(f"rules.added_damage_conversion.node 缺少 {key}")
    stat_index = node.get("stat_index", 0)
    if isinstance(stat_index, bool) or not isinstance(stat_index, int) or stat_index < 0:
        raise BindingError("rules.added_damage_conversion.node.stat_index 必须是 >=0 的整数")
    status = str(declared.get("status") or "assumed")
    if status not in ("confirmed", "derived", "assumed", "unresolved"):
        raise BindingError("rules.added_damage_conversion.status 不受支持")


def _validate_row_normalizations(declared: Any) -> None:
    """迁移行归一化：显式、可追溯地保留旧基线口径（如 OQ6 未验证公式/固定条件）。"""
    if not isinstance(declared, list) or not declared:
        raise BindingError("rules.row_normalizations 必须是非空数组")
    ids: set[str] = set()
    for index, rule in enumerate(declared):
        field = f"rules.row_normalizations[{index}]"
        if not isinstance(rule, dict):
            raise BindingError(f"{field} 必须是对象")
        unknown = set(rule) - ROW_NORMALIZATION_KEYS
        if unknown:
            raise BindingError(f"{field} 含未支持的字段：{sorted(unknown)}")
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise BindingError(f"{field}.id 缺失")
        if rule_id in ids:
            raise BindingError(f"{field}.id 重复：{rule_id}")
        ids.add(rule_id)
        match = rule.get("match")
        if not isinstance(match, dict) or not match:
            raise BindingError(f"{field}.match 必须是非空对象")
        unknown = set(match) - ROW_NORMALIZATION_MATCH_KEYS
        if unknown:
            raise BindingError(f"{field}.match 含未支持的字段：{sorted(unknown)}")
        if "contribution_id" in match:
            if not isinstance(match["contribution_id"], str) or not match["contribution_id"]:
                raise BindingError(f"{field}.match.contribution_id 必须是字符串")
        else:
            anchors = ("node_id", "tree", "contribution_id")
            if not any(match.get(key) is not None for key in anchors):
                raise BindingError(
                    f"{field}.match 必须声明稳定锚点（contribution_id 或 node_id/tree）"
                )
        set_values = rule.get("set")
        if not isinstance(set_values, dict) or not set_values:
            raise BindingError(f"{field}.set 必须是非空对象")
        unknown = set(set_values) - ROW_NORMALIZATION_SET_KEYS
        if unknown:
            raise BindingError(f"{field}.set 含未支持的字段：{sorted(unknown)}")
        if "value" in set_values:
            _finite(set_values.get("value"), f"{field}.set.value")
        for key in ("included",):
            if key in set_values and not isinstance(set_values.get(key), bool):
                raise BindingError(f"{field}.set.{key} 必须是布尔值")
        if "condition_code" in set_values and set_values.get("condition_code") is not None and not isinstance(
            set_values.get("condition_code"), str
        ):
            raise BindingError(f"{field}.set.condition_code 必须是字符串或 null")
        if "condition_status" in set_values and not isinstance(set_values.get("condition_status"), str):
            raise BindingError(f"{field}.set.condition_status 必须是字符串")
        status = str(rule.get("status") or "assumed")
        if status not in ("confirmed", "derived", "assumed", "unresolved"):
            raise BindingError(f"{field}.status 不受支持")


DERIVED_EVENTS_RULE_KEYS = frozenset({"kind", "derivation", "evidence", "notes"})


def _validate_derived_events_rule(declared: Any) -> None:
    """``rules.derived_events``：声明 kind + derivation；derivation 由公共纯函数校验。"""
    from .mechanics.frequency import DERIVATION_KINDS, FrequencyError, validate_derivation  # noqa: PLC0415

    if not isinstance(declared, dict):
        raise BindingError("rules.derived_events 必须是对象")
    unknown = set(declared) - DERIVED_EVENTS_RULE_KEYS
    if unknown:
        raise BindingError(f"rules.derived_events 含未知字段：{sorted(unknown)}")
    kind = str(declared.get("kind") or "")
    if kind not in DERIVATION_KINDS:
        raise BindingError(f"rules.derived_events.kind 不受支持：{kind!r}")
    derivation = declared.get("derivation")
    if not isinstance(derivation, dict):
        raise BindingError("rules.derived_events.derivation 必须是对象")
    try:
        validate_derivation(derivation)
    except FrequencyError as exc:
        raise BindingError(f"rules.derived_events.derivation 非法：{exc}") from exc


def validate_plan_v2_rules(rules: Mapping[str, Any]) -> None:
    """plan-v2 规则校验；由定义契约与计划装配共同调用。"""
    if rules.get("stat_transfer") is not None:
        from . import actor_pool  # noqa: PLC0415

        try:
            actor_pool.validate_stat_transfer_rule(rules.get("stat_transfer"))
        except actor_pool.ActorPoolError as exc:
            raise BindingError(f"rules.stat_transfer 非法：{exc}") from exc
    if rules.get("derived_events") is not None:
        _validate_derived_events_rule(rules.get("derived_events"))
    if rules.get("node_attribute_scaled") is not None:
        from . import actor_pool  # noqa: PLC0415

        try:
            actor_pool.validate_node_attribute_scaled_rule(rules.get("node_attribute_scaled"))
        except actor_pool.ActorPoolError as exc:
            raise BindingError(f"rules.node_attribute_scaled 非法：{exc}") from exc
    if rules.get("hit_events") is not None:
        _validate_hit_events_rule(rules.get("hit_events"))
    if rules.get("rotation") is not None:
        _validate_rotation_rule(rules.get("rotation"))
    if rules.get("item_effectiveness") is not None:
        _validate_item_effectiveness_rule(rules.get("item_effectiveness"))
    if rules.get("added_damage_conversion") is not None:
        _validate_added_damage_conversion_rule(rules.get("added_damage_conversion"))
    if rules.get("row_normalizations") is not None:
        _validate_row_normalizations(rules.get("row_normalizations"))


def validate_definition_contract(
    payload: dict[str, Any],
    *,
    allow_instance_fields: bool = True,
    allow_unresolved_sources: bool = False,
) -> None:
    """入口无关校验：组件/effects/rules/state_sources/attribute_conversion/ailments。

    两种适配器在把定义交给计划装配器前都调用本函数；native template 不允许当前实例字段。
    ``definition.plan_version`` 决定组件基数：v1 仍要求 hit+dot，plan-v2 允许 0..N
    （至少一个），新规则（hit_events/rotation/item_effectiveness/
    added_damage_conversion）只在这里做结构校验。
    """
    if not isinstance(payload, dict):
        raise BindingError("definition 必须是对象")
    controls = payload.get("controls") or []
    if not isinstance(controls, list) or not controls:
        raise BindingError("definition.controls 必须是非空数组")
    controls_by_key: dict[str, Any] = {}
    for control in controls:
        key = control.get("key") if isinstance(control, dict) else None
        if not isinstance(key, str) or not key:
            raise BindingError("control 缺少 key")
        if key in controls_by_key:
            raise BindingError(f"control key 重复：{key}")
        controls_by_key[key] = control
    actor = payload.get("actor")
    if actor is not None:
        from . import actor_pool  # noqa: PLC0415

        try:
            actor_pool.validate_actor_declaration(actor)
        except actor_pool.ActorPoolError as exc:
            raise BindingError(f"definition.actor 非法：{exc}") from exc
    frequency = payload.get("frequency")
    if frequency is not None:
        if not isinstance(frequency, dict):
            raise BindingError("definition.frequency 必须是对象")
        unknown = set(frequency) - {"driver", "derivation", "evidence", "notes"}
        if unknown:
            raise BindingError(f"definition.frequency 含未知字段：{sorted(unknown)}")
        if str(frequency.get("driver") or "") != "derived_event_rate":
            raise BindingError(
                f"definition.frequency.driver 不受支持：{frequency.get('driver')!r}"
            )
        derivation = frequency.get("derivation")
        if not isinstance(derivation, dict):
            raise BindingError("definition.frequency.derivation 必须是对象")
        from .mechanics.frequency import FrequencyError, validate_derivation  # noqa: PLC0415

        try:
            validate_derivation(derivation)
        except FrequencyError as exc:
            raise BindingError(f"definition.frequency.derivation 非法：{exc}") from exc
    skill = payload.get("skill") or {}
    components = skill.get("components") or {}
    actor_assembly = False
    plan_version = str(payload.get("plan_version") or "le-calculation-plan-v1")
    if plan_version == "le-calculation-plan-v4":
        assembly_version = str(payload.get("assembly_plan_version") or "")
        if assembly_version not in (
            "le-calculation-plan-v1", "le-calculation-plan-v2", "le-calculation-plan-v3", "v4-actor"
        ):
            raise BindingError("plan-v4 definition 必须声明 assembly_plan_version（v1/v2/v3/v4-actor）")
        if assembly_version == "v4-actor":
            # v4-actor：允许 hit-only（招唤物单次爆炸命中），组件基数不再强制 dot。
            if not isinstance(payload.get("actor"), dict):
                raise BindingError("assembly_plan_version=v4-actor 必须声明 definition.actor")
            components = payload.get("skill", {}).get("components") or {}
            if not isinstance(components, dict) or "hit" not in components:
                raise BindingError("v4-actor 定义必须声明 skill.components.hit")
            for kind, declared in components.items():
                if kind not in ("hit", "dot"):
                    raise BindingError(f"v4-actor 定义 components 含未知组件类型：{kind!r}")
                if not isinstance(declared, dict) or not isinstance(declared.get("tags"), list):
                    raise BindingError(f"v4-actor definition.skill.components.{kind}.tags 必须是数组")
            actor_assembly = True
            assembly_version = "le-calculation-plan-v1"
        else:
            actor_assembly = False
        plan_version = assembly_version
    if plan_version == PLAN_V3_RULE_VERSION:
        validate_plan_v3_rules(payload.get("rules") or {})
    elif plan_version == PLAN_V2_RULE_VERSION:
        if not isinstance(components, dict) or not components:
            raise BindingError("plan-v2 定义必须声明至少一个命中/持续组件")
        if not any(kind in components for kind in ("hit", "dot")):
            raise BindingError("plan-v2 定义 components 必须包含 hit 或 dot")
        for kind, declared in components.items():
            if kind not in ("hit", "dot"):
                raise BindingError(f"plan-v2 定义 components 含未知组件类型：{kind!r}")
            if not isinstance(declared, dict) or not isinstance(declared.get("tags"), list):
                raise BindingError(f"definition.skill.components.{kind}.tags 必须是数组")
    elif plan_version not in ("le-calculation-plan-v1", "le-calculation-plan-v4"):
        raise BindingError(f"不支持的 definition.plan_version：{plan_version!r}")
    else:
        for component_id in ("hit", "dot"):
            if component_id not in components:
                if actor_assembly and component_id == "dot":
                    continue
                raise BindingError(f"definition.skill.components 缺少 {component_id!r} 组件定义")
            if not isinstance((components.get(component_id) or {}).get("tags"), list):
                raise BindingError(f"definition.skill.components.{component_id}.tags 必须是数组")
    if plan_version != PLAN_V3_RULE_VERSION:
        validate_plan_v2_rules(payload.get("rules") or {})
    for effect in payload.get("effects") or []:
        effect_id = effect.get("id")
        source = effect.get("source") or {}
        if not isinstance(source, dict):
            raise BindingError(f"effect {effect_id!r}.source 必须是对象")
        if source.get("kind") not in KNOWN_EFFECT_SOURCE_KINDS:
            raise BindingError(f"effect {effect_id!r} 的来源类型未知：{source.get('kind')!r}")
        if not allow_instance_fields and (
            "item_instance_id" in source or "mod_id" in source
        ):
            raise BindingError(f"effect {effect_id!r}.source 不得携带当前实例字段")
        if effect.get("stat") is None or effect.get("operation") not in ("added", "increased", "more", "base", "flag"):
            raise BindingError(f"effect {effect_id!r} 的 stat/operation 不合法")
        if "value" in effect:
            _finite(effect.get("value"), f"effects.{effect_id}.value")
        if "required" in effect and not isinstance(effect.get("required"), bool):
            raise BindingError(f"effect {effect_id!r} 的 required 必须是布尔值")
        if not (allow_unresolved_sources and source.get("_binding_source_absent")):
            _require_source_anchors(source, field=f"effects.{effect_id}.source")
    rules = payload.get("rules") or {}
    if not isinstance(rules, dict):
        raise BindingError("definition.rules 必须是对象")
    for unknown in set(rules) - KNOWN_RULE_HANDLERS:
        raise BindingError(f"未知规则处理器：{unknown!r}")
    movement = rules.get("movement_conversion") or {}
    if movement:
        if not isinstance(movement, dict):
            raise BindingError("rules.movement_conversion 必须是对象")
        input_stat = movement.get("input_stat")
        output_stat = (movement.get("output") or {}).get("stat")
        if input_stat and output_stat and input_stat == output_stat:
            raise BindingError("movement_conversion 输入与输出属性相同，形成依赖环")
        if "required" in movement and not isinstance(movement.get("required"), bool):
            raise BindingError("rules.movement_conversion.required 必须是布尔值")
        movement_source = movement.get("source") or {}
        if not (allow_unresolved_sources and movement_source.get("_binding_source_absent")):
            _require_source_anchors(movement_source, field="rules.movement_conversion.source")
    every_nth = rules.get("every_nth_hit") or {}
    if every_nth:
        _validate_every_nth_hit(every_nth)
    for control, declared in (payload.get("state_sources") or {}).items():
        if control not in controls_by_key:
            raise BindingError(f"state_sources 引用了未声明的控制项 {control!r}")
        if not isinstance(declared, dict) or declared.get("kind") not in (EQUIPMENT_MOD, UNIQUE_DESCRIPTION):
            raise BindingError(f"state_sources.{control}.kind 不支持")
        if not allow_instance_fields and (
            "item_instance_id" in declared or "mod_id" in declared
        ):
            raise BindingError(f"state_sources.{control} 不得携带当前实例字段")
        _require_source_anchors(declared, field=f"state_sources.{control}")
    conversion = payload.get("attribute_conversion")
    if conversion is not None:
        if not isinstance(conversion, dict) or not conversion:
            raise BindingError("attribute_conversion 必须是非空对象或 null")
        if not isinstance(conversion.get("rules"), str) or not conversion.get("rules"):
            raise BindingError("attribute_conversion 缺少 rules 路径")
        expected = conversion.get("expected")
        if not isinstance(expected, dict) or not expected:
            raise BindingError("attribute_conversion 声明时必须提供非空 expected 身份锚点")
        error = _expected_identity_error(expected)
        if error:
            raise BindingError(f"attribute_conversion.expected 身份锚点无效：{error}")
    for ailment in payload.get("ailments") or []:
        if not isinstance(ailment, dict) or not isinstance(ailment.get("name"), str):
            raise BindingError("ailments 条目缺少 name")


def validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """加载时校验类型/范围/引用/组件 ID/依赖环；非法必需输入直接拒绝。"""
    if not isinstance(profile, dict):
        raise BindingError("profile 必须是对象")
    if profile.get("schema") != "le-dps-profile-v2":
        raise BindingError(
            "旧版或不支持的 dps profile 不能安全转换（缺少显式组件/来源绑定/受限规则声明）："
            f"schema={profile.get('schema')!r}，请迁移到 le-dps-profile-v2，"
            "不要静默补来源、Buff 或规则"
        )
    controls = profile.get("controls") or []
    if not isinstance(controls, list) or not controls:
        raise BindingError("profile.controls 必须是非空数组")
    controls_by_key: dict[str, Any] = {}
    for control in controls:
        key = control.get("key")
        if not isinstance(key, str) or not key:
            raise BindingError("control 缺少 key")
        if key in controls_by_key:
            raise BindingError(f"control key 重复：{key}")
        controls_by_key[key] = control
    profile["controls_by_key"] = controls_by_key
    skill = profile.get("skill") or {}
    components = skill.get("components") or {}
    for component_id in ("hit", "dot"):
        if component_id not in components:
            raise BindingError(f"skill.components 缺少 {component_id!r} 组件定义")
        spec = components[component_id] or {}
        if not isinstance(spec.get("tags"), list):
            raise BindingError(f"skill.components.{component_id}.tags 必须是数组")
        if component_id == "hit":
            adaptive = spec.get("adaptive_damage_type")
            declared_types = skill.get("base_damage") or {}
            if adaptive is not None and adaptive not in declared_types:
                raise BindingError("skill.components.hit.adaptive_damage_type 必须出现在 base_damage 中")
    # effects：来源类型、控制项、数值类型、来源身份/语义锚点、required 标志
    for declared in profile.get("effects") or []:
        effect_id = declared.get("id")
        source = declared.get("source") or {}
        if source.get("kind") not in KNOWN_EFFECT_SOURCE_KINDS:
            raise BindingError(f"effect {effect_id!r} 的来源类型未知：{source.get('kind')!r}")
        if declared.get("stat") is None or declared.get("operation") not in ("added", "increased", "more", "base", "flag"):
            raise BindingError(f"effect {effect_id!r} 的 stat/operation 不合法")
        if "value" in declared:
            _finite(declared.get("value"), f"effects.{effect_id}.value")
        if "required" in declared and not isinstance(declared.get("required"), bool):
            raise BindingError(f"effect {effect_id!r} 的 required 必须是布尔值")
        _require_source_anchors(source, field=f"effects.{effect_id}.source")
    # 规则依赖：输入属性不得等于输出属性（禁止自反馈），属性转换必须在属性缩放之前
    rules = profile.get("rules") or {}
    for unknown in set(rules) - KNOWN_RULE_HANDLERS:
        raise BindingError(f"未知规则处理器：{unknown!r}")
    movement = rules.get("movement_conversion") or {}
    if movement:
        input_stat = movement.get("input_stat")
        output_stat = (movement.get("output") or {}).get("stat")
        if input_stat and output_stat and input_stat == output_stat:
            raise BindingError("movement_conversion 输入与输出属性相同，形成依赖环")
        if "required" in movement and not isinstance(movement.get("required"), bool):
            raise BindingError("rules.movement_conversion.required 必须是布尔值")
        _require_source_anchors(
            movement.get("source") or {}, field="rules.movement_conversion.source"
        )
    every_nth = rules.get("every_nth_hit") or {}
    if every_nth:
        _validate_every_nth_hit(every_nth)
    # 状态来源引用的控制项与身份锚点
    for control, declared in (profile.get("state_sources") or {}).items():
        if control not in controls_by_key:
            raise BindingError(f"state_sources 引用了未声明的控制项 {control!r}")
        if declared.get("kind") not in (EQUIPMENT_MOD, UNIQUE_DESCRIPTION):
            raise BindingError(f"state_sources.{control}.kind 不支持：{declared.get('kind')!r}")
        _require_source_anchors(declared, field=f"state_sources.{control}")
    if "attribute_conversion" in profile and profile.get("attribute_conversion") is not None:
        conversion = profile.get("attribute_conversion")
        if not isinstance(conversion, dict) or not conversion:
            raise BindingError(
                "attribute_conversion 已声明但为空对象；缺少 rules 与 expected 身份锚点"
            )
        if not isinstance(conversion.get("rules"), str) or not conversion.get("rules"):
            raise BindingError("attribute_conversion 缺少 rules 路径")
        identity_error = _expected_identity_error(conversion.get("expected"))
        if identity_error:
            raise BindingError(f"attribute_conversion.expected 身份锚点无效：{identity_error}")
    validate_definition_contract(profile, allow_instance_fields=True)
    return profile


def _require_source_anchors(source: dict[str, Any], *, field: str) -> None:
    """按来源类型强制要求稳定身份锚点与效果语义锚点。"""
    if not isinstance(source, dict):
        raise BindingError(f"{field} 必须是对象")
    kind = source.get("kind")
    if kind == SCENARIO_ASSUMPTION:
        return
    if kind == SKILL_NODE:
        for key in ("tree", "node", "stat_index"):
            if source.get(key) in (None, ""):
                raise BindingError(f"{field}（skill_node）缺少 {key}")
        return
    if kind == EQUIPMENT_MOD:
        if not source.get("item_instance_id") or not source.get("mod_id"):
            raise BindingError(f"{field}（equipment_mod）必须声明 item_instance_id 与 mod_id")
        if not (source.get("expected_definition_entity_id") or source.get("expected_base_entity_id")):
            raise BindingError(
                f"{field}（equipment_mod）缺少稳定定义身份锚点"
                "（expected_definition_entity_id / expected_base_entity_id）"
            )
        semantic_anchors = (
            source.get("expected_property"),
            source.get("expected_special_tag"),
            source.get("expected_tags"),
            source.get("expected_readable_text_contains"),
            source.get("expected_stat_name_contains"),
        )
        if all(anchor in (None, [], "") for anchor in semantic_anchors):
            raise BindingError(f"{field}（equipment_mod）缺少效果语义锚点")
        return
    if kind == UNIQUE_DESCRIPTION:
        if not source.get("item_instance_id"):
            raise BindingError(f"{field}（unique_description）缺少 item_instance_id")
        if source.get("description_index") is None:
            raise BindingError(f"{field}（unique_description）缺少 description_index")
        if not str(source.get("expected_text_contains") or "").strip():
            raise BindingError(f"{field}（unique_description）缺少非空 expected_text_contains")
        if not source.get("expected_definition_entity_id"):
            raise BindingError(
                f"{field}（unique_description）缺少稳定定义身份 expected_definition_entity_id"
            )
        return
    if kind == STATE_SOURCE:
        if not source.get("control"):
            raise BindingError(f"{field}（state_source）缺少 control")
        return
    raise BindingError(f"{field} 的来源类型不受支持：{kind!r}")


__all__ = [
    "BINDING_APPLIED",
    "BINDING_CONDITION_NOT_SATISFIED",
    "BINDING_IDENTITY_MISMATCH",
    "BINDING_NOT_APPLICABLE",
    "BINDING_SEMANTICS_MISMATCH",
    "BINDING_SOURCE_ABSENT",
    "BINDING_STATUSES",
    "BINDING_UNRESOLVED",
    "BINDING_UNVERIFIED",
    "BINDING_VERIFIED",
    "BindingCheck",
    "BindingError",
    "EffectResult",
    "EquipmentFacts",
    "NodeBindingResult",
    "WeaponFact",
    "bind_attribute_conversion",
    "bind_attribute_scaling",
    "bind_movement_conversion",
    "bind_node_value",
    "bind_profile_effects",
    "bind_state_scalings",
    "component_rule_payload",
    "equipment_facts",
    "evaluate_planner_condition",
    "load_json_document",
    "passive_condition_evaluator",
    "validate_profile",
    "verify_mod",
    "verify_skill_constants",
    "verify_unique_description",
]

