"""计算计划编译器：冻结上下文 → ``le-calculation-plan-v1``。

两个入口共享同一计划契约：

* :func:`compile_from_profile_v2`：当前案例 profile-v2 的迁移适配；
* :func:`compile_from_definitions`：原生技能模板（``le-skill-template-v2``）入口。

编译器负责打开/校验输入、解析来源、冻结参数、生成覆盖诊断；**不执行伤害计算**。
计划是纯 JSON：不含 callable、Python 对象、NaN 或机器绝对路径；同一输入必得同一
``plan_sha256``。
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import calculation_plan as planmod
from . import contribution_mapping as mapping
from . import dps_components as components
from . import dps_result
from . import effect_binding as binding
from . import model
from . import plan_handlers
from .plan_model import ResolvedModel
from .catalog import Catalog

COMPILER_VERSION = "1.0.0"
TARGET_KEYS = ("monster_level", "resistance", "high_health", "boss", "blinded", "slowed")
DEFAULT_TARGET = {
    "monster_level": 100,
    "resistance": 0.0,
    "high_health": False,
    "boss": True,
    "blinded": False,
    "slowed": False,
}
TARGET_CONTROL_TYPES = {
    "monster_level": ("integer", 1, 100),
    "resistance": ("number", -1.0, 0.75),
    "high_health": ("boolean", None, None),
    "boss": ("boolean", None, None),
    "blinded": ("boolean", None, None),
    "slowed": ("boolean", None, None),
}
SCHEDULE_WINDOWS = (5, 30, 120)


class PlanCompileError(ValueError):
    """编译被拒绝（输入/绑定/模板不满足已声明契约）。"""


@dataclass
class FrozenContext:
    """编译期冻结上下文；执行器不得再接触 catalog/profile/case 文件。"""

    root: Path
    case: dict[str, Any]
    calc: dict[str, Any]
    dataset: dict[str, Any]
    profile: Optional[dict[str, Any]] = None
    catalog: Any = None
    input_refs: list[dict[str, Any]] = field(default_factory=list)
    synthetic: bool = False
    build_identity: Optional[dict[str, Any]] = None

    def close(self) -> None:
        if self.catalog is not None and hasattr(self.catalog, "close"):
            try:
                self.catalog.close()
            finally:
                self.catalog = None


@dataclass
class CompileResult:
    execution_status: str  # "completed" | "rejected"
    readiness: str  # "ready" | "partial" | "blocked"
    plan: Optional[dict[str, Any]]
    diagnostics: list[dict[str, Any]]

    @property
    def ok(self) -> bool:
        return self.execution_status == "completed" and self.readiness in ("ready", "partial") and self.plan is not None


# ---------------------------------------------------------------------------
# 冻结上下文
# ---------------------------------------------------------------------------


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _content_digest(payload: Any) -> str:
    return planmod.sha256_text(planmod.canonical_json(payload))


def _input_ref(role: str, path: Optional[str], sha256: str) -> dict[str, Any]:
    return {"role": role, "path": path, "sha256": sha256}


def _require_handoff_sha256(value: Optional[str], artifact: str) -> str:
    """交接 override 必须同时给出合法的 artifact sha256。

    缺失、空字符串、非字符串或格式非法（非 64 位小写十六进制）都明确拒绝；
    不允许 ``if expected and ...`` 式的静默跳过——那会让 override 绕过交接校验。
    """
    if value is None:
        raise PlanCompileError(
            f"交接 {artifact} override 缺少 artifact_ref.sha256；"
            "声明 override 时必须同时提供合法 SHA-256"
        )
    if value == "":
        raise PlanCompileError(
            f"交接 {artifact} override 的 artifact_ref.sha256 为空字符串；"
            "必须提供合法 SHA-256"
        )
    if not planmod.is_sha256(value):
        raise PlanCompileError(
            f"交接 {artifact} override 的 artifact_ref.sha256 非法（需 64 位小写十六进制）："
            f"{value!r}"
        )
    return value


def _compiler_source_refs(root: Path) -> list[dict[str, Any]]:
    refs = []
    fingerprint = plan_handlers.implementation_fingerprint()
    files = sorted({**fingerprint["compiler_files"], **fingerprint["executor_files"]})
    for relative in files:
        refs.append(
            _input_ref("compiler_source", relative, planmod.sha256_file(root / relative))
        )
    return refs


def _profile_content(profile: dict[str, Any]) -> dict[str, Any]:
    """用于一致性比较/内容指纹的 profile：剔除内部派生的索引字段。"""
    return {key: value for key, value in profile.items() if key != "controls_by_key"}


def prepare_context(
    case: str | Path | dict[str, Any],
    root: str | Path,
    *,
    profile: Optional[dict[str, Any]] = None,
    calculation_input_override: Optional[str | Path] = None,
    build_spec_override: Optional[str | Path] = None,
    calculation_input_expected_sha256: Optional[str] = None,
    build_spec_expected_sha256: Optional[str] = None,
) -> FrozenContext:
    """加载并校验当前案例输入，冻结为编译上下文；不产生任何写操作。

    ``*_override`` 用于 prepare 生成的新 artifact_ref：此时以实际生成文件为准，
    但只允许在显式交接路径使用，run 仍默认使用 case 冻结输入。
    声明 override 时必须同时给出该 artifact 的合法 sha256（缺失/空串/格式非法一律拒绝）。
    """
    root = Path(root).resolve()
    if isinstance(case, (str, Path)):
        case_path = (root / case).resolve()
        case_doc = json.loads(case_path.read_text(encoding="utf-8"))
        case_doc["_path"] = _relative(root, case_path)
    else:
        case_doc = copy.deepcopy(case)
        case_doc.pop("_root", None)
        case_doc.pop("_dir", None)
        if case_doc.get("_path") is not None:
            case_path = Path(case_doc["_path"])
            if not case_path.is_absolute():
                case_path = root / case_path
            case_doc["_path"] = _relative(root, case_path)
    if calculation_input_override:
        expected_calc_sha = _require_handoff_sha256(
            calculation_input_expected_sha256, "calculation-input"
        )
        calc_path = Path(str(calculation_input_override))
        if not calc_path.is_absolute():
            calc_path = root / calc_path
        actual_calc_sha = planmod.sha256_file(calc_path)
        if actual_calc_sha != expected_calc_sha:
            raise PlanCompileError("交接 calculation-input 的实际 sha256 与 artifact_ref 不一致")
        calc = json.loads(calc_path.read_text(encoding="utf-8"))
    else:
        calc_path = root / case_doc["inputs"]["calculation_input"]
        calc = json.loads(calc_path.read_text(encoding="utf-8"))
        if planmod.sha256_file(calc_path) != case_doc["input_sha256"]["calculation_input"]:
            raise PlanCompileError("计算输入指纹不匹配")
    if build_spec_override:
        expected_build_sha = _require_handoff_sha256(
            build_spec_expected_sha256, "build-spec"
        )
        build_path = Path(str(build_spec_override))
        if not build_path.is_absolute():
            build_path = root / build_path
        actual_build_sha = planmod.sha256_file(build_path)
        if actual_build_sha != expected_build_sha:
            raise PlanCompileError("交接 BuildSpec 的实际 sha256 与 artifact_ref 不一致")
    else:
        build_path = root / case_doc["build"]["build_spec"]
        if planmod.sha256_file(build_path) != case_doc["build"]["build_spec_sha256"]:
            raise PlanCompileError("构筑 revision 指纹不匹配")
    build_document = json.loads(build_path.read_text(encoding="utf-8"))
    build_meta = build_document.get("meta") or {}
    build_revision = build_meta.get("revision") or {}
    build_identity = {
        "build_id": str(build_meta.get("build_id") or ""),
        "revision_id": str(build_revision.get("revision_id") or ""),
        "content_sha256": str(build_revision.get("content_sha256") or ""),
    }
    if not all(build_identity.values()):
        raise PlanCompileError("BuildSpec 缺少 build_id/revision_id/content_sha256，无法核验身份")
    # 交接计算输入不得携带另一构筑/修订的身份；新构筑必须绑定新 BuildSpec revision。
    calc_identity = {
        "build_id": str(calc.get("build_id") or ""),
        "revision_id": str(calc.get("revision_id") or ""),
        "content_sha256": str(calc.get("content_sha256") or ""),
    }
    if any(calc_identity.values()):
        for key in ("build_id", "revision_id", "content_sha256"):
            if calc_identity[key] != build_identity[key]:
                raise PlanCompileError(
                    f"计算输入身份与 BuildSpec 不一致（{key}）："
                    f"calc={calc_identity[key]!r}, build={build_identity[key]!r}"
                )
    elif calculation_input_override:
        raise PlanCompileError("交接 calculation-input 缺少 build_id/revision_id/content_sha256，无法核验构筑身份")
    dataset_path = root / case_doc["dataset"]
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    database_path = root / dataset["database"]
    database_sha = planmod.sha256_file(database_path)
    if not planmod.is_sha256(dataset.get("file_sha256")) or database_sha != dataset["file_sha256"]:
        raise PlanCompileError("dataset 内容 sha256 与 data/current.json 不一致")
    refs: list[dict[str, Any]] = []
    if case_doc.get("_path"):
        refs.append(_input_ref("case", case_doc["_path"], planmod.sha256_file(root / case_doc["_path"])))
    refs.append(_input_ref("calculation_input", _relative(root, calc_path), planmod.sha256_file(calc_path)))
    refs.append(_input_ref("build_spec", _relative(root, build_path), planmod.sha256_file(build_path)))
    refs.append(_input_ref("dataset_pointer", _relative(root, dataset_path), planmod.sha256_file(dataset_path)))
    refs.append(_input_ref("dataset_content", _relative(root, database_path), database_sha))
    profile_doc: Optional[dict[str, Any]] = None
    profile_path = case_doc.get("dps_profile")
    if profile_path:
        absolute = root / profile_path
        if absolute.is_file():
            file_profile = json.loads(absolute.read_text(encoding="utf-8"))
            file_sha = planmod.sha256_file(absolute)
            if profile is None:
                profile_doc = file_profile
            else:
                if planmod.canonical_json(file_profile) != planmod.canonical_json(_profile_content(profile)):
                    raise PlanCompileError(
                        "profile 内容与 case 引用文件不一致；请从文件重新加载或生成新报告"
                    )
                profile_doc = profile
            refs.append(_input_ref("profile", _relative(root, absolute), file_sha))
            refs.append(_input_ref("profile_content", None, _content_digest(_profile_content(profile_doc))))
            conversion = profile_doc.get("attribute_conversion") or {}
            rules_path = conversion.get("rules") if isinstance(conversion, dict) else None
            if isinstance(rules_path, str) and rules_path:
                rules_absolute = root / rules_path
                if not rules_absolute.is_file():
                    raise PlanCompileError(f"attribute_conversion 规则文件不存在：{rules_path}")
                refs.append(
                    _input_ref("attribute_conversion_rules", rules_path, planmod.sha256_file(rules_absolute))
                )
        elif profile is None:
            raise PlanCompileError(f"case.dps_profile 文件不存在：{profile_path}")
        else:
            profile_doc = profile
    elif profile is not None:
        profile_doc = profile
    refs.extend(_compiler_source_refs(root))
    refs.sort(key=lambda ref: (ref["role"], ref.get("path") or "", ref["sha256"]))
    catalog = Catalog(database_path, root=root)
    return FrozenContext(
        root=root,
        case=case_doc,
        calc=calc,
        dataset=dataset,
        profile=profile_doc,
        catalog=catalog,
        input_refs=refs,
        build_identity=build_identity,
    )


# ---------------------------------------------------------------------------
# 公共小工具
# ---------------------------------------------------------------------------


def _slot_item(calc: dict[str, Any], slot: str) -> Optional[dict[str, Any]]:
    return next(
        (item for item in calc.get("items") or [] if str(item.get("instance_id")) == slot),
        None,
    )


def _base_type_id(base_entity_id: Any) -> Optional[int]:
    text = str(base_entity_id or "")
    if ":" not in text:
        return None
    try:
        return int(text.rsplit(":", 1)[-1])
    except ValueError:
        return None


def _display_item_rarity(
    calc: dict[str, Any],
    slot: str,
    *,
    tier0_min: int,
    excluded_base_type_ids: list[int],
) -> tuple[Optional[str], dict[str, Any]]:
    """迁移规则：非 unique 装备在任一 affix tier0 >= 阈值时显示为 Exalted。

    与旧 ``hit_inputs._display_rarity`` 同一判定口径；当前实例的 affix 层级
    从计算输入读取，共享定义只保存阈值/排除列表。
    """
    item = _slot_item(calc, slot)
    if item is None:
        return None, {"slot": slot, "reason": "槽位没有当前物品"}
    if item.get("definition_entity_id"):
        return "unique", {"slot": slot, "reason": "unique 定义不按 affix 层级升级"}
    base_type_id = _base_type_id(item.get("base_entity_id"))
    affix_tiers: list[int] = []
    for mod in item.get("mods") or []:
        if not isinstance(mod, dict) or mod.get("source") != "affix":
            continue
        affix = mod.get("affix") or {}
        tier = affix.get("tier_index")
        if tier is not None:
            affix_tiers.append(int(tier))
    highest = max(affix_tiers) if affix_tiers else None
    if (
        base_type_id is not None
        and base_type_id not in set(int(value) for value in excluded_base_type_ids)
        and highest is not None
        and highest >= int(tier0_min)
    ):
        return "exalted", {
            "slot": slot,
            "base_type_id": base_type_id,
            "highest_affix_tier0": highest,
            "tier0_min": int(tier0_min),
            "excluded_base_type_ids": sorted(int(value) for value in excluded_base_type_ids),
        }
    return str((item.get("rarity") or {}).get("code") or "normal"), {
        "slot": slot,
        "base_type_id": base_type_id,
        "highest_affix_tier0": highest,
        "reason": "未达到 Exalted 显示规则",
    }


def _node_points(calc: dict[str, Any], tree_id: str, node_id: str) -> int:
    for spec in (calc.get("skills") or {}).get("specializations") or []:
        tree = str((spec.get("tree") or {}).get("entity_id") or "")
        if not tree.endswith(":" + str(tree_id)):
            continue
        for node in spec.get("nodes") or []:
            if str(node.get("node_id")) == str(node_id):
                return int(node.get("points") or 0)
    return 0


def _resolve_hit_events_v2(
    calc: dict[str, Any],
    catalog: Catalog,
    profile: dict[str, Any],
    *,
    ability: str,
) -> dict[str, Any]:
    """plan-v2 多命中规则：节点分配真实校验 + 返回命中数与逐击权重。"""
    rule = (profile.get("rules") or {}).get("hit_events") or {}
    base_hits = int(rule.get("base_hits", 1))
    resolved_returns: list[dict[str, Any]] = []
    total_returns = 0
    for source in rule.get("return_sources") or []:
        tree_id = str(source.get("tree") or ability)
        node_id = str(source.get("node") or "")
        require_points = int(source.get("require_points", 1))
        points = _node_points(calc, tree_id, node_id)
        if points < require_points:
            raise PlanCompileError(
                f"多命中来源节点 {tree_id}:{node_id} 需要至少 {require_points} 点，当前 {points} 点"
            )
        returns = int(source.get("returns") or 0)
        total_returns += returns
        resolved_returns.append(
            {
                "tree": tree_id,
                "node": node_id,
                "points": points,
                "require_points": require_points,
                "returns": returns,
                "evidence": list(source.get("evidence") or []),
            }
        )
    hits = base_hits + total_returns
    explicit_weights = rule.get("damage_weights")
    if explicit_weights is not None:
        weights = [float(value) for value in explicit_weights]
        if len(weights) != hits:
            raise PlanCompileError("rules.hit_events.damage_weights 长度与命中数不一致")
    else:
        multiplier = float(rule.get("extra_hit_damage_multiplier", 0.5))
        weights = [1.0] + [multiplier] * (hits - 1)
    return {
        "base_hits": base_hits,
        "hits_per_cast": hits,
        "damage_weights": weights,
        "effective_hits_per_cast": sum(weights),
        "return_sources": resolved_returns,
        "extra_hit_damage_multiplier": (
            None if explicit_weights is not None else float(rule.get("extra_hit_damage_multiplier", 0.5))
        ),
        "evidence": list(rule.get("evidence") or []),
    }


def _resolve_rotation_v2(
    calc: dict[str, Any],
    catalog: Catalog,
    profile: dict[str, Any],
    *,
    ability: str,
) -> dict[str, Any]:
    """plan-v2 周期平均因子：消费加成从真实节点读数，循环长度是显式假设。"""
    rule = (profile.get("rules") or {}).get("rotation") or {}
    rhythm = rule.get("rhythm_cycle")
    factor = 1.0
    rhythm_payload: Optional[dict[str, Any]] = None
    if rhythm:
        consume = rhythm.get("consume_more") or {}
        tree_id = str(consume.get("tree") or ability)
        node_id = str(consume.get("node") or "")
        stat_index = int(consume.get("stat_index") or 0)
        value, points, check = binding.bind_node_value(
            calc, catalog, tree_id=tree_id, node_id=node_id, stat_index=stat_index
        )
        if not check.ok:
            raise PlanCompileError(
                f"周期平均来源节点 {tree_id}:{node_id}[{stat_index}] 绑定失败：{check.detail}"
            )
        cycle_casts = int(rhythm.get("cycle_casts") or 0)
        factor = 1.0 + float(value or 0.0) / cycle_casts
        rhythm_payload = {
            "cycle_casts": cycle_casts,
            "consume_more": {
                "tree": tree_id,
                "node": node_id,
                "stat_index": stat_index,
                "value": float(value or 0.0),
                "points": points,
            },
            "status": str(rhythm.get("status") or "assumed"),
            "label": rhythm.get("label"),
            "notes": rhythm.get("notes"),
            "evidence": list(rhythm.get("evidence") or []),
            "average_more_factor": factor,
            "binding_check": check.to_dict(),
        }
    policies = list(rule.get("policies") or ["cooldown_limited"])
    primary = str(rule.get("primary_policy") or policies[0])
    schedule_policies = []
    for policy_id in policies:
        schedule_policies.append(
            {
                "policy_id": policy_id,
                "binding_constraint": "cooldown" if policy_id == "cooldown_limited" else "attack_speed",
                "interval_policy": (
                    "max(action_time, cooldown)"
                    if policy_id == "cooldown_limited"
                    else "action_time"
                ),
            }
        )
    return {
        "factor": factor,
        "rhythm_cycle": rhythm_payload,
        "policies": schedule_policies,
        "primary_policy": primary,
        "windows_s": list(rule.get("windows_s") or SCHEDULE_WINDOWS),
        "window_semantics": str(rule.get("window_semantics") or "expected_casts"),
        "status": str(
            (rhythm_payload or {}).get("status") or "confirmed"
        ),
        "evidence": list(rule.get("evidence") or []),
        "notes": rule.get("notes"),
    }


def _component_entries_v2(
    profile: dict[str, Any], hit_events: dict[str, Any]
) -> list[dict[str, Any]]:
    """plan-v2 组件条目：多命中计数只在 hit 组件上声明（0..N dot 不含命中数）。"""
    declared = (profile.get("skill") or {}).get("components") or {}
    entries: list[dict[str, Any]] = []
    hit_count = 0
    for kind in ("hit", "dot"):
        spec = declared.get(kind)
        if not isinstance(spec, dict):
            continue
        entry: dict[str, Any] = {
            "component_id": str(spec.get("component_id") or f"{kind}:declared"),
            "kind": kind,
            "tags": list(spec.get("tags") or []),
            "crit_capable": bool(spec.get("crit_capable")),
            "adaptive_damage_type": spec.get("adaptive_damage_type"),
            "damage_type": spec.get("damage_type"),
        }
        if kind == "hit":
            hit_count += 1
            if hit_count > 1:
                raise PlanCompileError("plan-v2 rules.hit_events 只支持一个命中组件")
            entry["hits_per_cast"] = int(hit_events["hits_per_cast"])
            entry["damage_weights"] = list(hit_events["damage_weights"])
        else:
            entry.pop("damage_type", None)
        entries.append(entry)
    if not entries:
        raise PlanCompileError("plan-v2 定义必须声明至少一个组件")
    return entries


def _fake_scenario(profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    states: dict[str, Any] = {}
    for control in profile.get("controls") or []:
        key = control["key"]
        if control.get("type") == "boolean":
            states[key] = True
        else:
            low = int(control.get("min") or 0)
            high = int(control.get("max") or 1)
            states[key] = max(1, low) if high >= 1 else 0
    target = dict(DEFAULT_TARGET)
    target.update({"high_health": True, "boss": True, "blinded": True, "slowed": True})
    return states, target


def _control_entries(profile: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for control in profile.get("controls") or []:
        entries.append(
            {
                "control_id": control["key"],
                "namespace": "state",
                "key": control["key"],
                "label": control.get("label"),
                "source": control.get("source"),
                "type": control["type"],
                "default": control["default"],
                "min": control.get("min"),
                "max": control.get("max"),
                "readonly": bool(control.get("readonly")),
                "effect": control.get("effect"),
                "requires_target": control.get("requires_target"),
                "single_cast_only": bool(control.get("single_cast_only")),
            }
        )
    for key in TARGET_KEYS:
        control_type, minimum, maximum = TARGET_CONTROL_TYPES[key]
        entries.append(
            {
                "control_id": f"target.{key}",
                "namespace": "target",
                "key": key,
                "label": key,
                "type": control_type,
                "default": DEFAULT_TARGET[key],
                "min": minimum,
                "max": maximum,
                "readonly": False,
            }
        )
    return entries


def _source_descriptor(row: model.Contribution) -> Optional[dict[str, Any]]:
    raw = row.raw or {}
    if raw.get("mod_id"):
        return {"kind": "item_mod", "mod_id": str(raw.get("mod_id"))}
    if raw.get("tree") is not None and raw.get("node") is not None:
        return {
            "kind": "skill_node",
            "tree": str(raw.get("tree")),
            "node": str(raw.get("node")),
            "stat_index": raw.get("stat_index"),
        }
    if raw.get("node_id") is not None and raw.get("index") is not None:
        return {
            "kind": "passive_node",
            "node_id": str(raw.get("node_id")),
            "index": raw.get("index"),
        }
    return None


def _effect_source_id(row: model.Contribution, meta: dict[str, Any]) -> str:
    """每个 effect 的稳定 source_id（与 sources 列表一致）。"""
    owner = _source_descriptor(row)
    if owner:
        kind = owner["kind"]
        if kind == "item_mod":
            return f"item_mod:{owner['mod_id']}"
        if kind == "skill_node":
            return f"skill_node:{owner['tree']}:{owner['node']}:{owner['stat_index']}"
        if kind == "passive_node":
            return f"passive_node:{owner['node_id']}:{owner['index']}"
    source_facts = meta.get("source_facts") or {}
    if source_facts:
        rule_id = meta.get("rule_id") or row.contribution_id
        return f"effect:{rule_id}"
    kind = str(row.source_kind or "derived")
    return f"{kind}:{row.source_path}"


def _ownership_payload(row: model.Contribution) -> dict[str, Any]:
    """计划级所有权键：来源实例 + 机制/规则 + 输出通道；用于重复注入与冲突校验。

    ``condition_code`` 是机制定义的一部分：同来源/同输出但条件程序不同的两条
    词条（例如“基础值”与“300 法力时翻倍”两个独立子句）是不同效果，必须分别
    保留，不能在装配阶段被当成重复/冲突。
    """
    owner = _source_descriptor(row)
    source = dict(owner) if owner else {
        "kind": str(row.source_kind or "derived"),
        "path": row.source_path,
    }
    return {
        "source": source,
        "condition": row.condition_code,
        "stat_key": row.stat_key,
        "operation": row.operation,
        "damage_tags": sorted(str(tag) for tag in row.damage_tags or []),
        "target": row.target,
        "group": row.group,
    }


def _definition_signature(row: model.Contribution, meta: dict[str, Any]) -> str:
    """同所有权下的完整定义签名：数值/单位/条件程序/值模式都必须一致。"""
    return planmod.canonical_json(
        {
            "value": float(row.value),
            "unit": row.unit,
            "status": row.status,
            "included": bool(row.included),
            "condition_code": row.condition_code,
            "condition_status": row.condition_status,
            "exclusion_reason": row.exclusion_reason,
            "notes": row.notes,
            "value_source": row.value_source,
            "condition_ref": meta.get("condition_ref"),
            "condition_style": meta.get("condition_style"),
            "value_mode": meta.get("value_mode"),
            "value_params": meta.get("value_params") or {},
        }
    )


def _row_semantic_signature(row: model.Contribution) -> str:
    """同所有权下判断“同语义重复”与“不同定义冲突”的签名。"""
    return planmod.canonical_json(
        {
            "value": float(row.value),
            "unit": row.unit,
            "status": row.status,
            "included": bool(row.included),
            "condition_code": row.condition_code,
            "condition_status": row.condition_status,
            "exclusion_reason": row.exclusion_reason,
            "notes": row.notes,
            "value_source": row.value_source,
        }
    )


def _effect_semantic_signature(effect: dict[str, Any]) -> str:
    return planmod.canonical_json(
        {
            "value": float(effect.get("value") or 0.0),
            "unit": effect.get("unit"),
            "status": effect.get("status"),
            "included_static": bool(effect.get("included_static")),
            "condition_ref": effect.get("condition_ref"),
            "condition_code": effect.get("condition_code"),
            "condition_status_static": effect.get("condition_status_static"),
            "exclusion_reason": effect.get("exclusion_reason"),
            "value_mode": effect.get("value_mode"),
            "value_params": effect.get("value_params") or {},
        }
    )


def _serialize_effect(index: int, row: model.Contribution, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "effect_id": row.contribution_id,
        "sequence": index,
        "label": row.source_label,
        "category": meta["category"],
        "stage": meta["stage"],
        "handler_id": meta["handler_id"],
        "handler_version": plan_handlers.VERSION,
        "source_kind": row.source_kind,
        "source_label": row.source_label,
        "source_path": row.source_path,
        "stat_key": row.stat_key,
        "stat_name": row.stat_name,
        "operation": row.operation,
        "value": float(row.value),
        "unit": row.unit,
        "damage_tags": list(row.damage_tags or []),
        "target": row.target,
        "group": row.group,
        "value_source": row.value_source,
        "status": row.status,
        "notes": row.notes,
        "included_static": bool(row.included),
        "condition_status_static": str(meta.get("condition_status_static") or row.condition_status),
        "exclusion_reason": meta.get("exclusion_reason", row.exclusion_reason),
        "condition_ref": meta.get("condition_ref"),
        "condition_code": meta.get("condition_code"),
        "condition_code_static": bool(meta.get("condition_code_static")),
        "coverage_class": meta.get("coverage_class"),
        "coverage_status": meta.get("coverage_status"),
        "required": bool(meta.get("required", True)),
        "emits_contribution": bool(meta.get("emits_contribution", True)),
        "value_mode": meta.get("value_mode", "static"),
        "value_params": meta.get("value_params") or {},
        "ledger_kind": meta.get("ledger_kind"),
        "ledger": meta.get("ledger"),
        "rule_id": meta.get("rule_id"),
        "condition_style": meta.get("condition_style"),
        "owner": _source_descriptor(row),
        "scope": (row.raw or {}).get("scope") if isinstance(row.raw, dict) else None,
        "actor_id": (row.raw or {}).get("actor_id") if isinstance(row.raw, dict) else None,
        "source_facts": meta.get("source_facts") or {},
        "raw": row.raw,
        "evidence": [
            item.to_dict() if hasattr(item, "to_dict") else dict(item)
            for item in (row.evidence or [])
        ] + list(meta.get("merged_evidence") or []),
    }


# ---------------------------------------------------------------------------
# profile-v2 编译
# ---------------------------------------------------------------------------


def _node_ledger_entry(rule_id: str, tree_id: str, node_id: str, stat_index: int, points: int, status: str, value: Optional[float], reason: Optional[str], contribution_id: Optional[str], facts: dict[str, Any]) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "tree_id": tree_id,
        "node_id": node_id,
        "stat_index": stat_index,
        "points": points,
        "status": status,
        "value": value,
        "reason": reason,
        "contribution_id": contribution_id,
        "facts": facts,
    }


#: effect ``per_attribute`` 缩放：绑定阶段只保留声明，属性池就绪后由本函数换算，
#: 使序列化 effects 与 execution_v1 都拿到最终值（运行期不再回读属性或规则）。
def _apply_per_attribute_scaling(
    result: Any,
    attributes: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """把已绑定的 ``per_attribute`` 声明换算为最终值并写入审计事实。

    语义：final = 绑定后的 value × attributes[attribute] / per。
    返回审计事实 dict；effect 未声明 ``per_attribute`` 时返回 ``None``。
    属性值缺失或不为正数时不静默按 0 折算：本应生效的 effect 转为 unresolved
    （不产出贡献，覆盖缺口带明确 reason），本来就不生效的保留原状态并记录原因。
    """
    declared = (result.payload or {}).get("per_attribute")
    if not declared:
        return None
    attribute = str(declared.get("attribute") or "")
    per = float(declared.get("per") or 0.0)
    contribution = result.contribution
    facts: dict[str, Any] = {
        "attribute": attribute,
        "per": per,
        "declared_value": float(contribution.value) if contribution is not None else None,
        "attribute_value": None,
        "scaled_value": None,
        "status": result.status,
        "reason": None,
    }
    if contribution is None:
        result.payload["per_attribute"] = dict(facts)
        return facts
    raw_attribute = attributes.get(attribute)
    attribute_value: Optional[float]
    try:
        attribute_value = float(raw_attribute) if raw_attribute is not None else None
    except (TypeError, ValueError):
        attribute_value = None
    reason: Optional[str] = None
    if not math.isfinite(per) or per <= 0:
        reason = f"per_attribute.per 非法（{per!r}），不能折算"
    elif attribute_value is None or not math.isfinite(attribute_value) or attribute_value <= 0:
        reason = (
            f"per_attribute 缩放属性 {attribute!r} 在当前输入中缺失或不为正数，"
            "不能静默按 0 折算"
        )
    if reason is not None:
        facts["status"] = binding.BINDING_UNRESOLVED
        facts["reason"] = reason
        result.payload["per_attribute"] = dict(facts)
        contribution.raw = {**(contribution.raw or {}), "per_attribute": dict(facts)}
        if bool(contribution.included):
            # 本应生效：不产出贡献，转为 unresolved（由覆盖缺口记录明确 reason）。
            result.contribution = None
            result.status = binding.BINDING_UNRESOLVED
            result.reason = reason
        else:
            contribution.exclusion_reason = reason
            contribution.notes = (
                f"{contribution.notes}；{reason}" if contribution.notes else reason
            )
        return facts
    scaled = float(contribution.value) * float(attribute_value) / per
    facts["attribute_value"] = float(attribute_value)
    facts["scaled_value"] = scaled
    facts["status"] = binding.BINDING_APPLIED
    result.payload["per_attribute"] = dict(facts)
    contribution.raw = {**(contribution.raw or {}), "per_attribute": dict(facts)}
    note = f"按 {attribute} 缩放（每 {per:g} 点，当前 {attribute_value:g}）"
    contribution.notes = f"{contribution.notes}；{note}" if contribution.notes else note
    contribution.value = scaled
    return facts


def compile_from_resolved_model(
    context: FrozenContext, resolved_model: "ResolvedModel"
) -> CompileResult:
    """共享装配入口：只接受独立 ResolvedModel（共享定义+实例+场景）。

    ``definition.plan_version`` 决定产物契约：缺省仍为 plan-v1；只有显式声明
    ``le-calculation-plan-v2`` 时才启用多命中/具名频率策略/具名评估，旧 v1
    语义不会被静默放宽。
    """
    resolved_model.validate()
    profile = resolved_model.assembler_view()
    plan_schema = str(profile.get("plan_version") or planmod.SCHEMA)
    if plan_schema not in (planmod.SCHEMA, planmod.SCHEMA_V2, planmod.SCHEMA_V3, "le-calculation-plan-v4"):
        raise PlanCompileError(f"不支持的 plan_version：{plan_schema!r}")
    if plan_schema == planmod.SCHEMA_V3:
        from . import plan_compiler_v3  # noqa: PLC0415 - v3 装配器独立模块

        return plan_compiler_v3.compile_v3(context, resolved_model)
    if plan_schema == "le-calculation-plan-v4":
        from . import plan_v4  # noqa: PLC0415

        return plan_v4.compile_from_resolved_model(context, resolved_model)
    is_v2 = plan_schema == planmod.SCHEMA_V2
    rules_declared = profile.get("rules") or {}
    try:
        binding.validate_plan_v2_rules(rules_declared)
    except binding.BindingError as exc:
        raise PlanCompileError(str(exc)) from exc
    if is_v2 and not rules_declared.get("hit_events"):
        raise PlanCompileError("plan-v2 定义缺少 rules.hit_events（多命中计数来源）")
    if is_v2 and not rules_declared.get("rotation"):
        raise PlanCompileError("plan-v2 定义缺少 rules.rotation（频率策略与周期因子）")
    calc = context.calc
    catalog = context.catalog
    case = context.case
    ability = str(case.get("main_skill", {}).get("ability_id") or profile.get("ability_id") or "")
    if ability != profile.get("ability_id"):
        raise PlanCompileError("技能绑定不匹配")
    spec = next(
        (row for row in (calc.get("skills") or {}).get("specializations") or []
         if str((row.get("tree") or {}).get("entity_id", "")).endswith(":" + ability)),
        None,
    )
    if spec is None:
        raise PlanCompileError(f"计算输入没有主技能专精：{ability}")
    skill_name = str((spec.get("owner_skill") or {}).get("name") or "")
    actor_declared = profile.get("actor") if isinstance(profile.get("actor"), dict) else None
    damage_source_entity = (
        str((actor_declared.get("damage_source") or {}).get("ability_entity") or "")
        if actor_declared
        else ""
    )
    skill_constants = binding.verify_skill_constants(
        profile,
        catalog,
        ability_id=ability,
        skill_name=skill_name,
        damage_source_entity_id=damage_source_entity or None,
    )
    ailment_constants = binding.verify_ailment_constants(profile, catalog)
    if skill_constants.get("status") not in (None, binding.BINDING_VERIFIED):
        raise PlanCompileError(str(skill_constants.get("reason") or "技能常量与目录不一致"))
    if ailment_constants.get("status") not in (None, binding.BINDING_VERIFIED):
        raise PlanCompileError(str(ailment_constants.get("reason") or "异常常量与 dataset 不一致"))
    facts = binding.equipment_facts(calc, catalog)
    class_entity_id = ((calc.get("character") or {}).get("class") or {}).get("entity_id") or ""
    class_base = mapping.class_base(catalog, class_entity_id)
    fake_states, fake_target = _fake_scenario(profile)
    fake_values = {**fake_states, **fake_target}
    rows: list[model.Contribution] = []
    metas: dict[int, dict[str, Any]] = {}
    pseudo_effects: list[dict[str, Any]] = []
    ledger: dict[str, list[dict[str, Any]]] = {
        "node_bindings": [],
        "effects": [],
        "state_scalings": [],
        "movement_conversion": [],
        "attribute_scaling": [],
        "attribute_conversion": [],
        "cooldown_reduction": [],
        "ailment_chance_sources": [],
        "equipment_condition_effects": [],
    }
    duplicates: list[dict[str, Any]] = []
    owned: dict[str, str] = {}
    per_attribute_problems: list[str] = []
    coverage_effect_statuses: list[dict[str, Any]] = []
    conditions: list[dict[str, Any]] = []
    condition_index: dict[str, str] = {}

    def register_condition(op: str, terms: list[dict[str, Any]]) -> Optional[str]:
        if not terms:
            return None
        payload = {"op": op, "terms": terms}
        digest = planmod.sha256_text(planmod.canonical_json(payload))[:16]
        condition_id = f"cond:{digest}"
        if condition_id not in condition_index:
            conditions.append({"condition_id": condition_id, **payload})
            condition_index[condition_id] = condition_id
        return condition_id

    ownership_rows: dict[str, model.Contribution] = {}
    conflicts: list[dict[str, Any]] = []

    def append(row: model.Contribution, meta: dict[str, Any]) -> Optional[str]:
        """同所有权：同语义合并（来源可合并），不同定义阻断编译。

        返回 ``None`` 表示已追加；返回已有 contribution_id 表示同语义重复已合并；
        不同定义记录 ``ownership_conflict`` 并阻止编译。
        """
        ownership_key = planmod.canonical_json(_ownership_payload(row))
        existing = ownership_rows.get(ownership_key)
        if existing is not None:
            existing_definition = _definition_signature(existing, metas.get(id(existing)) or {})
            new_definition = _definition_signature(row, meta)
            if existing_definition != new_definition:
                conflicts.append(
                    {
                        "effect_id": row.contribution_id,
                        "existing_effect_id": existing.contribution_id,
                        "existing_value": float(existing.value),
                        "new_value": float(row.value),
                        "existing_condition": existing.condition_code,
                        "new_condition": row.condition_code,
                        "source": row.source_label,
                    }
                )
                return occupied_return(row, existing)
            duplicates.append(
                {
                    "effect_id": row.contribution_id,
                    "kind": str(row.source_kind or "effect"),
                    "duplicate_of": existing.contribution_id,
                    "disposition": "merged",
                }
            )
            existing_meta = metas.get(id(existing)) or {}
            source_refs = existing_meta.setdefault("merged_source_refs", [])
            merged_source_id = _effect_source_id(row, meta)
            if merged_source_id not in source_refs:
                source_refs.append(merged_source_id)
            merged_evidence = existing_meta.setdefault("merged_evidence", [])
            for item in row.evidence or []:
                payload = item.to_dict() if hasattr(item, "to_dict") else dict(item)
                if payload not in merged_evidence:
                    merged_evidence.append(payload)
            return existing.contribution_id
        ownership_rows[ownership_key] = row
        rows.append(row)
        metas[id(row)] = meta
        return None

    def occupied_return(row: model.Contribution, existing: model.Contribution) -> str:
        return existing.contribution_id

    def static_meta(category: str, stage: str, handler_id: str, *, ledger_kind: Optional[str] = None, ledger_payload: Optional[dict[str, Any]] = None, coverage_status: str = "mapped") -> dict[str, Any]:
        return {
            "category": category,
            "stage": stage,
            "handler_id": handler_id,
            "included_static": True,
            "value_mode": "static",
            "ledger_kind": ledger_kind,
            "ledger": ledger_payload,
            "coverage_status": coverage_status,
        }

    for row in mapping.base_contributions(catalog, calc, calc["dataset_id"]):
        append(row, {
            "category": "base",
            "stage": "static",
            "handler_id": "base_contribution-v1",
            "included_static": True,
            "value_mode": "static",
            "coverage_status": "mapped",
        })
    mods, _descriptions = mapping.mod_contributions(
        calc,
        dataset_id=calc["dataset_id"],
        states=fake_values,
        calc_input_label=case["inputs"]["calculation_input"],
        map_mod_fn=mapping.map_mod,
    )
    for row in mods:
        raw = row.raw or {}
        append(row, {
            "category": "mod",
            "stage": "static",
            "handler_id": "generic_mod_mapping-v1",
            "included_static": bool(row.included),
            "condition_status_static": row.condition_status,
            "condition_code": row.condition_code,
            "value_mode": "static",
            "coverage_class": raw.get("coverage_class"),
            "coverage_status": "mapped",
        })
    passive_rows, deferred = mapping.passive_contributions(
        catalog,
        calc,
        dataset_id=calc["dataset_id"],
        condition_evaluator=binding.passive_condition_evaluator(facts),
    )
    for row in passive_rows:
        append(row, {
            "category": "passive",
            "stage": "static",
            "handler_id": "passive_mapping-v1",
            "included_static": bool(row.included),
            "condition_status_static": row.condition_status,
            "condition_code": row.condition_code,
            "condition_code_static": bool(row.condition_code),
            "value_mode": "static",
            "coverage_class": (row.raw or {}).get("coverage_class"),
            "coverage_status": "mapped",
        })
    # -- 装备数量条件 effect（早期阶段，必须参与属性汇总） ------------------
    # 语义：当当前输入满足“满足某 channel 的装备实例数（可排除角色）>= min_items”时，
    # 产出 owner 行（如 all_attributes added +8），从而 Dex/属性汇总/attribute_scaling/
    # per_attribute/actor per-Dex 全部按新 Dex 计算；不满足时只记 not_applicable 账本，
    # 不产出 0 值贡献。规则来源身份用 verify_mod 唯一校验，未知结构在 validate 阶段拒绝。
    for rule in binding.normalize_equipment_condition_effects(
        rules_declared.get("equipment_condition_effects")
    ):
        rule_id = str(rule["id"])
        rule_label = str(rule["label"])
        try:
            source_check = binding.resolve_equipment_condition_source(calc, rule["source"])
        except binding.BindingError as exc:
            raise PlanCompileError(
                f"equipment_condition_effects[{rule_id}] 来源绑定失败：{exc}"
            ) from exc
        condition = binding.count_equipment_condition_items(calc, rule["requires"])
        required = bool(rule.get("required", True))
        status = binding.BINDING_APPLIED
        reason: Optional[str] = None
        if not source_check.ok:
            if required:
                raise PlanCompileError(
                    f"equipment_condition_effects[{rule_id}] 来源不可用：{source_check.detail}"
                )
            status = source_check.status
            reason = source_check.detail
        elif not condition["satisfied"]:
            status = binding.BINDING_NOT_APPLICABLE
            reason = (
                f"条件未满足：{condition['count']} < min_items {condition['min_items']}"
                f"（channel={condition['channel']}, exclude_roles={condition['exclude_roles']}）"
            )
        contribution = None
        if status == binding.BINDING_APPLIED:
            evidence_rows = [
                model.Evidence(
                    kind=str(item.get("kind") or "user_rule"),
                    detail=str(item.get("detail") or ""),
                    locator=item.get("locator"),
                )
                for item in rule.get("evidence") or []
            ]
            contribution = mapping.make_contribution(
                contribution_id=f"rule:equipment_condition:{rule_id}",
                source_kind=str(rule.get("source_kind") or "equipment"),
                source_label=rule_label,
                source_path=str(rule.get("source_path") or "rules.equipment_condition_effects"),
                stat_name=str(rule["stat"]),
                stat_key=str(rule["stat"]),
                operation=str(rule["operation"]),
                value=float(rule["value"]),
                unit=str(rule.get("unit") or "flat"),
                damage_tags=list(rule.get("tags") or []),
                group=f"equipment_condition:{rule_id}",
                included=True,
                value_source="derived",
                status=str(rule.get("status") or model.STATUS_DERIVED),
                evidence_rows=evidence_rows,
                notes=rule.get("notes"),
                raw={
                    "rule_id": rule_id,
                    "mod_id": source_check.facts.get("mod_id"),
                    "item_instance_id": source_check.facts.get("item_instance_id"),
                    "source": source_check.to_dict(),
                    "requires": copy.deepcopy(rule["requires"]),
                    "condition": copy.deepcopy(condition),
                },
            )
            append(
                contribution,
                {
                    "category": "base",
                    "stage": "baseline",
                    "handler_id": "base_contribution-v1",
                    "included_static": True,
                    "value_mode": "static",
                    "coverage_status": "mapped",
                    "ledger_kind": "equipment_condition_effects",
                },
            )
        ledger_row = {
            "rule_id": rule_id,
            "label": rule_label,
            "status": status,
            "reason": reason,
            "required": required,
            "contribution_id": contribution.contribution_id if contribution else None,
            "stat": rule["stat"],
            "operation": rule["operation"],
            "value": float(rule["value"]),
            "unit": rule.get("unit"),
            "condition": condition,
            "source": source_check.to_dict(),
        }
        ledger["equipment_condition_effects"].append(ledger_row)
        coverage_effect_statuses.append(
            {
                "effect_id": f"rule:equipment_condition:{rule_id}",
                "label": rule_label,
                "status": status,
                "reason": reason,
            }
        )
        if contribution is None:
            pseudo_effects.append(
                {
                    "effect_id": f"rule:equipment_condition:{rule_id}",
                    "ledger": ledger_row,
                    "coverage_status": status,
                    "required": required,
                    "included_static": False,
                    "pseudo_row": {
                        "effect_id": f"rule:equipment_condition:{rule_id}",
                        "label": rule_label,
                        "stat_key": str(rule["stat"]),
                        "operation": str(rule["operation"]),
                        "unit": str(rule.get("unit") or "flat"),
                        "damage_tags": list(rule.get("tags") or []),
                        "target": None,
                        "group": f"equipment_condition:{rule_id}",
                    },
                }
            )

    attribute_info = mapping.attribute_detail(rows, class_base)
    attributes = attribute_info["attributes"]
    for row, stat in deferred:
        _row, problem = binding.resolve_per_attribute(row, stat, attributes)
        if problem and (row.raw or {}).get("coverage_class") in mapping.COVERAGE_GAP_CLASSES:
            per_attribute_problems.append(f"{row.contribution_id}：{problem}")
        append(row, {
            "category": "passive",
            "stage": "static",
            "handler_id": "passive_mapping-v1",
            "included_static": bool(row.included),
            "condition_status_static": row.condition_status,
            "condition_code": row.condition_code,
            "condition_code_static": bool(row.condition_code),
            "value_mode": "static",
            "coverage_class": (row.raw or {}).get("coverage_class"),
            "coverage_status": "mapped",
        })

    # -- 物品效率规则（plan-v2；FR offhand Exalted 迁移） --------------------
    item_effectiveness_result: Optional[dict[str, Any]] = None
    if is_v2 and rules_declared.get("item_effectiveness"):
        rule = rules_declared["item_effectiveness"]
        source = rule.get("source") or {}
        check = binding.verify_mod(
            calc,
            mod_id=str(source.get("mod_id") or ""),
            item_instance_id=source.get("item_instance_id"),
            expected_property=source.get("expected_property"),
            expected_special_tag=source.get("expected_special_tag"),
            expected_tags=source.get("expected_tags"),
            expected_definition_entity_id=source.get("expected_definition_entity_id"),
            expected_base_entity_id=source.get("expected_base_entity_id"),
            expected_readable_text_contains=source.get("expected_readable_text_contains"),
            expected_stat_name_contains=source.get("expected_stat_name_contains"),
        )
        factor = float((check.facts or {}).get("value_numeric") or 0.0) if check.ok else 0.0
        required = bool(rule.get("required", True))
        target_slot = str(rule.get("target_slot") or "")
        rarity, rarity_facts = _display_item_rarity(
            calc,
            target_slot,
            tier0_min=int(rule.get("tier0_min", 5)),
            excluded_base_type_ids=list(rule.get("excluded_base_type_ids") or []),
        )
        expected_rarity = str(rule.get("require_display_rarity") or "exalted")
        rarity_ok = rarity == expected_rarity
        status: str
        reason: Optional[str] = None
        if not check.ok:
            status = check.status
            reason = check.detail
        elif factor <= 0:
            status = binding.BINDING_UNRESOLVED
            reason = "物品效率来源没有正数值"
        elif not rarity_ok:
            status = binding.BINDING_NOT_APPLICABLE if not required else binding.BINDING_CONDITION_NOT_SATISFIED
            reason = f"目标槽位显示稀有度 {rarity!r}，不满足 {expected_rarity!r}"
        else:
            status = binding.BINDING_APPLIED
        multiplier = 1.0 + factor
        scaled: list[str] = []
        if status == binding.BINDING_APPLIED:
            for row in rows:
                raw = row.raw or {}
                if str(raw.get("item_instance_id") or "") != target_slot:
                    continue
                row.value = float(row.value) * multiplier
                row.value_source = "derived_item_effectiveness"
                row.notes = (
                    (row.notes or "")
                    + f" [迁移规则: {rule.get('label') or 'item_effectiveness'} ×{multiplier:g} on {target_slot}]"
                ).strip()
                scaled.append(row.contribution_id)
        item_effectiveness_result = {
            "rule": "item_effectiveness",
            "label": rule.get("label") or "item_effectiveness",
            "status": status,
            "reason": reason,
            "source": source,
            "factor": factor,
            "multiplier": multiplier,
            "target_slot": target_slot,
            "display_rarity": rarity,
            "rarity_facts": rarity_facts,
            "scaled_contribution_ids": scaled,
        }
        ledger["effects"].append(
            {
                "effect_id": "rule:item_effectiveness",
                "label": item_effectiveness_result["label"],
                "status": status,
                "reason": reason,
                "contribution_id": None,
                "source": {"rule": "item_effectiveness", "source": source, "target_slot": target_slot},
                "payload": {
                    "factor": factor,
                    "multiplier": multiplier,
                    "scaled_contribution_ids": scaled,
                    "display_rarity": rarity,
                },
                "check": check.to_dict(),
            }
        )
        coverage_effect_statuses.append(
            {
                "effect_id": "rule:item_effectiveness",
                "label": item_effectiveness_result["label"],
                "status": status,
                "reason": reason,
            }
        )

    # -- 技能节点绑定（结构化条件延迟到执行期） ---------------------------
    for rule in profile.get("node_bindings") or []:
        tree_id = str(rule.get("tree") or ability)
        node_id = str(rule.get("node") or "")
        stat_index = int(rule.get("stat_index") or 0)
        value, points, check = binding.bind_node_value(
            calc, catalog, tree_id=tree_id, node_id=node_id, stat_index=stat_index
        )
        node_spec = binding._specialization_for(calc, tree_id)  # 内部确定性读取，仅编译期
        node_name = ""
        if node_spec is not None:
            entry = next(
                (node for node in node_spec.get("nodes") or [] if str(node.get("node_id")) == node_id),
                None,
            )
            node_name = str((entry or {}).get("name") or f"node {node_id}")
        rule_id = str(rule.get("id") or f"node:{tree_id}:{node_id}:{stat_index}")
        terms: list[dict[str, Any]] = []
        if rule.get("condition"):
            terms.append(_control_term(str(rule.get("condition"))))
        for key in rule.get("requires_states") or []:
            terms.append(_control_term(str(key)))
        condition_ref = register_condition("all", terms)
        per_stack = rule.get("per_stack")
        cap_per_point = rule.get("cap_per_point")
        base_value = float(value or 0.0) if check.ok else 0.0
        contribution = None
        if check.ok:
            contribution = mapping.make_contribution(
                contribution_id=f"skill:{tree_id}:{node_id}:{node_name or 'node'}",
                source_kind="skill_node",
                source_label=node_name or f"{tree_id}:{node_id}",
                source_path=f"skill_tree.{tree_id}.nodes.{node_id}",
                stat_name=str(rule.get("stat_name") or rule.get("stat") or ""),
                stat_key=str(rule.get("stat") or ""),
                operation=str(rule.get("operation") or "increased"),
                value=base_value,
                unit="percent" if rule.get("operation") in ("increased", "more") else "flat",
                damage_tags=list(rule.get("tags") or []),
                group=f"node:{tree_id}:{node_id}",
                included=True,
                value_source="source",
                status=model.STATUS_DERIVED,
                evidence_rows=[
                    model.Evidence(
                        kind="catalog",
                        detail=f"tree {tree_id} node {node_id} stat_index {stat_index}",
                        locator=f"data/releases/{calc.get('dataset_id')}/data.sqlite#trees source_tree_id={tree_id}",
                    )
                ],
                raw={
                    "rule_id": rule_id,
                    "tree": tree_id,
                    "node": node_id,
                    "stat_index": stat_index,
                    "points": points,
                    "per_point_value": (base_value / points) if points else 0.0,
                    "binding": {
                        "status": check.status,
                        "detail": check.detail,
                        "facts": check.facts,
                    },
                },
            )
            duplicate_of = append(contribution, {
                "category": "node_binding",
                "stage": "node",
                "handler_id": "node_binding-v1",
                "included_static": True,
                "condition_ref": condition_ref,
                "value_mode": "stack_scale" if per_stack else "static",
                "value_params": ({
                    "base_value": base_value,
                    "control": str(per_stack),
                    "cap_per_point": cap_per_point,
                    "points": points,
                } if per_stack else {}),
                "coverage_status": "applied",
                "rule_id": rule_id,
                "condition_style": {
                    "condition": str(rule.get("condition")) if rule.get("condition") else None,
                    "requires": [str(key) for key in rule.get("requires_states") or []],
                },
                "ledger_kind": "node_bindings",
                "ledger": _node_ledger_entry(
                    rule_id, tree_id, node_id, stat_index, points, "applied", base_value, None,
                    f"skill:{tree_id}:{node_id}:{node_name or 'node'}", check.facts,
                ),
            })
            if duplicate_of:
                contribution.contribution_id = duplicate_of
        ledger["node_bindings"].append(
            {
                "effect_id": rule_id,
                "label": node_name or rule_id,
                "status": check.status,
                "reason": check.detail if not check.ok else None,
                "contribution_id": contribution.contribution_id if contribution else None,
                "source": {
                    "tree": tree_id,
                    "node": node_id,
                    "stat_index": stat_index,
                    "points": points,
                },
                "payload": {
                    "computed_value": base_value if check.ok else None,
                    "per_stack": per_stack,
                    "cap": cap_per_point,
                },
                "check": check.to_dict() if hasattr(check, "to_dict") else {"status": check.status, "detail": check.detail, "facts": check.facts},
            }
        )
        coverage_effect_statuses.append(
            {"effect_id": rule_id, "label": node_name or rule_id, "status": check.status if not check.ok else "applied", "reason": check.detail if not check.ok else None}
        )

    # -- 属性转换 ---------------------------------------------------------
    hit_template = (profile.get("skill") or {}).get("components", {}).get("hit") or {}
    conversion_tags = list(hit_template.get("tags") or [])
    for damage_type in (profile.get("skill") or {}).get("base_damage") or {}:
        if damage_type not in conversion_tags:
            conversion_tags.append(str(damage_type))
    conversion = binding.bind_attribute_conversion(
        calc,
        catalog,
        profile,
        attributes=attributes,
        hit_component_tags=conversion_tags,
        root=context.root,
    )
    attributes_before_conversion = dict(attributes)
    scaling_attributes = dict(attributes)
    effective_strength = float(attributes.get("strength") or 0.0)
    if conversion.get("status") == binding.BINDING_APPLIED:
        source_mod_ids = set(conversion.get("source_mod_ids") or [])
        for row in rows:
            mod_id = (row.raw or {}).get("mod_id")
            if mod_id in source_mod_ids:
                row.stat_key = "attribute_conversion"
                row.stat_name = "Strength 转为 Brutality"
                row.included = True
                row.condition_status = model.CONDITION_ALWAYS
                row.exclusion_reason = None
                row.notes = "汇总属性后转换；技能、节点及力量门槛使用等效力量。"
        contribution = conversion["contribution"]
        attributes.update(conversion["attributes_update"])
        effective_strength = float(conversion["scaling_strength"])
        scaling_attributes["strength"] = effective_strength
        append(contribution, {
            "category": "attribute_conversion",
            "stage": "attribute",
            "handler_id": "attribute_conversion-v1",
            "included_static": True,
            "value_mode": "static",
            "coverage_status": "applied",
            "ledger_kind": "attribute_conversion",
            "ledger": {
                "status": conversion.get("status"),
                "source_mod_ids": conversion.get("source_mod_ids"),
                "rule_sha256": conversion.get("rule_sha256"),
            },
        })
        ledger["attribute_conversion"].append(
            {
                "status": conversion.get("status"),
                "source_mod_ids": conversion.get("source_mod_ids"),
                "rule_sha256": conversion.get("rule_sha256"),
                "evidence": [
                    item.to_dict() if hasattr(item, "to_dict") else dict(item)
                    for item in conversion.get("evidence") or []
                ],
            }
        )
    coverage_effect_statuses.append(
        {
            "effect_id": "attribute_conversion",
            "label": "属性转换",
            "status": str(conversion.get("status") or binding.BINDING_NOT_APPLICABLE),
            "reason": conversion.get("reason"),
        }
    )
    scaling_result = binding.bind_attribute_scaling(
        profile, attributes=scaling_attributes, effective_strength=effective_strength
    )
    ledger["attribute_scaling"].append(scaling_result.to_dict())
    if scaling_result.contribution is not None:
        append(scaling_result.contribution, {
            "category": "attribute_scaling",
            "stage": "attribute",
            "handler_id": "attribute_scaling-v1",
            "included_static": True,
            "value_mode": "static",
            "coverage_status": scaling_result.status,
            "ledger_kind": "attribute_scaling",
            "ledger": scaling_result.to_dict(),
        })
    coverage_effect_statuses.append(
        {"effect_id": "attribute_scaling", "label": "属性缩放", "status": scaling_result.status, "reason": scaling_result.reason}
    )

    def pool_sum(stat_key: str, target_value: Optional[str]) -> float:
        total = 0.0
        for row in rows:
            if not row.active:
                continue
            if row.stat_key != stat_key:
                continue
            if target_value is not None and str(row.target) != target_value:
                continue
            total += float(row.value)
        return total

    # -- profile effects ---------------------------------------------------
    effect_results = binding.bind_profile_effects(
        calc,
        catalog,
        profile,
        states=fake_values,
        target=fake_target,
        pool_lookup=pool_sum,
    )
    for result in effect_results:
        declared = next(
            (item for item in profile.get("effects") or [] if str(item.get("id")) == result.effect_id),
            {},
        )
        # per_attribute：属性池已就绪，在 append/序列化之前换算成最终值（含审计事实）。
        per_attribute_facts = _apply_per_attribute_scaling(result, attributes)
        terms: list[dict[str, Any]] = [
            _control_term(str(key)) for key in declared.get("states") or []
        ]
        if declared.get("requires_target"):
            terms.append({"kind": "target_true", "key": str(declared.get("requires_target"))})
        condition_ref = register_condition("all", terms)
        stack_control = declared.get("stack_control")
        base_value = float(result.contribution.value) if result.contribution is not None else 0.0
        meta = {
            "category": "effect",
            "stage": "effect",
            "handler_id": "profile_effect-v1",
            "included_static": bool(result.contribution.included) if result.contribution is not None else False,
            "condition_ref": condition_ref,
            "value_mode": "stack_scale" if stack_control else "static",
            "value_params": ({"base_value": base_value, "control": str(stack_control)} if stack_control else {}),
            "coverage_status": result.status,
            "rule_id": result.effect_id,
            "ledger_kind": "effects",
            "ledger": result.to_dict(),
            "emits_contribution": result.contribution is not None,
            "required": bool(declared.get("required", True)),
            "source_facts": copy.deepcopy(result.source or {}),
        }
        if per_attribute_facts is not None:
            # 审计事实随 ledger（result.to_dict() → payload）与 contribution.raw 序列化；
            # 此处的 meta 只供编译期门禁/调试读取，不新增计划字段。
            meta["per_attribute"] = dict(per_attribute_facts)
        if result.contribution is not None:
            duplicate_of = append(result.contribution, meta)
            if duplicate_of:
                result.contribution.contribution_id = duplicate_of
        else:
            # 非生效/来源缺失也保留 effect 记录（不产出贡献）
            pseudo_effects.append({
                **meta,
                "emits_contribution": False,
                "pseudo_row": {
                    "effect_id": result.effect_id,
                    "label": result.label,
                    "stat_key": str(declared.get("stat") or ""),
                    "operation": str(declared.get("operation") or "flag"),
                    "unit": str(declared.get("unit") or "flat"),
                    "damage_tags": list(declared.get("tags") or []),
                    "target": None,
                    "group": result.effect_id,
                },
            })
        ledger["effects"].append(result.to_dict())
        coverage_effect_statuses.append(
            {"effect_id": result.effect_id, "label": result.label, "status": result.status, "reason": result.reason}
        )

    # -- 附加伤害换算规则（plan-v2；FR Arcane Severance 迁移） --------------
    if is_v2 and rules_declared.get("added_damage_conversion"):
        rule = rules_declared["added_damage_conversion"]
        pool_spec = rule.get("source_pool") or {}
        required_tags = [str(tag) for tag in pool_spec.get("tags") or []]
        exclusive = bool(pool_spec.get("exclusive_tags", True))
        pool_total = 0.0
        pool_ids: list[str] = []
        for row in rows:
            if not row.active:
                continue
            if row.stat_key != str(pool_spec.get("stat") or model.STAT_ADDED_DAMAGE):
                continue
            if row.operation != str(pool_spec.get("operation") or "added"):
                continue
            tags = set(row.damage_tags)
            if exclusive:
                matched = tags == set(required_tags)
            else:
                matched = set(required_tags) <= tags
            if not matched:
                continue
            pool_total += float(row.value)
            pool_ids.append(row.contribution_id)
        node = rule.get("node") or {}
        tree_id = str(node.get("tree") or ability)
        node_id = str(node.get("node") or "")
        stat_index = int(node.get("stat_index") or 0)
        node_value, node_points, node_check = binding.bind_node_value(
            calc, catalog, tree_id=tree_id, node_id=node_id, stat_index=stat_index
        )
        divisor = float(rule.get("divisor") or 1.0)
        output = rule.get("output") or {}
        conversion_status = (
            binding.BINDING_APPLIED if node_check.ok else node_check.status
        )
        reason = None if node_check.ok else node_check.detail
        converted = (pool_total / divisor) * float(node_value or 0.0) if node_check.ok else 0.0
        if node_check.ok and converted != 0.0:
            contribution = mapping.make_contribution(
                contribution_id=f"derived:{ability}:{rule.get('label') or 'added_damage_conversion'}",
                source_kind="derived",
                source_label=str(rule.get("label") or "added damage conversion"),
                source_path=f"rules.added_damage_conversion",
                stat_name="converted added damage",
                stat_key=str(output.get("stat") or model.STAT_ADDED_DAMAGE),
                operation=str(output.get("operation") or "added"),
                value=float(converted),
                unit="flat",
                damage_tags=list(output.get("tags") or []),
                group="rule:added_damage_conversion",
                value_source="derived",
                status=str(rule.get("status") or model.STATUS_ASSUMED),
                evidence_rows=binding._declared_evidence(rule) + binding._declared_evidence(node),
                notes=(
                    f"迁移规则：来源池 {sorted(required_tags)} 合计 {pool_total:g} / {divisor:g} × "
                    f"节点 {tree_id}:{node_id}[{stat_index}]={float(node_value or 0.0):g}"
                ),
                raw={
                    "rule_id": "added_damage_conversion",
                    "tree": tree_id,
                    "node": node_id,
                    "stat_index": stat_index,
                    "source_pool_total": pool_total,
                    "source_pool_ids": pool_ids,
                    "divisor": divisor,
                },
            )
            append(
                contribution,
                {
                    "category": "effect",
                    "stage": "effect",
                    "handler_id": "added_damage_conversion-v1",
                    "included_static": True,
                    "value_mode": "static",
                    "coverage_status": conversion_status,
                    "rule_id": "added_damage_conversion",
                    "ledger_kind": "effects",
                    "ledger": {
                        "effect_id": "rule:added_damage_conversion",
                        "label": str(rule.get("label") or "added damage conversion"),
                        "status": conversion_status,
                        "reason": reason,
                        "contribution_id": contribution.contribution_id,
                        "source": {"rule": "added_damage_conversion", "node": node},
                        "payload": {
                            "source_pool_total": pool_total,
                            "source_pool_ids": pool_ids,
                            "divisor": divisor,
                            "node_value": float(node_value or 0.0),
                            "node_points": node_points,
                            "computed_value": float(converted),
                        },
                        "check": node_check.to_dict(),
                    },
                },
            )
            coverage_effect_statuses.append(
                {
                    "effect_id": "rule:added_damage_conversion",
                    "label": str(rule.get("label") or "added damage conversion"),
                    "status": conversion_status,
                    "reason": reason,
                }
            )

    # -- 状态缩放 ----------------------------------------------------------
    state_scaling_rules = (profile.get("rules") or {}).get("state_scalings") or []
    for declared in state_scaling_rules:
        rule_id = str(declared.get("id") or "")
        label = str(declared.get("label") or rule_id)
        state_keys = [str(key) for key in declared.get("states") or []]
        if not rule_id or not state_keys:
            raise PlanCompileError(f"state_scaling 规则不完整：{rule_id!r}")
        per_state = float(declared.get("per_state") or 0.0)
        scaling_sum = 0.0
        scaling = declared.get("scaling") or {}
        if scaling:
            scaling_sum = pool_sum(str(scaling.get("stat") or ""), None if scaling.get("target") is None else str(scaling.get("target")))
        output = declared.get("output") or {}
        fake_count = len(state_keys)
        fake_value = per_state * fake_count * (1.0 + scaling_sum)
        contribution = mapping.make_contribution(
            contribution_id=f"rule:{rule_id}",
            source_kind="derived",
            source_label=label,
            source_path=f"rules.state_scalings.{rule_id}",
            stat_name=str(output.get("stat") or ""),
            stat_key=str(output.get("stat") or ""),
            operation=str(output.get("operation") or "increased"),
            value=fake_value,
            unit="percent" if output.get("operation") in ("increased", "more") else "flat",
            damage_tags=list(output.get("tags") or []),
            group=rule_id,
            value_source="assumed",
            status=model.STATUS_ASSUMED,
            evidence_rows=binding._declared_evidence(declared),
            notes=declared.get("notes"),
            raw={"rule_id": rule_id, "scaling_sum": scaling_sum, "state_count_runtime": True},
        )
        append(contribution, {
            "category": "state_scaling",
            "stage": "state",
            "handler_id": "state_scaling-v1",
            "included_static": True,
            "value_mode": "state_count",
            "value_params": {"per_state": per_state, "scaling_sum": scaling_sum, "states": state_keys},
            "coverage_status": "applied",
            "rule_id": rule_id,
            "ledger_kind": "state_scalings",
            "ledger": {
                "effect_id": rule_id,
                "label": label,
                "status": "applied",
                "reason": None,
                "contribution_id": contribution.contribution_id,
                "source": {"states": state_keys},
                "payload": {"state_count": fake_count, "scaling_sum": scaling_sum},
                "check": None,
            },
        })
        ledger["state_scalings"].append(
            {
                "effect_id": rule_id,
                "label": label,
                "status": "applied",
                "reason": None,
                "contribution_id": contribution.contribution_id,
                "source": {"states": state_keys},
                "payload": {"state_count": fake_count, "scaling_sum": scaling_sum},
                "check": None,
            }
        )
        coverage_effect_statuses.append({"effect_id": rule_id, "label": label, "status": "applied", "reason": None})

    # -- 移速转伤害 --------------------------------------------------------
    fake_movement_total = sum(
        float(row.value)
        for row in rows
        if row.active and row.stat_key == mapping.STAT_MOVEMENT_SPEED
    )
    movement_result = binding.bind_movement_conversion(
        calc, profile, movement_total=fake_movement_total
    )
    if movement_result.contribution is not None:
        append(movement_result.contribution, {
            "category": "movement_conversion",
            "stage": "movement",
            "handler_id": "movement_conversion-v1",
            "included_static": True,
            "value_mode": "movement_conversion",
            "value_params": {
                "ratio": float(((profile.get("rules") or {}).get("movement_conversion") or {}).get("ratio") or 0.0),
                "input_stat": mapping.STAT_MOVEMENT_SPEED,
            },
            "coverage_status": movement_result.status,
            "ledger_kind": "movement_conversion",
            "ledger": movement_result.to_dict(),
        })
    ledger["movement_conversion"].append(movement_result.to_dict())
    coverage_effect_statuses.append(
        {"effect_id": movement_result.effect_id, "label": movement_result.label, "status": movement_result.status, "reason": movement_result.reason}
    )

    # -- 冷却节点 ----------------------------------------------------------
    cooldown_result = binding.bind_cooldown_reduction(calc, catalog, profile)
    cooldown_reduction = float(cooldown_result.get("reduction") or 0.0)
    if cooldown_result.get("contribution") is not None:
        append(cooldown_result["contribution"], {
            "category": "cooldown_reduction",
            "stage": "cooldown",
            "handler_id": "cooldown_reduction-v1",
            "included_static": True,
            "value_mode": "static",
            "coverage_status": str(cooldown_result.get("status") or "applied"),
            "ledger_kind": "cooldown_reduction",
            "ledger": {
                "status": cooldown_result.get("status"),
                "reduction": cooldown_reduction,
                "points": cooldown_result.get("points"),
            },
        })
    ledger["cooldown_reduction"].append(
        {
            "status": cooldown_result.get("status"),
            "reduction": cooldown_reduction,
            "points": cooldown_result.get("points"),
        }
    )
    coverage_effect_statuses.append(
        {"effect_id": "cooldown_reduction", "label": "冷缩节点", "status": str(cooldown_result.get("status")), "reason": cooldown_result.get("detail")}
    )

    # -- plan-v2 迁移行归一化（显式保留旧基线口径） --------------------------
    row_normalizations_applied: list[dict[str, Any]] = []
    if is_v2 and rules_declared.get("row_normalizations"):
        for declared in rules_declared["row_normalizations"]:
            normalization_id = str(declared.get("id") or "")
            match = declared.get("match") or {}
            updates = declared.get("set") or {}
            matched = []
            for row in rows:
                raw = row.raw or {}
                if "contribution_id" in match and row.contribution_id != match["contribution_id"]:
                    continue
                if match.get("source_kind") is not None and row.source_kind != match["source_kind"]:
                    continue
                if match.get("node_id") is not None and str(raw.get("node_id")) != str(match["node_id"]):
                    continue
                if match.get("index") is not None and str(raw.get("index")) != str(match["index"]):
                    continue
                if match.get("tree") is not None and str(raw.get("tree")) != str(match["tree"]):
                    continue
                if match.get("node") is not None and str(raw.get("node")) != str(match["node"]):
                    continue
                if match.get("stat_index") is not None and str(raw.get("stat_index")) != str(match["stat_index"]):
                    continue
                if match.get("stat_key") is not None and row.stat_key != match["stat_key"]:
                    continue
                if match.get("operation") is not None and row.operation != match["operation"]:
                    continue
                if match.get("damage_tags") is not None and sorted(row.damage_tags) != sorted(match["damage_tags"]):
                    continue
                matched.append(row)
            if not matched:
                raise PlanCompileError(
                    f"迁移行归一化 {normalization_id!r} 未命中任何当前来源行；"
                    "稳定锚点必须绑定当前实例，不能用规则掩盖缺失来源"
                )
            applied_ids = []
            for row in matched:
                meta = metas.get(id(row)) or {}
                if "value" in updates:
                    row.value = float(updates["value"])
                if "included" in updates:
                    row.included = bool(updates["included"])
                    meta["included_static"] = bool(updates["included"])
                if "condition_code" in updates:
                    row.condition_code = updates["condition_code"]
                    meta["condition_code"] = updates["condition_code"]
                    meta["condition_code_static"] = bool(updates["condition_code"])
                if "condition_status" in updates:
                    row.condition_status = str(updates["condition_status"])
                    meta["condition_status_static"] = str(updates["condition_status"])
                if "exclusion_reason" in updates:
                    row.exclusion_reason = updates["exclusion_reason"]
                    meta["exclusion_reason"] = updates["exclusion_reason"]
                if "status" in updates:
                    row.status = str(updates["status"])
                note = str(updates.get("note") or "").strip()
                if note:
                    row.notes = ((row.notes or "") + f" [迁移归一化 {normalization_id}: {note}]").strip()
                applied_ids.append(row.contribution_id)
            row_normalizations_applied.append(
                {
                    "id": normalization_id,
                    "label": declared.get("label") or normalization_id,
                    "status": str(declared.get("status") or "assumed"),
                    "applied_contribution_ids": applied_ids,
                    "set": copy.deepcopy(updates),
                    "evidence": copy.deepcopy(declared.get("evidence") or []),
                }
            )

    if conflicts:
        conflict_diagnostics = [
            {
                "code": "ownership_conflict",
                "message": (
                    f"同一所有权键出现不同定义：{conflict['effect_id']} 与 "
                    f"{conflict['existing_effect_id']}（来源 {conflict['source']}，"
                    f"值 {conflict['existing_value']!r} vs {conflict['new_value']!r}，"
                    f"条件 {conflict['existing_condition']!r} vs {conflict['new_condition']!r}）；"
                    "不能保留首条或末条，必须修正来源定义"
                ),
                "severity": "error",
                "related_ids": [conflict["effect_id"], conflict["existing_effect_id"]],
                "affects_coverage": False,
            }
            for conflict in conflicts
        ]
        return CompileResult("rejected", "blocked", None, conflict_diagnostics)

    # -- 异常额外施加概率 --------------------------------------------------
    ailment_chance = binding.resolve_ailment_additional_chance(calc, catalog, profile, states=fake_values)
    for contribution in ailment_chance.get("contributions") or []:
        ledger["ailment_chance_sources"].append(contribution.contribution_id)

    # -- plan-v2 规则解析（多命中/频率/周期平均；只读真实当前实例） ---------
    hit_events_result: Optional[dict[str, Any]] = None
    rotation_result: Optional[dict[str, Any]] = None
    if is_v2:
        hit_events_result = _resolve_hit_events_v2(calc, catalog, profile, ability=ability)
        rotation_result = _resolve_rotation_v2(calc, catalog, profile, ability=ability)

    # -- 通用 actor 原生装配（assembly_plan_version=v4-actor） --------------
    actor_model_payload: Optional[dict[str, Any]] = None
    actor_stat_transfers: list[dict[str, Any]] = []
    derived_frequency: Optional[dict[str, Any]] = None
    declared_capabilities: list[str] = []
    if actor_declared is not None:
        from . import plan_compiler_actor  # noqa: PLC0415

        try:
            actor_result = plan_compiler_actor.compile_actor_model(
                rows=rows,
                resolved_model=resolved_model,
                definition=profile,
                catalog=catalog,
                calc=calc,
            )
        except plan_compiler_actor.ActorCompileError as exc:
            raise PlanCompileError(f"actor 装配失败：{exc}") from exc
        for actor_row, actor_meta in actor_result["row_metas"]:
            if actor_meta.get("existing_row"):
                # 已被既有装配（effects/attribute_scaling）加入 rows 的行：只更新 scope 标记。
                continue
            duplicate_of = append(actor_row, actor_meta)
            if duplicate_of:
                actor_row.contribution_id = duplicate_of
        actor_model_payload = actor_result["actor_model"]
        actor_stat_transfers = list(actor_result["stat_transfers"])
        derived_frequency = actor_result["frequency"]
        declared_capabilities = ["hit_components", "minion_actor"]
        rules_for_caps = profile.get("rules") or {}
        if (
            rules_for_caps.get("stat_transfer")
            or rules_for_caps.get("attribute_scaling")
            or rules_for_caps.get("node_attribute_scaled")
        ):
            declared_capabilities.append("explicit_stat_transfer")
        if derived_frequency:
            declared_capabilities.append("derived_event_rate")
        coverage_effect_statuses.append(
            {
                "effect_id": "actor_model",
                "label": "actor 原生装配",
                "status": binding.BINDING_APPLIED,
                "reason": None,
            }
        )

    # -- 规则/参数（静态） -------------------------------------------------
    rule_payload = binding.component_rule_payload(profile, controls_by_key=profile["controls_by_key"])
    penetration_rule = rule_payload.get("damage_to_penetration")
    if penetration_rule:
        source = penetration_rule.get("source") or {}
        ratio, points, check = binding.bind_node_value(
            calc,
            catalog,
            tree_id=str(source.get("tree") or ability),
            node_id=str(source.get("node") or ""),
            stat_index=int(source.get("stat_index") or 0),
        )
        penetration_rule = dict(penetration_rule)
        penetration_rule["ratio"] = float(ratio or 0.0) if check.ok else 0.0
        penetration_rule["source_check"] = check.to_dict()
        penetration_rule["points"] = points
    parameters = {
        "skill": {
            "base_damage": dict((profile.get("skill") or {}).get("base_damage") or {}),
            "added_damage_scaling": float((profile.get("skill") or {}).get("added_damage_scaling") or 0.0),
            "crit_chance": float((profile.get("skill") or {}).get("crit_chance") or 0.0),
            "crit_multiplier": float((profile.get("skill") or {}).get("crit_multiplier") or 0.0),
            "cooldown_s": float((profile.get("skill") or {}).get("cooldown_s") or 0.0),
            "action_s": float((profile.get("skill") or {}).get("action_s") or 0.0),
            "skill_name": skill_name,
        },
        "crit": rule_payload.get("crit") or {},
        "ailments": rule_payload.get("ailments") or [],
        "additional_chance": ailment_chance.get("chance") or {},
        "damage_to_penetration": penetration_rule,
        "cooldown_reduction": cooldown_reduction,
        "attribute_ratios": copy.deepcopy(((profile.get("rules") or {}).get("attribute_scaling") or {}).get("ratios") or {}),
        "attribute_conversion": {
            "status": conversion.get("status"),
            "source_mod_ids": conversion.get("source_mod_ids"),
            "rule_sha256": conversion.get("rule_sha256"),
        },
    }
    if is_v2:
        parameters["skill"]["base_attack_rate"] = float(
            (profile.get("skill") or {}).get("base_attack_rate") or 0.0
        )

    # -- 覆盖缺口（编译期静态部分） ---------------------------------------
    claimed_mod_ids: set[str] = set(conversion.get("matched_mod_ids") or [])
    for declared in profile.get("effects") or []:
        source = declared.get("source") or {}
        if source.get("kind") == binding.EQUIPMENT_MOD and source.get("mod_id"):
            claimed_mod_ids.add(str(source.get("mod_id")))
        if source.get("kind") == binding.UNIQUE_DESCRIPTION:
            claimed_mod_ids.add(_description_key(source))
        if source.get("kind") == binding.STATE_SOURCE:
            state_source = (profile.get("state_sources") or {}).get(str(source.get("control"))) or {}
            claimed_mod_ids.add(_state_source_key(state_source))
    for source in (profile.get("state_sources") or {}).values():
        claimed_mod_ids.add(_state_source_key(source))
    coverage_decision_result = _compile_coverage_decisions(calc, profile, facts)
    claimed_mod_ids.update(coverage_decision_result["claimed_mod_ids"])
    declared_inactive_checks = coverage_decision_result["declared_inactive_checks"]
    early_issues = coverage_decision_result["assumed_active_issues"]
    coverage_decisions = coverage_decision_result["decisions"]
    coverage_decision_diagnostics = coverage_decision_result["diagnostics"]
    movement_source = ((profile.get("rules") or {}).get("movement_conversion") or {}).get("source") or {}
    if movement_source.get("kind") == binding.EQUIPMENT_MOD and movement_source.get("mod_id"):
        claimed_mod_ids.add(str(movement_source.get("mod_id")))
    item_effectiveness_source = (
        ((profile.get("rules") or {}).get("item_effectiveness") or {}).get("source") or {}
    )
    if (
        item_effectiveness_source.get("kind") == binding.EQUIPMENT_MOD
        and item_effectiveness_source.get("mod_id")
    ):
        claimed_mod_ids.add(str(item_effectiveness_source.get("mod_id")))
    extra_gaps = [
        {"code": "ailment_chance_source", "detail": gap.get("detail")}
        for gap in ailment_chance.get("gaps") or []
    ]
    if cooldown_result.get("contribution") is None and cooldown_result.get("status") not in (
        None,
        binding.BINDING_NOT_APPLICABLE,
    ):
        extra_gaps.append(
            {
                "code": "cooldown_reduction_source",
                "detail": f"冷却缩减节点不可用：{cooldown_result.get('detail')}",
            }
        )
    base_gaps = dps_result.static_coverage_gaps(
        contributions=rows,
        claimed_mod_ids=claimed_mod_ids,
        effect_statuses=coverage_effect_statuses,
        declared_inactive_checks=declared_inactive_checks,
        skill_constants=skill_constants,
        per_attribute_problems=per_attribute_problems,
        extra_gaps=extra_gaps,
    )
    post_assembly_issues: list[str] = []
    if conversion.get("status") == binding.BINDING_SOURCE_ABSENT:
        post_assembly_issues.append("当前输入未命中属性转换来源（规则不生效）")
    duplicate_issues = [
        f"重复注入已合并：{duplicate['kind']} {duplicate['effect_id']} 与 {duplicate['duplicate_of']} 同所有权同语义"
        for duplicate in duplicates
        if duplicate.get("disposition") != "merged"
    ]

    # -- 序列化 effects ----------------------------------------------------
    effects: list[dict[str, Any]] = []
    used_effect_ids: dict[str, int] = {}
    used_ownership: dict[str, str] = {}
    for index, row in enumerate(rows):
        meta = metas.get(id(row))
        if meta is None:
            raise PlanCompileError(f"行缺少编译元数据：{row.contribution_id}")
        serialized = _serialize_effect(index, row, meta)
        serialized["stage"] = plan_handlers.CATEGORY_STAGE.get(
            str(meta.get("category") or "effect"), "damage_components"
        )
        merged_refs = [meta.get("merged_source_refs") or []]
        serialized["source_refs"] = list(
            dict.fromkeys(
                [_effect_source_id(row, meta)] + [ref for refs in merged_refs for ref in refs]
            )
        )
        serialized["ownership_key"] = _ownership_payload(row)
        serialized["required"] = bool(meta.get("required", True))
        serialized["support_status"] = str(meta.get("coverage_status") or "mapped")
        ownership_digest = planmod.canonical_json(serialized["ownership_key"])
        if ownership_digest in used_ownership:
            raise PlanCompileError(
                f"计划内部错误：所有权合并后仍出现重复键：{serialized['effect_id']}"
            )
        used_ownership[ownership_digest] = serialized["effect_id"]
        base_id = serialized["effect_id"]
        if base_id in used_effect_ids:
            used_effect_ids[base_id] += 1
            serialized["effect_id"] = f"{base_id}#{used_effect_ids[base_id]}"
        else:
            used_effect_ids[base_id] = 0
        serialized["contribution_id"] = row.contribution_id
        effects.append(serialized)
    next_index = len(effects)
    for meta in pseudo_effects:
        pseudo = meta["pseudo_row"]
        effects.append(
            {
                "effect_id": pseudo["effect_id"],
                "contribution_id": None,
                "sequence": next_index,
                "label": pseudo["label"],
                "category": "effect",
                "stage": "effect",
                "handler_id": "profile_effect-v1",
                "handler_version": plan_handlers.VERSION,
                "source_kind": "derived",
                "source_label": pseudo["label"],
                "source_path": pseudo["effect_id"],
                "stat_key": pseudo["stat_key"],
                "stat_name": pseudo["stat_key"],
                "operation": pseudo["operation"],
                "value": 0.0,
                "unit": pseudo["unit"],
                "damage_tags": pseudo["damage_tags"],
                "target": None,
                "group": pseudo["group"],
                "value_source": "assumed",
                "status": model.STATUS_ASSUMED,
                "notes": None,
                "stage": "derived_pools",
                "source_refs": [],
                "ownership_key": None,
                "required": True,
                "support_status": str(meta.get("coverage_status") or "unresolved"),
                "included_static": False,
                "condition_status_static": model.CONDITION_ALWAYS,
                "exclusion_reason": meta["ledger"].get("reason") if isinstance(meta.get("ledger"), dict) else None,
                "condition_ref": meta.get("condition_ref"),
                "condition_code": None,
                "coverage_class": None,
                "coverage_status": meta.get("coverage_status"),
                "required": meta.get("required", True),
                "emits_contribution": False,
                "value_mode": "static",
                "value_params": {},
                "ledger_kind": "effects",
                "ledger": meta.get("ledger"),
                "owner": None,
                "raw": {"effect_id": pseudo["effect_id"]},
                "evidence": [],
            }
        )
        next_index += 1

    # -- sources -----------------------------------------------------------
    sources_by_id: dict[str, dict[str, Any]] = {}
    for effect in effects:
        if not effect.get("emits_contribution"):
            continue
        source_refs = effect.get("source_refs") or []
        if not source_refs:
            raise PlanCompileError(f"effect {effect.get('effect_id')} 没有 source_refs")
        for source_id in source_refs:
            owner = effect.get("owner") or {}
            source_facts = effect.get("source_facts") or {}
            source_kind = str(effect.get("source_kind") or "derived")
            identity: dict[str, Any] = {}
            if source_id.startswith("item_mod:"):
                source_kind = "item_mod"
                identity = {"mod_id": owner.get("mod_id") or source_id.split(":", 1)[1]}
            elif source_id.startswith("skill_node:"):
                source_kind = "skill_node"
                identity = dict(owner)
            elif source_id.startswith("passive_node:"):
                source_kind = "passive_node"
                identity = dict(owner)
            elif source_id.startswith("effect:"):
                source_kind = str(source_facts.get("kind") or "rule_source")
                identity = dict(source_facts) or {"effect_id": source_id.split(":", 1)[1]}
            elif source_id.startswith("base_character:"):
                source_kind = "base_character"
                identity = {"source_path": effect.get("source_path")}
            else:
                source_kind = "rule_source"
                identity = {"source_path": effect.get("source_path")}
            if source_kind not in plan_handlers.SOURCE_KINDS:
                source_kind = "rule_source"
            record = sources_by_id.get(source_id)
            if record is None:
                record = {
                    "source_id": source_id,
                    "kind": source_kind,
                    "identity": identity,
                    "semantics": {
                        "stat_keys": [],
                        "binding_status": effect.get("coverage_status") or "mapped",
                    },
                    "binding_status": effect.get("coverage_status") or "mapped",
                    "evidence": [],
                }
                sources_by_id[source_id] = record
            else:
                if effect.get("stat_key") not in record["semantics"]["stat_keys"]:
                    pass
                if str(effect.get("coverage_status") or "mapped") in plan_handlers.SOURCE_FAILURE_STATUSES:
                    record["binding_status"] = str(effect.get("coverage_status"))
                    record["semantics"]["binding_status"] = record["binding_status"]
            if effect.get("stat_key") not in record["semantics"]["stat_keys"]:
                record["semantics"]["stat_keys"].append(effect.get("stat_key"))
            for item in effect.get("evidence") or []:
                if item not in record["evidence"]:
                    record["evidence"].append(item)
    # 状态来源 / 规则来源（显式记录绑定状态；不要求每个都被 effect 直接引用）
    for control, declared in (profile.get("state_sources") or {}).items():
        state_source = copy.deepcopy(declared)
        source_id = f"state_source:{control}"
        check = binding._state_source_check(calc, profile, control)
        sources_by_id[source_id] = {
            "source_id": source_id,
            "kind": "state_source",
            "identity": {"control": control, **{key: value for key, value in state_source.items() if key != "kind"}},
            "semantics": {"stat_keys": [], "binding_status": check.status},
            "binding_status": check.status,
            "evidence": [],
        }
    if movement_result.check is not None:
        movement_status = movement_result.check.status
        sources_by_id["rule_source:movement_conversion"] = {
            "source_id": "rule_source:movement_conversion",
            "kind": "rule_source",
            "identity": {
                "rule": "movement_conversion",
                "source": copy.deepcopy(movement_result.source or {}),
            },
            "semantics": {"stat_keys": [mapping.STAT_MOVEMENT_SPEED, "damage"], "binding_status": movement_status},
            "binding_status": movement_status,
            "evidence": [item.to_dict() for item in movement_result.evidence],
        }
    conversion_evidence = [
        item.to_dict() if hasattr(item, "to_dict") else dict(item)
        for item in conversion.get("evidence") or []
    ]
    sources_by_id["rule_source:attribute_conversion"] = {
        "source_id": "rule_source:attribute_conversion",
        "kind": "rule_source",
        "identity": {
            "rule": "attribute_conversion",
            "source_mod_ids": conversion.get("source_mod_ids") or [],
            "rule_sha256": conversion.get("rule_sha256"),
        },
        "semantics": {"stat_keys": [], "binding_status": str(conversion.get("status") or binding.BINDING_NOT_APPLICABLE)},
        "binding_status": str(conversion.get("status") or binding.BINDING_NOT_APPLICABLE),
        "evidence": conversion_evidence,
    }
    # plan-v2 规则来源：多命中/频率/物品效率规则本身也是可追溯来源
    if item_effectiveness_result is not None:
        sources_by_id["rule_source:item_effectiveness"] = {
            "source_id": "rule_source:item_effectiveness",
            "kind": "rule_source",
            "identity": {
                "rule": "item_effectiveness",
                "target_slot": item_effectiveness_result["target_slot"],
                "source": copy.deepcopy(item_effectiveness_result["source"]),
            },
            "semantics": {
                "stat_keys": [],
                "binding_status": str(item_effectiveness_result["status"]),
            },
            "binding_status": str(item_effectiveness_result["status"]),
            "evidence": copy.deepcopy(
                ((profile.get("rules") or {}).get("item_effectiveness") or {}).get("evidence") or []
            ),
        }
    if hit_events_result is not None:
        sources_by_id["rule_source:hit_events"] = {
            "source_id": "rule_source:hit_events",
            "kind": "rule_source",
            "identity": {
                "rule": "hit_events",
                "base_hits": hit_events_result["base_hits"],
                "hits_per_cast": hit_events_result["hits_per_cast"],
                "return_sources": copy.deepcopy(hit_events_result["return_sources"]),
            },
            "semantics": {"stat_keys": ["hits_per_cast"], "binding_status": binding.BINDING_APPLIED},
            "binding_status": binding.BINDING_APPLIED,
            "evidence": copy.deepcopy(hit_events_result["evidence"]),
        }
    if rotation_result is not None:
        sources_by_id["rule_source:rotation"] = {
            "source_id": "rule_source:rotation",
            "kind": "rule_source",
            "identity": {
                "rule": "rotation",
                "primary_policy": rotation_result["primary_policy"],
                "rhythm_cycle": copy.deepcopy(rotation_result["rhythm_cycle"]),
            },
            "semantics": {"stat_keys": ["frequency"], "binding_status": binding.BINDING_APPLIED},
            "binding_status": binding.BINDING_APPLIED,
            "evidence": copy.deepcopy(rotation_result["evidence"]),
        }
    if row_normalizations_applied:
        sources_by_id["rule_source:row_normalizations"] = {
            "source_id": "rule_source:row_normalizations",
            "kind": "rule_source",
            "identity": {
                "rule": "row_normalizations",
                "normalizations": copy.deepcopy(row_normalizations_applied),
            },
            "semantics": {"stat_keys": [], "binding_status": binding.BINDING_APPLIED},
            "binding_status": binding.BINDING_APPLIED,
            "evidence": [
                item
                for applied in row_normalizations_applied
                for item in applied.get("evidence") or []
            ],
        }
    sources = sorted(sources_by_id.values(), key=lambda item: item["source_id"])

    # -- 计划 -----------------------------------------------------------------
    scope_payload = {
        "skill": skill_name,
        "ailment_labels": [
            {"name": declared.get("name"), "label_zh": declared.get("label_zh")}
            for declared in rule_payload.get("ailments") or []
        ],
        "limitations": [str(item) for item in profile.get("limitations") or []],
    }
    diagnostics = []
    for gap in base_gaps:
        related: list[str] = []
        if gap.get("contribution_ids"):
            related.extend(str(item) for item in gap["contribution_ids"])
        if gap.get("effect_id"):
            related.append(str(gap["effect_id"]))
        diagnostics.append(
            {
                "code": gap["code"],
                "message": gap["detail"],
                "severity": "warning",
                "related_ids": related,
                "affects_coverage": True,
                "count": gap["count"],
                "impact": "coverage_partial",
            }
        )
    for duplicate in duplicates:
        diagnostics.append(
            {
                "code": (
                    "duplicate_merged"
                    if duplicate.get("disposition") == "merged"
                    else "duplicate_effect"
                ),
                "message": (
                    f"同所有权同语义重复已合并来源：{duplicate['effect_id']} → "
                    f"{duplicate['duplicate_of']}"
                    if duplicate.get("disposition") == "merged"
                    else f"重复注入已抑制：{duplicate['kind']} {duplicate['effect_id']}"
                ),
                "severity": "info",
                "related_ids": [duplicate["effect_id"], duplicate["duplicate_of"]],
                "affects_coverage": False,
            }
        )
    diagnostics.extend(copy.deepcopy(coverage_decision_diagnostics))
    assumptions = [
        {
            "assumption_id": f"limitation:{index}",
            "status": "assumed",
            "kind": "limitation",
            "scope": "result",
            "note": str(note),
        }
        for index, note in enumerate(profile.get("limitations") or [])
    ]
    fingerprint = plan_handlers.implementation_fingerprint()
    if is_v2:
        if rotation_result and rotation_result.get("rhythm_cycle"):
            cycle = rotation_result["rhythm_cycle"]
            assumptions.append(
                {
                    "assumption_id": "rule:rotation:rhythm_cycle",
                    "status": str(cycle.get("status") or "assumed"),
                    "kind": "rotation",
                    "scope": "dps",
                    "note": (
                        f"周期平均因子 ×{rotation_result['factor']:.6g}：每 {cycle['cycle_casts']} 次施放"
                        f"消耗一次（来源节点 {cycle['consume_more']['tree']}:{cycle['consume_more']['node']}"
                        f"[{cycle['consume_more']['stat_index']}]={cycle['consume_more']['value']:g}）；"
                        "只作用于持续输出评估，不改变首击伤害；叠层获取/消耗全过程不模拟。"
                    ),
                }
            )
        if item_effectiveness_result is not None:
            assumptions.append(
                {
                    "assumption_id": "rule:item_effectiveness",
                    "status": str(item_effectiveness_result["status"]),
                    "kind": "item_effectiveness",
                    "scope": "result",
                    "note": (
                        f"{item_effectiveness_result['label']}：目标槽位 {item_effectiveness_result['target_slot']}"
                        f" 显示稀有度 {item_effectiveness_result['display_rarity']!r}，"
                        f"倍率 ×{item_effectiveness_result['multiplier']:.6g}"
                    ),
                }
            )
    if is_v2:
        components_payload = _component_entries_v2(profile, hit_events_result)
        schedule_payload: dict[str, Any] = {
            "schema": "le-schedule-v2",
            "driver": "steady_state",
            "first_cast_s": 0.0,
            "windows_s": [float(value) for value in rotation_result["windows_s"]],
            "window_semantics": str(rotation_result["window_semantics"]),
            "policies": copy.deepcopy(rotation_result["policies"]),
            "primary_policy": str(rotation_result["primary_policy"]),
            "rotation": {
                "kind": "fixed_more_factor",
                "factor": float(rotation_result["factor"]),
                "applies_to": ["direct_hit_dps"],
                "status": str(rotation_result["status"]),
                "label": (
                    (rotation_result.get("rhythm_cycle") or {}).get("label")
                    or "周期平均因子"
                ),
                "evidence": copy.deepcopy(rotation_result["evidence"]),
            },
            "every_nth_hit": None,
        }
    else:
        components_payload = _component_entries(profile)
        schedule_payload = {
            "driver": "fixed_repeat",
            "interval_policy": "max(action_time, cooldown)",
            "first_cast_s": 0.0,
            "windows_s": list(SCHEDULE_WINDOWS),
            "every_nth_hit": copy.deepcopy(rule_payload.get("every_nth_hit")),
        }
        if derived_frequency:
            schedule_payload["driver"] = "derived_event_rate"
            schedule_payload["derivation"] = copy.deepcopy(
                derived_frequency.get("derivation") or {}
            )
    plan_payload: dict[str, Any] = {
        "schema": plan_schema,
        "subject": {
            "case_id": str(case.get("case_id") or ""),
            "build_id": str(
                (context.build_identity or {}).get("build_id")
                or (case.get("build") or {}).get("build_id")
                or f"build:{case.get('case_id') or 'synthetic'}"
            ),
            "revision_id": str(
                (context.build_identity or {}).get("revision_id")
                or (case.get("build") or {}).get("revision_id")
                or "synthetic-rev-1"
            ),
            "main_ability_id": ability,
        },
        "compiler": {
            "version": COMPILER_VERSION,
            "origin": (
                "definitions_v3"
                if (is_v2 or getattr(resolved_model, "origin", "") == "definitions_v3")
                else "profile_v2"
            ),
            "implementation_fingerprint": fingerprint["compiler_files"],
            "fingerprint": fingerprint["compiler_fingerprint"],
        },
        "executor_requirements": {
            "handlers": plan_handlers.handler_versions("execute"),
            "executor_fingerprint": fingerprint["executor_fingerprint"],
        },
        "input_refs": copy.deepcopy(context.input_refs),
        "sources": sources,
        "facts": {
            "character": copy.deepcopy(calc.get("character")),
            "skill_name": skill_name,
            "attributes": copy.deepcopy(attributes),
            "attribute_conversion_info": copy.deepcopy(conversion.get("conversion_info")),
            "equipment_facts": facts.to_dict(),
            "skill_constants": copy.deepcopy(skill_constants),
            "ailment_constants": copy.deepcopy(ailment_constants),
            "coverage_decisions": copy.deepcopy(coverage_decisions),
            "attribute_views": {
                "original": copy.deepcopy(attributes_before_conversion),
                "converted": copy.deepcopy(conversion.get("conversion_info") or {}),
                "effective": copy.deepcopy(attributes),
            },
            "coverage_base_gaps": copy.deepcopy(base_gaps),
            "early_issues": early_issues,
            "post_assembly_issues": post_assembly_issues,
            "duplicate_effects": copy.deepcopy(duplicates),
            "duplicate_issues": duplicate_issues,
            "per_attribute_problems": per_attribute_problems,
            "actor_model": copy.deepcopy(actor_model_payload) if actor_model_payload else {},
            "stat_transfers": copy.deepcopy(actor_stat_transfers),
            "declared_capabilities": list(declared_capabilities),
            "derived_frequency": copy.deepcopy(derived_frequency) if derived_frequency else None,
            "hit_events": copy.deepcopy(hit_events_result),
            "rotation": copy.deepcopy(rotation_result),
            "item_effectiveness": copy.deepcopy(item_effectiveness_result),
            "row_normalizations": copy.deepcopy(row_normalizations_applied),
            "ledger_static": {
                "node_bindings": copy.deepcopy(ledger["node_bindings"]),
                "effects": copy.deepcopy(ledger["effects"]),
                "state_scalings": copy.deepcopy(ledger["state_scalings"]),
                "movement_conversion": copy.deepcopy(ledger["movement_conversion"][0] if ledger["movement_conversion"] else {}),
                "attribute_scaling": copy.deepcopy(ledger["attribute_scaling"][0] if ledger["attribute_scaling"] else {}),
                "attribute_conversion": copy.deepcopy(ledger["attribute_conversion"][0] if ledger["attribute_conversion"] else {}),
                "cooldown_reduction": copy.deepcopy(ledger["cooldown_reduction"][0] if ledger["cooldown_reduction"] else {}),
                "ailment_chance_sources": copy.deepcopy(ledger["ailment_chance_sources"]),
                "equipment_condition_effects": copy.deepcopy(ledger["equipment_condition_effects"]),
            },
        },
        "parameters": parameters,
        "controls": _control_entries(profile),
        "conditions": conditions,
        "components": components_payload,
        "effects": effects,
        "evaluation": {
            "stage_order": list(plan_handlers.STAGE_ORDER),
            "effect_stage": {effect["effect_id"]: effect["stage"] for effect in effects},
            "dependencies": plan_handlers.canonical_dependencies(),
            "ownership": "unique_by_ownership_key",
        },
        "schedule": schedule_payload,
        "diagnostics": diagnostics,
        "assumptions": assumptions,
        "scope": scope_payload,
        "presentation": {
            "skill_name": skill_name,
            "skill_names": {
                row["tree"]["entity_id"].rsplit(":", 1)[-1]: row["tree"]["name"]
                for row in (calc.get("skills") or {}).get("specializations") or []
            },
            "character": copy.deepcopy(calc.get("character")),
            "equipment": [
                {**copy.deepcopy(item), "mods": copy.deepcopy(
                    [mod for mod in calc.get("mods") or [] if mod.get("item_instance_id") == item["instance_id"]]
                )}
                for item in calc.get("items") or []
            ],
            "controls": copy.deepcopy(profile.get("controls") or []),
        },
    }
    if is_v2:
        plan_payload["evaluations"] = [
            {
                "evaluation_id": str(evaluation.get("evaluation_id")),
                "label": evaluation.get("label") or evaluation.get("evaluation_id"),
                "metrics": list(evaluation.get("metrics") or []),
                "primary": bool(evaluation.get("primary")),
                "scenario_path": evaluation.get("scenario_path"),
                "scenario_sha256": evaluation.get("scenario_sha256"),
            }
            for evaluation in profile.get("evaluations") or []
        ]
    plan = planmod.finalize_plan(plan_payload, schema=plan_schema)
    readiness = "partial" if base_gaps else "ready"
    return CompileResult("completed", readiness, plan, diagnostics)


COVERAGE_DECISION_FIELDS = (
    "mod_id",
    "reason",
    "evidence",
    "require",
    "expected_definition_entity_id",
    "expected_base_entity_id",
    "expected_property",
    "expected_special_tag",
    "expected_readable_text_contains",
)


def compile_from_profile_v2(context: FrozenContext, profile: dict[str, Any]) -> CompileResult:
    """profile-v2 适配器：校验旧 profile 后归一到独立 ResolvedModel。"""
    try:
        validated = binding.validate_profile(copy.deepcopy(profile))
    except binding.BindingError as exc:
        return CompileResult(
            "rejected",
            "blocked",
            None,
            [{"code": "invalid_profile", "message": str(exc), "severity": "error", "related_ids": ["profile"], "affects_coverage": False}],
        )
    definition = {
        key: copy.deepcopy(value)
        for key, value in validated.items()
        if key not in ("schema", "profile_version", "controls", "controls_by_key", "monster_level", "resistance")
    }
    controls = copy.deepcopy(validated.get("controls") or [])
    return compile_from_resolved_model(
        context,
        ResolvedModel(
            origin="profile_v2",
            ability_id=str(validated.get("ability_id") or ""),
            definition=definition,
            controls=controls,
            scenario={
                "monster_level": validated.get("monster_level"),
                "resistance": validated.get("resistance"),
            },
            instance={"build_id": (context.case.get("build") or {}).get("build_id")},
        ),
    )


def _mod_instance_identity(calc: dict[str, Any], mod_id: str) -> Optional[dict[str, Any]]:
    """从当前 calculation input 解析 mod 的实际身份（不接受裸 mod_id 声明）。"""
    mod = next((item for item in calc.get("mods") or [] if str(item.get("mod_id")) == mod_id), None)
    if mod is None:
        return None
    item = next(
        (
            row
            for row in calc.get("items") or []
            if str(row.get("instance_id")) == str(mod.get("item_instance_id"))
        ),
        None,
    )
    if item is None:
        return None
    stat = mod.get("stat") or {}
    return {
        "mod_id": mod_id,
        "item_instance_id": mod.get("item_instance_id"),
        "definition_entity_id": item.get("definition_entity_id"),
        "base_entity_id": item.get("base_entity_id"),
        "property": mod.get("property"),
        "special_tag": mod.get("special_tag"),
        "stat_name": stat.get("name"),
        "modifier_type": mod.get("modifier_type"),
        "source": mod.get("source"),
    }


def _coverage_premise(
    *,
    action: str,
    declared: dict[str, Any],
    identity: dict[str, Any],
    facts: Any,
) -> dict[str, Any]:
    requirement = declared.get("require") or {}
    if requirement:
        satisfied = True
        details: list[str] = []
        for fact_name, expected in requirement.items():
            actual = getattr(facts, fact_name, None)
            if actual != expected:
                satisfied = False
                details.append(f"{fact_name}={expected!r}，实际 {actual!r}")
        return {
            "kind": "require",
            "require": copy.deepcopy(requirement),
            "satisfied": satisfied,
            "detail": "；".join(details),
        }
    if action == "confirmed_inactive":
        return {
            "kind": "require",
            "require": {},
            "satisfied": False,
            "detail": "confirmed_inactive 必须声明 require 事实前提",
        }
    if action == "not_applicable":
        return {
            "kind": "scope_exclusion",
            "require": {},
            "satisfied": True,
            "detail": "声明把该来源排除出当前计算范围（按声明假设记录，不升级为 confirmed）",
        }
    return {
        "kind": "assumption",
        "require": {},
        "satisfied": True,
        "detail": "显式假设该来源条件成立（结果保留未验证标记）",
    }


def _compile_coverage_decisions(
    calc: dict[str, Any],
    profile: dict[str, Any],
    facts: Any,
) -> dict[str, Any]:
    """把 profile 的来源裁决声明编译成有身份/证据/前提的裁决记录。

    只有身份可从当前输入解析、证据非空且前提满足（或明确为作用域假设）的声明才能
    把来源加入“已处理”集合；缺失理由/证据/身份或前提不满足的声明被拒绝，原缺口保留。
    """
    coverage = profile.get("coverage") or {}
    claimed: set[str] = set()
    declared_inactive_checks: list[dict[str, Any]] = []
    assumed_active_issues: list[str] = []
    decisions: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []

    def reject(action: str, mod_id: str, detail: str) -> None:
        decisions.append(
            {
                "decision_id": f"coverage:{action}:{mod_id or 'missing'}",
                "action": action,
                "mod_id": mod_id,
                "identity": {},
                "reason": "",
                "evidence": [],
                "premise": {"kind": "rejected", "satisfied": False, "detail": detail},
                "status": "rejected",
                "applied": False,
            }
        )
        diagnostics.append(
            {
                "code": "coverage_decision_rejected",
                "message": f"{action} 声明被拒绝（{mod_id or '缺少 mod_id'}）：{detail}",
                "severity": "warning",
                "related_ids": [mod_id or action],
                "affects_coverage": False,
            }
        )

    for action, key in (
        ("confirmed_inactive", "confirmed_inactive"),
        ("not_applicable", "not_applicable_mods"),
        ("assumed_active", "assumed_active_mods"),
    ):
        for declared in coverage.get(key) or []:
            if not isinstance(declared, dict):
                reject(action, "", "声明必须是对象")
                continue
            unknown = set(declared) - set(COVERAGE_DECISION_FIELDS)
            if unknown:
                reject(action, str(declared.get("mod_id") or ""), f"声明含未支持字段：{sorted(unknown)}")
                continue
            mod_id = str(declared.get("mod_id") or "")
            if not mod_id:
                reject(action, "", "缺少 mod_id")
                continue
            reason = str(declared.get("reason") or "").strip()
            if not reason:
                reject(action, mod_id, "缺少 reason（不能仅凭 mod_id 消除缺口）")
                continue
            evidence = declared.get("evidence") or []
            if not isinstance(evidence, list) or not evidence:
                reject(action, mod_id, "缺少 evidence（来源裁决必须有证据入口）")
                continue
            normalized_evidence = []
            for item in evidence:
                if isinstance(item, dict) and item.get("detail"):
                    normalized_evidence.append(dict(item))
                elif isinstance(item, str) and item.strip():
                    normalized_evidence.append({"kind": "declared", "detail": item})
                else:
                    normalized_evidence = []
                    break
            if not normalized_evidence:
                reject(action, mod_id, "evidence 条目必须包含 detail")
                continue
            identity = _mod_instance_identity(calc, mod_id)
            if identity is None:
                reject(action, mod_id, "当前 calculation input 中不存在该来源实例，无法验证身份")
                continue
            for anchor, expected in (
                ("expected_definition_entity_id", identity["definition_entity_id"]),
                ("expected_base_entity_id", identity["base_entity_id"]),
                ("expected_property", identity["property"]),
                ("expected_special_tag", identity["special_tag"]),
            ):
                if anchor not in declared:
                    continue
                if declared.get(anchor) != expected:
                    reject(
                        action,
                        mod_id,
                        f"身份锚点 {anchor} 与当前输入不一致（声明 {declared.get(anchor)!r}，实际 {expected!r}）",
                    )
                    identity = None
                    break
            if identity is None:
                continue
            anchor = declared.get("expected_readable_text_contains")
            if anchor:
                mod = next(
                    (row for row in calc.get("mods") or [] if str(row.get("mod_id")) == mod_id), {}
                )
                if str(anchor) not in str(mod.get("readable_text") or ""):
                    reject(action, mod_id, "readable_text 语义锚点与当前输入不一致")
                    continue
            premise = _coverage_premise(action=action, declared=declared, identity=identity, facts=facts)
            applied = bool(premise["satisfied"])
            decision_status = "verified" if applied else "rejected"
            if applied and premise.get("kind") in ("scope_exclusion", "assumption"):
                decision_status = "assumed"
            diagnostics_evidence = [
                {
                    "kind": "calculation_input",
                    "detail": f"mod {mod_id} 当前实例",
                    "locator": f"{calc.get('_calculation_input_path') or 'calculation-input'}#mods {mod_id}",
                },
                {
                    "kind": "definition",
                    "detail": "item definition / base entity",
                    "locator": (
                        f"{identity.get('definition_entity_id') or ''}"
                        f" / {identity.get('base_entity_id') or ''}"
                    ),
                },
            ]
            merged_evidence = normalized_evidence + diagnostics_evidence
            decisions.append(
                {
                    "decision_id": f"coverage:{action}:{mod_id}",
                    "action": action,
                    "mod_id": mod_id,
                    "identity": identity,
                    "reason": reason,
                    "evidence": merged_evidence,
                    "premise": premise,
                    "status": decision_status,
                    "applied": applied,
                }
            )
            if not applied:
                diagnostics.append(
                    {
                        "code": "coverage_decision_rejected",
                        "message": f"{action} 声明的前提未满足：{mod_id}：{premise.get('detail')}",
                        "severity": "warning",
                        "related_ids": [mod_id],
                        "affects_coverage": False,
                    }
                )
                if action == "confirmed_inactive":
                    declared_inactive_checks.append(
                        {
                            "mod_id": mod_id,
                            "satisfied": False,
                            "detail": f"{mod_id} 声明不生效要求未满足：{premise.get('detail')}",
                        }
                    )
                continue
            claimed.add(mod_id)
            if action == "assumed_active":
                assumed_active_issues.append(f"{reason}（按条件成立处理，未验证）")
    return {
        "claimed_mod_ids": claimed,
        "declared_inactive_checks": declared_inactive_checks,
        "assumed_active_issues": assumed_active_issues,
        "decisions": decisions,
        "diagnostics": diagnostics,
    }


def _description_key(source: dict[str, Any]) -> str:
    return (
        f"{source.get('item_instance_id')}#unique_description#"
        f"{int(source.get('description_index') or 0)}"
    )


def _state_source_key(source: dict[str, Any]) -> str:
    if source.get("kind") == binding.EQUIPMENT_MOD:
        return str(source.get("mod_id") or "")
    if source.get("kind") == binding.UNIQUE_DESCRIPTION:
        return _description_key(source)
    return ""


TEMPLATE_SCHEMA = "le-skill-template-v2"
TEMPLATE_FIELDS = (
    "schema",
    "template_id",
    "ability_id",
    "title",
    "skill",
    "effects",
    "controls",
    "ailments",
    "rules",
    "schedule",
    "assumptions",
    "limitations",
    "mechanism_refs",
)
TEMPLATE_EFFECT_KEYS = (
    "effect_id",
    "label",
    "stat_key",
    "operation",
    "value",
    "unit",
    "damage_tags",
    "target",
    "group",
    "status",
    "condition",
    "source",
    "notes",
    "handler_id",
)


def _control_term(key: str) -> dict[str, Any]:
    """condition term：目标侧键生成 target_true，其余按状态控制项。"""
    if key in TARGET_KEYS:
        return {"kind": "target_true", "key": key}
    return {"kind": "control_true", "key": key}


def _template_title(template: dict[str, Any]) -> str:
    title = template.get("title") or template.get("template_id") or "synthetic skill"
    if isinstance(title, dict):
        return str(title.get("zh") or title.get("en") or template.get("template_id") or "synthetic skill")
    return str(title)


def _template_control_entries(template: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for control in template.get("controls") or []:
        if not isinstance(control, dict) or not control.get("key"):
            raise PlanCompileError("skill-template controls 必须是含 key 的对象")
        entries.append(
            {
                "control_id": str(control["key"]),
                "namespace": "state",
                "key": str(control["key"]),
                "label": control.get("label") or control["key"],
                "type": control.get("type") or "boolean",
                "default": control.get("default"),
                "min": control.get("min"),
                "max": control.get("max"),
                "readonly": bool(control.get("readonly")),
            }
        )
    for key in TARGET_KEYS:
        control_type, minimum, maximum = TARGET_CONTROL_TYPES[key]
        entries.append(
            {
                "control_id": f"target.{key}",
                "namespace": "target",
                "key": key,
                "label": key,
                "type": control_type,
                "default": DEFAULT_TARGET[key],
                "min": minimum,
                "max": maximum,
                "readonly": False,
            }
        )
    return entries


def _template_condition(declared: Any, control_keys: set[str]) -> tuple[str, list[dict[str, Any]]]:
    if not declared:
        return "always", []
    if not isinstance(declared, dict):
        raise PlanCompileError("template effect.condition 必须是对象")
    op = str(declared.get("op") or "all")
    if op not in planmod.CONDITION_KINDS:
        raise PlanCompileError(f"不支持的 condition.op：{op!r}")
    terms: list[dict[str, Any]] = []
    for term in declared.get("terms") or []:
        if not isinstance(term, dict):
            raise PlanCompileError("condition term 必须是对象")
        kind = term.get("kind")
        if kind not in planmod.CONDITION_TERM_KINDS:
            raise PlanCompileError(f"不支持的 condition term：{kind!r}")
        if kind == "code":
            terms.append({"kind": "code", "code": str(term.get("code") or "")})
            continue
        key = str(term.get("key") or "")
        if kind in ("control_true", "control_equals", "control_count") and key not in control_keys:
            raise PlanCompileError(f"condition 引用了未声明控制项：{key!r}")
        if kind == "target_true" and key not in TARGET_KEYS:
            raise PlanCompileError(f"condition 引用了未知目标键：{key!r}")
        payload = {"kind": kind, "key": key}
        if kind == "control_equals":
            payload["value"] = term.get("value")
        terms.append(payload)
    return op, terms


def _merge_mechanisms(
    template: dict[str, Any],
    mechanisms: dict[str, Any],
    *,
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    """把共享机制库中不依赖实例绑定的条目合并进原生定义。"""
    merged: dict[str, Any] = {"crit": {}, "every_nth_hit": None, "state_scalings": [], "effects": []}
    library = (mechanisms or {}).get("mechanisms") or {}
    for ref in template.get("mechanism_refs") or []:
        mechanism_id = str(ref)
        entry = library.get(mechanism_id)
        if not isinstance(entry, dict):
            diagnostics.append(
                {
                    "code": "unknown_mechanism_ref",
                    "message": f"机制引用不存在：{mechanism_id}",
                    "severity": "error",
                    "related_ids": [mechanism_id],
                    "affects_coverage": False,
                }
            )
            continue
        for key in ("target_stack_chance", "crit_deadly"):
            declared = entry.get(key)
            if not declared:
                continue
            if key == "target_stack_chance":
                merged["crit"]["target_stack_chance"] = copy.deepcopy(declared)
            else:
                merged["crit"]["deadly"] = copy.deepcopy(declared)
        if entry.get("every_nth_hit"):
            merged["every_nth_hit"] = copy.deepcopy(entry["every_nth_hit"])
        for declared in entry.get("state_scalings") or []:
            merged["state_scalings"].append(copy.deepcopy(declared))
        for declared in entry.get("effects") or []:
            source = declared.get("source") or {}
            if source.get("kind") in ("scenario_assumption", "state_source"):
                merged["effects"].append(copy.deepcopy(declared))
            else:
                diagnostics.append(
                    {
                        "code": "mechanism_requires_instance_binding",
                        "message": (
                            f"机制 {mechanism_id} 的 effect 需要 current:matched_instances 绑定；"
                            "请使用 profile-v2 适配器编译该构筑"
                        ),
                        "severity": "error",
                        "related_ids": [mechanism_id],
                        "affects_coverage": False,
                    }
                )
        for key in ("node_bindings", "movement_conversion", "damage_to_penetration", "cooldown_reduction"):
            if entry.get(key):
                diagnostics.append(
                    {
                        "code": "mechanism_requires_catalog_binding",
                        "message": f"机制 {mechanism_id} 的 {key} 需要当前输入/目录绑定；原生定义入口不支持",
                        "severity": "error",
                        "related_ids": [mechanism_id],
                        "affects_coverage": False,
                    }
                )
    return merged


def compile_from_definitions(
    context: FrozenContext,
    template: dict[str, Any],
    mechanisms: Optional[dict[str, Any]] = None,
    rules: Optional[dict[str, Any]] = None,
) -> CompileResult:
    """原生技能模板（``le-skill-template-v2``）→ 统一计算计划。"""
    diagnostics: list[dict[str, Any]] = []

    def reject(code: str, message: str) -> CompileResult:
        diagnostics.append(
            {
                "code": code,
                "message": message,
                "severity": "error",
                "related_ids": ["template"],
                "affects_coverage": False,
            }
        )
        return CompileResult("rejected", "blocked", None, diagnostics)

    if not isinstance(template, dict):
        return reject("invalid_template", "技能模板必须是 JSON 对象")
    if template.get("schema") != TEMPLATE_SCHEMA:
        return reject("invalid_template", f"技能模板 schema 必须为 {TEMPLATE_SCHEMA}")
    unknown = set(template) - set(TEMPLATE_FIELDS)
    if unknown:
        return reject("unknown_template_field", f"技能模板含未支持字段：{sorted(unknown)}")
    if not template.get("template_id") or not template.get("ability_id"):
        return reject("invalid_template", "技能模板缺少 template_id/ability_id")
    skill = template.get("skill")
    if not isinstance(skill, dict):
        return reject("invalid_template", "技能模板缺少 skill 对象")
    base_damage = skill.get("base_damage")
    if not isinstance(base_damage, dict) or not base_damage:
        return reject("invalid_template", "skill.base_damage 必须是非空对象")
    for damage_type, value in base_damage.items():
        if not isinstance(damage_type, str) or not isinstance(value, (int, float)):
            return reject("invalid_template", "skill.base_damage 必须是 伤害类型→数值 映射")
    components_spec = skill.get("components")
    if not isinstance(components_spec, dict) or "hit" not in components_spec or "dot" not in components_spec:
        return reject("invalid_template", "skill.components 必须声明 hit 与 dot")
    for kind in ("hit", "dot"):
        spec = components_spec[kind]
        if not isinstance(spec, dict) or not isinstance(spec.get("tags"), list):
            return reject("invalid_template", f"skill.components.{kind}.tags 必须是数组")
    control_keys = {str(control.get("key")) for control in template.get("controls") or []}
    merged = _merge_mechanisms(template, mechanisms or {}, diagnostics=diagnostics)
    template_effects = list(template.get("effects") or []) + merged["effects"]
    conditions: list[dict[str, Any]] = []
    effects: list[dict[str, Any]] = []
    for index, declared in enumerate(template_effects):
        if not isinstance(declared, dict):
            return reject("invalid_template_effect", f"effects[{index}] 必须是对象")
        unknown_effect = set(declared) - set(TEMPLATE_EFFECT_KEYS)
        if unknown_effect:
            return reject("unknown_effect_field", f"effects[{index}] 含未支持字段：{sorted(unknown_effect)}")
        effect_id = str(declared.get("effect_id") or "")
        stat_key = str(declared.get("stat_key") or "")
        operation = str(declared.get("operation") or "")
        if not effect_id or not stat_key or operation not in ("added", "increased", "more", "base", "flag"):
            return reject("invalid_template_effect", f"effects[{index}] 的 effect_id/stat_key/operation 不合法")
        value = declared.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return reject("invalid_template_effect", f"effects[{index}].value 必须是有限数值")
        try:
            condition_op, condition_terms = _template_condition(declared.get("condition"), control_keys)
        except PlanCompileError as exc:
            return reject("invalid_condition", str(exc))
        condition_ref = None
        if condition_terms or condition_op != "always":
            payload = {"op": condition_op, "terms": condition_terms}
            digest = planmod.sha256_text(planmod.canonical_json(payload))[:16]
            condition_ref = f"cond:{digest}"
            if not any(condition["condition_id"] == condition_ref for condition in conditions):
                conditions.append({"condition_id": condition_ref, **payload})
        source = declared.get("source") or {}
        unit = str(declared.get("unit") or ("percent" if operation in ("increased", "more") else "flat"))
        if unit not in ("flat", "percent", "seconds", "flag"):
            return reject("invalid_unit", f"effects[{index}].unit 不受支持：{unit!r}")
        effects.append(
            {
                "effect_id": effect_id,
                "contribution_id": effect_id,
                "sequence": index,
                "label": str(declared.get("label") or effect_id),
                "category": "effect",
                "stage": "effect",
                "handler_id": str(declared.get("handler_id") or "template_effect-v1"),
                "handler_version": plan_handlers.VERSION,
                "source_kind": str(source.get("kind") or "template_constant"),
                "source_label": str(source.get("label") or declared.get("label") or effect_id),
                "source_path": str(source.get("path") or f"template:{template.get('template_id')}#{effect_id}"),
                "stat_key": stat_key,
                "stat_name": stat_key,
                "operation": operation,
                "value": float(value),
                "unit": unit,
                "damage_tags": list(declared.get("damage_tags") or []),
                "target": declared.get("target"),
                "group": declared.get("group") or effect_id,
                "value_source": "template_constant",
                "status": str(declared.get("status") or "assumed"),
                "notes": declared.get("notes"),
                "included_static": True,
                "condition_status_static": "always",
                "exclusion_reason": None,
                "condition_ref": condition_ref,
                "condition_code": None,
                "coverage_class": None,
                "coverage_status": "template_constant",
                "required": True,
                "emits_contribution": True,
                "value_mode": "static",
                "value_params": {},
                "ledger_kind": "effects",
                "ledger": {
                    "effect_id": effect_id,
                    "label": str(declared.get("label") or effect_id),
                    "status": "applied",
                    "reason": None,
                    "contribution_id": effect_id,
                    "source": source,
                    "payload": {"declared_value": float(value)},
                    "check": None,
                },
                "stage": plan_handlers.CATEGORY_STAGE.get("effect", "derived_pools"),
                "source_refs": [f"template_source:{effect_id}"],
                "ownership_key": {
                    "source": {
                        "kind": "template_constant",
                        "source_path": str(
                            source.get("path")
                            or f"template:{template.get('template_id')}#{effect_id}"
                        ),
                    },
                    "stat_key": stat_key,
                    "operation": operation,
                    "damage_tags": sorted(str(tag) for tag in (declared.get("damage_tags") or [])),
                    "target": declared.get("target"),
                    "group": declared.get("group") or effect_id,
                },
                "required": True,
                "support_status": "template_constant",
                "owner": None,
                "source_facts": source,
                "raw": {"effect_id": effect_id, "template_id": template.get("template_id")},
                "evidence": [],
            }
        )
        if unit not in ("flat", "percent", "seconds", "flag"):
            return reject("invalid_unit", f"effects[{index}].unit 不受支持：{unit!r}")
    state_scaling_effects: list[dict[str, Any]] = []
    for declared in merged["state_scalings"]:
        rule_id = str(declared.get("id") or "")
        output = declared.get("output") or {}
        if not rule_id or not output.get("stat"):
            return reject("invalid_mechanism", f"state_scaling 规则不完整：{rule_id!r}")
        state_scaling_effects.append(
            {
                "effect_id": f"rule:{rule_id}",
                "contribution_id": f"rule:{rule_id}",
                "sequence": len(effects) + len(state_scaling_effects),
                "label": str(declared.get("label") or rule_id),
                "category": "state_scaling",
                "stage": "state",
                "handler_id": "state_scaling-v1",
                "handler_version": plan_handlers.VERSION,
                "source_kind": "mechanism",
                "source_label": rule_id,
                "source_path": f"mechanism:{rule_id}",
                "stat_key": str(output.get("stat")),
                "stat_name": str(output.get("stat")),
                "operation": str(output.get("operation") or "increased"),
                "value": 0.0,
                "unit": "percent",
                "damage_tags": list(output.get("tags") or []),
                "target": None,
                "group": rule_id,
                "value_source": "mechanism",
                "status": "assumed",
                "notes": None,
                "included_static": True,
                "condition_status_static": "always",
                "exclusion_reason": None,
                "condition_ref": None,
                "condition_code": None,
                "coverage_class": None,
                "coverage_status": "template_constant",
                "required": True,
                "emits_contribution": True,
                "value_mode": "state_count",
                "value_params": {
                    "per_state": float(declared.get("per_state") or 0.0),
                    "scaling_sum": 0.0,
                    "states": [str(key) for key in declared.get("states") or []],
                },
                "ledger_kind": "state_scalings",
                "ledger": {
                    "effect_id": rule_id,
                    "label": str(declared.get("label") or rule_id),
                    "status": "applied",
                    "reason": None,
                    "contribution_id": f"rule:{rule_id}",
                    "source": {"states": [str(key) for key in declared.get("states") or []]},
                    "payload": {"scaling_sum": 0.0},
                    "check": None,
                },
                "stage": "derived_pools",
                "source_refs": [f"effect:{rule_id}"],
                "ownership_key": {
                    "source": {"kind": "rule_source", "mechanism_id": rule_id},
                    "stat_key": str(output.get("stat")),
                    "operation": str(output.get("operation") or "increased"),
                    "damage_tags": sorted(str(tag) for tag in (output.get("tags") or [])),
                    "target": None,
                    "group": rule_id,
                },
                "required": True,
                "support_status": "template_constant",
                "owner": None,
                "source_facts": {"kind": "mechanism", "mechanism_id": rule_id},
                "raw": {"rule_id": rule_id},
                "evidence": [],
            }
        )
    effects.extend(state_scaling_effects)
    # 同所有权：同语义合并来源；不同定义明确冲突并阻止编译（不保留首/末条）。
    merged_effects: list[dict[str, Any]] = []
    seen_ownership: dict[str, dict[str, Any]] = {}
    for effect in effects:
        key = planmod.canonical_json(effect.get("ownership_key"))
        existing = seen_ownership.get(key)
        if existing is not None:
            if _effect_semantic_signature(existing) != _effect_semantic_signature(effect):
                diagnostics.append(
                    {
                        "code": "ownership_conflict",
                        "message": (
                            f"同一所有权键出现不同定义：{effect['effect_id']} 与 "
                            f"{existing['effect_id']}（值 {existing.get('value')!r} vs "
                            f"{effect.get('value')!r}，条件 {existing.get('condition_ref')!r} vs "
                            f"{effect.get('condition_ref')!r}）；不能保留首条或末条，必须修正定义"
                        ),
                        "severity": "error",
                        "related_ids": [effect["effect_id"], existing["effect_id"]],
                        "affects_coverage": False,
                    }
                )
                continue
            merged_refs = list(
                dict.fromkeys((existing.get("source_refs") or []) + (effect.get("source_refs") or []))
            )
            existing["source_refs"] = merged_refs
            merged_evidence = list(existing.get("evidence") or [])
            for item in effect.get("evidence") or []:
                if item not in merged_evidence:
                    merged_evidence.append(item)
            existing["evidence"] = merged_evidence
            diagnostics.append(
                {
                    "code": "duplicate_merged",
                    "message": (
                        f"同所有权同语义重复已合并来源：{effect['effect_id']} → "
                        f"{existing['effect_id']}"
                    ),
                    "severity": "info",
                    "related_ids": [effect["effect_id"], existing["effect_id"]],
                    "affects_coverage": False,
                }
            )
            continue
        seen_ownership[key] = effect
        merged_effects.append(effect)
    effects = merged_effects
    title = _template_title(template)
    diagnostics_before = list(diagnostics)
    blocked = any(item.get("severity") == "error" for item in diagnostics_before)
    if blocked:
        return CompileResult("rejected", "blocked", None, diagnostics)
    crit = dict((template.get("rules") or {}).get("crit") or {})
    for key, value in merged["crit"].items():
        crit.setdefault(key, value)
    every_nth = (template.get("rules") or {}).get("every_nth_hit") or merged["every_nth_hit"]
    if every_nth is not None:
        if not isinstance(every_nth, dict):
            return reject("invalid_rule_parameter", "every_nth_hit 必须是对象")
        n = every_nth.get("n")
        extra_more = every_nth.get("extra_more", 0.0)
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            return reject("invalid_rule_parameter", "every_nth_hit.n 必须是 >=1 的整数")
        if str(every_nth.get("counting") or "hit") != "hit":
            return reject("invalid_rule_parameter", "every_nth_hit.counting 只支持 hit")
        if str(every_nth.get("phase") or "from_first_hit") != "from_first_hit":
            return reject("invalid_rule_parameter", "every_nth_hit.phase 只支持 from_first_hit")
        if isinstance(extra_more, bool) or not isinstance(extra_more, (int, float)) or not math.isfinite(float(extra_more)):
            return reject("invalid_rule_parameter", "every_nth_hit.extra_more 必须是有限数值")
        every_nth = {
            "n": n,
            "extra_more": float(extra_more),
            "counting": "hit",
            "phase": "from_first_hit",
        }
    schedule_declared = template.get("schedule") or {}
    windows = schedule_declared.get("windows_s") or list(SCHEDULE_WINDOWS)
    if not isinstance(windows, list) or not windows or any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 for value in windows
    ):
        return reject("invalid_schedule", "schedule.windows_s 必须是正数数组")
    ailments = []
    for declared in template.get("ailments") or []:
        if not isinstance(declared, dict) or not declared.get("name"):
            return reject("invalid_ailment", "ailments 条目必须含 name")
        entry = copy.deepcopy(declared)
        entry.setdefault("duration_s", 1.0)
        entry.setdefault("base_damage", 0.0)
        entry.setdefault("chance", {"source_kinds": ["equipment"], "excluded_tags": [], "additional": []})
        ailments.append(entry)
    parameters = {
        "skill": {
            "base_damage": copy.deepcopy(base_damage),
            "added_damage_scaling": float(skill.get("added_damage_scaling") or 0.0),
            "crit_chance": float(skill.get("crit_chance") or 0.0),
            "crit_multiplier": float(skill.get("crit_multiplier") or 0.0),
            "cooldown_s": float(skill.get("cooldown_s") or 0.0),
            "action_s": float(skill.get("action_s") or 0.0),
            "skill_name": title,
        },
        "crit": crit,
        "ailments": ailments,
        "additional_chance": {},
        "damage_to_penetration": None,
        "cooldown_reduction": 0.0,
        "attribute_ratios": {},
        "attribute_conversion": None,
    }
    components_entries = [
        {
            "component_id": f"{kind}:declared",
            "kind": kind,
            "tags": list((components_spec.get(kind) or {}).get("tags") or []),
            "crit_capable": bool((components_spec.get(kind) or {}).get("crit_capable")),
            "adaptive_damage_type": (components_spec.get(kind) or {}).get("adaptive_damage_type"),
        }
        for kind in ("hit", "dot")
    ]
    sources_by_id: dict[str, dict[str, Any]] = {}
    for effect in effects:
        for source_id in effect.get("source_refs") or []:
            identity = (
                {"template_id": template.get("template_id"), "effect_id": effect["effect_id"]}
                if source_id.startswith("template_source:")
                else {"mechanism_id": source_id.split(":", 1)[1]}
            )
            record = sources_by_id.setdefault(
                source_id,
                {
                    "source_id": source_id,
                    "kind": "template_constant",
                    "identity": identity,
                    "semantics": {"stat_keys": [], "binding_status": "template_constant"},
                    "binding_status": "template_constant",
                    "evidence": [],
                },
            )
            if effect["stat_key"] not in record["semantics"]["stat_keys"]:
                record["semantics"]["stat_keys"].append(effect["stat_key"])
    sources = sorted(sources_by_id.values(), key=lambda item: item["source_id"])
    character = {
        "name": "synthetic",
        "level": 1,
        "class": {"entity_id": "synthetic:class", "name": "Synthetic"},
        "mastery": {"name": "Synthetic"},
    }
    limitations = [str(item) for item in template.get("limitations") or []]
    assumptions = [
        {
            "assumption_id": str(item.get("assumption_id") or f"template:{index}"),
            "status": str(item.get("status") or "assumed"),
            "kind": "assumption",
            "scope": "result",
            "note": str(item.get("note") or ""),
        }
        for index, item in enumerate(template.get("assumptions") or [])
    ]
    for index, note in enumerate(limitations):
        assumptions.append(
            {
                "assumption_id": f"limitation:{index}",
                "status": "assumed",
                "kind": "limitation",
                "scope": "result",
                "note": note,
            }
        )
    fingerprint = plan_handlers.implementation_fingerprint()
    context_case_id = str((context.case or {}).get("case_id") or "").strip()
    plan_payload: dict[str, Any] = {
        "schema": planmod.SCHEMA,
        "subject": {
            "case_id": str(template.get("case_id") or context_case_id or template.get("template_id")),
            "build_id": str(template.get("build_id") or f"template:{template.get('template_id')}"),
            "revision_id": str(template.get("revision_id") or "rev-1"),
            "main_ability_id": str(template.get("ability_id")),
        },
        "compiler": {
            "version": COMPILER_VERSION,
            "origin": "native_template",
            "implementation_fingerprint": fingerprint["compiler_files"],
            "fingerprint": fingerprint["compiler_fingerprint"],
        },
        "executor_requirements": {
            "handlers": plan_handlers.handler_versions("execute"),
            "executor_fingerprint": fingerprint["executor_fingerprint"],
        },
        "input_refs": copy.deepcopy(context.input_refs),
        "sources": sources,
        "facts": {
            "character": character,
            "skill_name": title,
            "attributes": {},
            "attribute_conversion_info": None,
            "equipment_facts": {
                "weapons": [],
                "main_hand": None,
                "off_hand_weapon": None,
                "off_hand_item": None,
                "shield": None,
                "dual_wield": False,
                "weapon_type_ids": [],
                "weapon_type_unknown": False,
            },
            "skill_constants": {"status": "verified", "observed": {}, "differences": []},
            "ailment_constants": {"status": "verified", "observed": {}, "differences": []},
            "coverage_decisions": [],
            "attribute_views": {"original": {}, "converted": {}, "effective": {}},
            "coverage_base_gaps": [],
            "early_issues": [],
            "post_assembly_issues": [],
            "duplicate_effects": [],
            "duplicate_issues": [],
            "per_attribute_problems": [],
        },
        "parameters": parameters,
        "controls": _template_control_entries(template),
        "conditions": conditions,
        "components": components_entries,
        "effects": effects,
        "evaluation": {
            "stage_order": list(plan_handlers.STAGE_ORDER),
            "effect_stage": {effect["effect_id"]: effect["stage"] for effect in effects},
            "dependencies": plan_handlers.canonical_dependencies(),
            "ownership": "unique_by_ownership_key",
        },
        "schedule": {
            "driver": "fixed_repeat",
            "interval_policy": "max(action_time, cooldown)",
            "first_cast_s": float(schedule_declared.get("first_cast_s") or 0.0),
            "windows_s": [float(value) for value in windows],
            "every_nth_hit": every_nth,
        },
        "diagnostics": diagnostics,
        "assumptions": assumptions,
        "scope": {
            "skill": title,
            "ailment_labels": [
                {"name": item.get("name"), "label_zh": item.get("label_zh") or item.get("name")}
                for item in ailments
            ],
            "limitations": limitations,
        },
        "presentation": {
            "skill_name": title,
            "skill_names": {str(template.get("ability_id")): title},
            "character": character,
            "equipment": [],
            "controls": [
                {
                    "key": str(control.get("key")),
                    "label": control.get("label") or str(control.get("key")),
                    "source": control.get("source") or "template",
                    "type": control.get("type") or "boolean",
                    "default": control.get("default"),
                    "min": control.get("min"),
                    "max": control.get("max"),
                    "readonly": bool(control.get("readonly")),
                    "effect": control.get("effect") or "合成定义控件",
                }
                for control in template.get("controls") or []
                if isinstance(control, dict) and control.get("key")
            ],
        },
    }
    plan = planmod.finalize_plan(plan_payload)
    return CompileResult("completed", "ready", plan, diagnostics)


def _component_entries(profile: dict[str, Any]) -> list[dict[str, Any]]:
    components_spec = (profile.get("skill") or {}).get("components") or {}
    entries: list[dict[str, Any]] = []
    for kind in ("hit", "dot"):
        if kind not in components_spec:
            # v4-actor 允许 hit-only；未声明的组件不生成占位行（0 dot 由执行层正常处理）。
            continue
        spec = components_spec.get(kind) or {}
        entries.append(
            {
                "component_id": f"{kind}:declared",
                "kind": kind,
                "tags": list(spec.get("tags") or []),
                "crit_capable": bool(spec.get("crit_capable")),
                "adaptive_damage_type": spec.get("adaptive_damage_type"),
            }
        )
    if not entries:
        raise PlanCompileError("definition.skill.components 至少需要一个 hit 组件")
    return entries
