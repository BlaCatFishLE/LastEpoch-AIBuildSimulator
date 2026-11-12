"""通用 actor 原生装配（``assembly_plan_version="v4-actor"``）。

把模板声明（``definition.actor`` / ``definition.frequency`` /
``rules.stat_transfer`` / ``rules.attribute_scaling(output.scope=actor)`` /
``rules.node_attribute_scaled``）在**编译期**转成 legacy-v1 形状的 Contribution 行与
facts，之后复用既有 ``_build_execution_v1`` → ``_execute_native_v4`` 执行链。

约束：
* 不按技能名/ability_id 分支；所有数值来自声明 + dataset 节点/ minion 实体；
* 运行期不回读 dataset/规则（派生值全部落盘到 plan/execution_v1，由 execution_sha256 绑定）；
* 只在该 assembly 版本启用，v1/v2/v3 行为不变。
"""

from __future__ import annotations

import copy
import math
from typing import Any, Mapping, Optional

from . import actor_pool, model, stats
from .mechanics.tag_match import UNKNOWN_RESTRICT, TagPolicy, tags_match

ACTOR_ASSEMBLY_VERSION = "v4-actor"
#: owner 行复制为 actor 行时写入 raw.origin 的稳定标记
NATIVE_ORIGIN = "native_minion_stat"

_ATTRIBUTE_SCALE_POLICY = TagPolicy(
    ignored_tags=frozenset({"Elemental"}),
    known_tags=frozenset(),
    unknown_tag_policy=UNKNOWN_RESTRICT,
    name="actor-attribute-scale",
)


class ActorCompileError(ValueError):
    """actor 原生装配失败（声明/来源/dataset 不一致）。"""


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ActorCompileError(f"{label} 必须是有限数值")
    return float(value)


def _actor_short(actor_id: str) -> str:
    return str(actor_id).rsplit(":", 1)[-1]


def _meta(handler_id: str, rule_id: str, *, source_facts: dict[str, Any], label: str) -> dict[str, Any]:
    return {
        "category": "effect",
        "stage": "derived_pools",
        "handler_id": handler_id,
        "included_static": True,
        "value_mode": "static",
        "value_params": {},
        "coverage_status": "mapped",
        "rule_id": rule_id,
        "ledger_kind": "effects",
        "ledger": None,
        "emits_contribution": True,
        "required": True,
        "source_facts": source_facts,
        "label": label,
    }


def _resolve_node_value(
    definition: Mapping[str, Any],
    catalog: Any,
    calc: Optional[Mapping[str, Any]],
    *,
    tree: str,
    node: str,
    stat_index: int,
    label: str,
) -> tuple[float, int, dict[str, Any]]:
    """从 dataset 节点读取每点值 × 当前点数；返回 (value, points, facts)。"""
    if catalog is None or calc is None:
        raise ActorCompileError(f"{label} 需要 calc/catalog 解析 skill_node 点数与目录每点值")
    from . import effect_binding as binding  # noqa: PLC0415

    try:
        value, points, check = binding.bind_node_value(
            dict(calc), catalog, tree_id=tree, node_id=node, stat_index=int(stat_index)
        )
    except Exception as exc:  # noqa: BLE001 - 统一转成装配错误
        raise ActorCompileError(f"{label} 节点绑定失败：{exc}") from exc
    if not check.ok:
        raise ActorCompileError(f"{label} 节点不可用：{check.detail}")
    facts = dict(check.facts or {})
    return float(value or 0.0), int(points), {"tree": tree, "node": node, "stat_index": int(stat_index), **facts}


def _dataset_actor_facts(actor: Mapping[str, Any], catalog: Any) -> dict[str, Any]:
    """硬校验 actor 的 dataset minion 身份 / base_effects / damage_source（只读目录）。

    约束（失败即 ``ActorCompileError``，不允许“没有事实就不写事实”）：
    * catalog 缺失 → 失败（不能在没有目录证据时继续）；
    * ``actor_id`` 与 ``source_entity`` 都必须解析到同一个 dataset minion 实体；
    * ``source=="dataset:minion.base_stats"`` 的 base_effect 必须在该 minion 解码出的
      base stats 中存在同 stat/operation/value 项；结构无法解析也明确失败；
    * ``damage_source.ability_entity`` 必须有 DamageEnemyOnHit 组件，目录异常不吞。
    """
    actor_id = str(actor["actor_id"])
    source_entity = str(actor["source_entity"])
    facts: dict[str, Any] = {"actor_id": actor_id, "source_entity": source_entity}
    if catalog is None:
        raise ActorCompileError(
            "actor 装配需要 dataset catalog 校验 minion 身份/base stats；"
            "catalog 缺失，拒绝在无目录证据下继续"
        )
    resolved: dict[str, Any] = {}
    for label, entity_id in (("actor.source_entity", source_entity), ("actor.actor_id", actor_id)):
        try:
            minion = catalog.minion(entity_id)
        except Exception as exc:  # noqa: BLE001 - 目录异常必须阻断，不能吞
            raise ActorCompileError(f"{label}={entity_id!r} 查询 dataset.minions 失败：{exc}") from exc
        if minion is None:
            raise ActorCompileError(
                f"{label}={entity_id!r} 在 dataset.minions 中不存在；actor 身份未绑定，拒绝继续"
            )
        resolved[label] = minion
    source_minion = resolved["actor.source_entity"]
    actor_minion = resolved["actor.actor_id"]
    if str(source_minion.get("entity_id")) != str(actor_minion.get("entity_id")):
        raise ActorCompileError(
            f"actor.actor_id={actor_id!r} 与 actor.source_entity={source_entity!r} "
            f"解析到不同 dataset minion 实体："
            f"{actor_minion.get('entity_id')!r} vs {source_minion.get('entity_id')!r}"
        )
    minion = source_minion
    facts["dataset_minion"] = {
        "entity_id": minion.get("entity_id"),
        "minion_type": minion.get("minion_type"),
        "inherits": minion.get("inherits"),
        "locator": f"dataset.minions#{minion.get('entity_id')}",
    }
    try:
        base_effect_evidence = actor_pool.validate_actor_base_effects_against_dataset(actor, minion)
    except actor_pool.ActorPoolError as exc:
        raise ActorCompileError(
            f"actor base_effects 与 dataset minion {minion.get('entity_id')!r} 的 base stats 不一致：{exc}"
        ) from exc
    facts["dataset_base_effects"] = base_effect_evidence
    damage_source = actor.get("damage_source") or {}
    ability_entity = str(damage_source.get("ability_entity") or "")
    if ability_entity:
        try:
            components = catalog.skill_components(ability_entity)
        except Exception as exc:  # noqa: BLE001 - 目录异常必须阻断，不能吞成空组件列表
            raise ActorCompileError(
                f"actor.damage_source.ability_entity={ability_entity!r} 查询 dataset skill_components 失败：{exc}"
            ) from exc
        damage_component = next(
            (item for item in components if item.get("component_type") == "DamageEnemyOnHit"),
            None,
        )
        if damage_component is None:
            raise ActorCompileError(
                f"actor.damage_source.ability_entity={ability_entity!r} 在 dataset 中没有 DamageEnemyOnHit 组件"
            )
        facts["dataset_damage_source"] = {
            "ability_entity": ability_entity,
            "component_id": damage_component.get("component_id"),
            "base_damage": damage_component.get("base_damage"),
            "added_damage_effectiveness": damage_component.get("added_damage_effectiveness"),
            "locator": f"dataset.skill_components#{damage_component.get('component_id')}",
        }
    return facts


def _copy_owner_scope(rows: list[model.Contribution], actor_ids: list[str]) -> None:
    """把非 actor 行标 owner scope（未标记的行按 owner 处理，显式标记以便装配期过滤）。"""
    actor_set = {str(item) for item in actor_ids}
    for row in rows:
        raw = dict(row.raw or {})
        actor = str(raw.get("actor_id") or raw.get("owner") or "")
        if actor in actor_set:
            raw.setdefault("scope", actor_pool.ACTOR_SCOPE)
        else:
            raw.setdefault("scope", actor_pool.OWNER_SCOPE)
        row.raw = raw


def _native_stat_rows(
    rows: list[model.Contribution],
    actor_id: str,
    native_stats: Mapping[str, Any],
) -> list[model.Contribution]:
    short = _actor_short(actor_id)
    stat_keys = {str(item) for item in native_stats["stat_keys"]}
    tags_include = [str(item) for item in native_stats.get("tags_include") or []]
    output_tags = native_stats.get("output_tags")
    produced: list[model.Contribution] = []
    for row in rows:
        if str(row.stat_key) not in stat_keys:
            continue
        if tags_include and not set(tags_include) <= set(row.damage_tags):
            continue
        produced.append(
            model.Contribution(
                contribution_id=f"actor:{short}:native:{row.contribution_id}",
                source_kind="derived",
                source_label=f"minion native copy of {row.contribution_id}",
                source_path="definition.actor.native_stats",
                stat_name=row.stat_name,
                stat_key=row.stat_key,
                operation=row.operation,
                value=float(row.value),
                unit=row.unit,
                damage_tags=[str(item) for item in (output_tags if output_tags is not None else row.damage_tags)],
                target=row.target,
                condition_status=row.condition_status,
                group=f"actor:{actor_id}:native:{row.contribution_id}",
                included=bool(row.included),
                value_source="derived",
                status=model.STATUS_DERIVED,
                evidence=[
                    model.Evidence(
                        kind="derived",
                        detail=f"actor.native_stats 100% 复制 owner 行 {row.contribution_id}",
                        locator="definition.actor.native_stats",
                    )
                ],
                notes=native_stats.get("label"),
                raw={
                    "scope": actor_pool.ACTOR_SCOPE,
                    "actor_id": actor_id,
                    "owner": actor_id,
                    "origin": NATIVE_ORIGIN,
                    "source_contribution_id": row.contribution_id,
                },
            )
        )
    return produced


def _annotate_declared_actor_effects(
    rows: list[model.Contribution],
    definition: Mapping[str, Any],
    actor_id: str,
) -> list[model.Contribution]:
    """把模板 effects 中显式 scope=actor 的行改为 actor 池（不复制，改标记）。"""
    actor_effect_ids = {
        str(effect.get("id"))
        for effect in definition.get("effects") or []
        if isinstance(effect, dict) and str(effect.get("scope") or "") == actor_pool.ACTOR_SCOPE
    }
    converted: list[model.Contribution] = []
    if not actor_effect_ids:
        return converted
    for row in rows:
        raw = dict(row.raw or {})
        if str(raw.get("effect_id") or "") not in actor_effect_ids:
            continue
        raw["scope"] = actor_pool.ACTOR_SCOPE
        raw["actor_id"] = actor_id
        raw["owner"] = actor_id
        raw["origin"] = "declared_effect_actor"
        row.raw = raw
        row.group = f"actor:{actor_id}:effect:{raw.get('effect_id')}"
        converted.append(row)
    return converted


def _annotate_attribute_scaling(
    rows: list[model.Contribution],
    definition: Mapping[str, Any],
    actor_id: str,
) -> Optional[model.Contribution]:
    declared = (definition.get("rules") or {}).get("attribute_scaling") or {}
    output = declared.get("output") or {}
    if str(output.get("scope") or "") != actor_pool.ACTOR_SCOPE:
        return None
    for row in rows:
        if row.contribution_id != "rule:attribute_scaling":
            continue
        raw = dict(row.raw or {})
        raw["scope"] = actor_pool.ACTOR_SCOPE
        raw["actor_id"] = actor_id
        raw["owner"] = actor_id
        raw["origin"] = "attribute_scaling_actor"
        row.raw = raw
        row.group = f"actor:{actor_id}:attribute_scaling"
        return row
    return None


def _resolve_transfer_rule(
    rule: Mapping[str, Any],
    calculator: Mapping[str, Any],
    catalog: Any,
    calc: Optional[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """把节点来源的转移转成 ratio（每点值×点数），其余原样交给 actor_pool。"""
    normalized = actor_pool.validate_stat_transfer_rule(copy.deepcopy(rule))
    resolved = copy.deepcopy(normalized)
    facts: list[dict[str, Any]] = []
    for transfer in resolved["transfers"]:
        source = transfer.get("source") or {}
        if str(source.get("kind") or "") != "skill_node":
            continue
        value, points, node_facts = _resolve_node_value(
            calculator,
            catalog,
            calc,
            tree=str(source.get("tree") or ""),
            node=str(source.get("node") or ""),
            stat_index=int(source.get("stat_index") or 0),
            label=f"stat_transfer {transfer['id']}",
        )
        per_point = transfer.get("ratio_per_point")
        if per_point is not None:
            # 模板声明每点比例：ratio = 每点比例 × 当前点数（点数来自专精绑定，不读场景）。
            transfer["ratio"] = float(per_point) * float(points)
        else:
            # 未声明每点比例：直接使用节点每点值 × 点数（dataset 数值）。
            transfer["ratio"] = value
        transfer["ratio_per_point"] = None
        transfer["points_control"] = None
        facts.append({"transfer_id": transfer["id"], "points": points, **node_facts})
    return resolved, facts


def _node_attribute_rows(
    declared: Any,
    actor_id: str,
    attributes: Mapping[str, float],
    catalog: Any,
    calc: Optional[Mapping[str, Any]],
    definition: Mapping[str, Any],
) -> tuple[list[model.Contribution], list[dict[str, Any]]]:
    rules = actor_pool.validate_node_attribute_scaled_rule(declared)
    short = _actor_short(actor_id)
    rows: list[model.Contribution] = []
    details: list[dict[str, Any]] = []
    for rule in rules:
        source = rule["source"]
        value_per_point, points, facts = _resolve_node_value(
            definition,
            catalog,
            calc,
            tree=source["tree"],
            node=source["node"],
            stat_index=source["stat_index"],
            label=f"node_attribute_scaled {rule['id']}",
        )
        attribute_value = float(attributes.get(str(rule["attribute"]), 0.0))
        value = value_per_point * attribute_value
        output = rule["output"]
        operation = str(output["operation"])
        rows.append(
            model.Contribution(
                contribution_id=f"actor:{short}:node_attr:{rule['id']}",
                source_kind="derived",
                source_label=str(rule.get("label") or f"node_attribute_scaled {rule['id']}"),
                source_path="rules.node_attribute_scaled",
                stat_name=str(output["stat"]),
                stat_key=str(output["stat"]),
                operation=operation,
                value=value,
                unit="percent" if operation in ("increased", "more") else "flat",
                damage_tags=[str(item) for item in output["tags"]],
                group=f"actor:{actor_id}:node_attr:{rule['id']}",
                included=True,
                value_source="derived",
                status=model.STATUS_DERIVED,
                evidence=[
                    model.Evidence(
                        kind="derived",
                        detail=(
                            f"节点每点 {value_per_point:.6g} × {points} 点 × "
                            f"{rule['attribute']}={attribute_value:g}"
                        ),
                        locator="rules.node_attribute_scaled",
                    )
                ],
                raw={
                    "scope": actor_pool.ACTOR_SCOPE,
                    "actor_id": actor_id,
                    "owner": actor_id,
                    "origin": "node_attribute_scaled",
                    "points": points,
                    "attribute": rule["attribute"],
                    "attribute_value": attribute_value,
                    "node": dict(source),
                },
            )
        )
        details.append(
            {
                "id": rule["id"],
                "points": points,
                "per_point": value_per_point / points if points else 0.0,
                "attribute": rule["attribute"],
                "attribute_value": attribute_value,
                "value": value,
                **facts,
            }
        )
    return rows, details


def _frequency_specialization_points(
    calc: Optional[Mapping[str, Any]],
    *,
    tree: str,
    node: str,
) -> Optional[tuple[bool, int]]:
    """当前输入专精中 ``tree:node`` 的 ``(节点是否存在, 点数)``；专精缺失返回 ``None``。

    只读当前输入的 skills.specializations，不回退场景控件。
    """
    if not isinstance(calc, Mapping):
        return None
    for spec in (calc.get("skills") or {}).get("specializations") or []:
        entity = str((spec.get("tree") or {}).get("entity_id") or "")
        if not entity.endswith(":" + str(tree)):
            continue
        for entry in spec.get("nodes") or []:
            if str(entry.get("node_id")) == str(node):
                return True, int(entry.get("points") or 0)
        return False, 0
    return None


def resolve_derived_frequency(
    frequency: Mapping[str, Any],
    *,
    catalog: Any = None,
    calc: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """编译期把 gate 的 skill_node points 来源解析成运行期静态 probability。

    硬约束（P1-B1）：
    * 真实节点点数只来自当前输入专精；``points=0`` / 专精缺失 / 目录缺失都不得回退场景点数；
    * ``points=0`` 明确报“当前输入无召唤来源（点数=0），拒绝以场景点数补回”；
    * ``catalog``/``calc`` 缺失且 gate 是 skill_node 时直接失败，不留给运行期；
    * 编译后 gate 只保留 ``probability``/``points``/``source`` 证据，删除
      ``points_control``/``per_point`` 的运行期回退语义。
    """
    resolved = copy.deepcopy(dict(frequency))
    derivation = resolved.get("derivation")
    if not isinstance(derivation, dict):
        raise ActorCompileError("definition.frequency.derivation 缺失")
    gate = derivation.get("gate")
    if not isinstance(gate, dict):
        raise ActorCompileError("definition.frequency.derivation.gate 缺失")
    source = gate.get("source") or {}
    if not isinstance(source, Mapping):
        raise ActorCompileError("definition.frequency.derivation.gate.source 必须是对象")
    if str(source.get("kind") or "") != "skill_node":
        return resolved
    tree = str(source.get("tree") or "").strip()
    node = str(source.get("node") or "").strip()
    if not tree or not node:
        raise ActorCompileError(
            "definition.frequency.derivation.gate.source 缺少 tree/node；"
            "节点点数来源无法绑定，不能留给运行期"
        )
    stat_index = int(source.get("stat_index") or 0)
    if catalog is None or calc is None:
        raise ActorCompileError(
            f"definition.frequency.derivation.gate 依赖 skill_node {tree}:{node}，"
            "但编译期没有 calc/catalog；真实节点点数必须从当前输入专精绑定，不能留给运行期"
        )
    points_info = _frequency_specialization_points(calc, tree=tree, node=node)
    if points_info is None:
        raise ActorCompileError(
            f"definition.frequency.derivation.gate 依赖 skill_node {tree}:{node}，"
            f"但当前输入没有 {tree} 专精（未选择）；不能回退场景点数，拒绝编译"
        )
    node_present, points = points_info
    if not node_present:
        raise ActorCompileError(
            f"definition.frequency.derivation.gate 依赖 skill_node {tree}:{node}，"
            f"但当前输入 {tree} 专精中没有该节点（节点缺失/未分配）；不能回退场景点数，拒绝编译"
        )
    if points <= 0:
        raise ActorCompileError(
            f"当前输入无召唤来源（{tree} 节点 {node} 点数=0），本口径不产生事件，"
            "拒绝以场景点数补回；真实节点点数只来自当前输入专精"
        )
    value, bound_points, facts = _resolve_node_value(
        {},
        catalog,
        calc,
        tree=tree,
        node=node,
        stat_index=stat_index,
        label="definition.frequency.gate",
    )
    if int(bound_points) != int(points):
        raise ActorCompileError(
            f"definition.frequency.gate 节点点数不一致：专精读取 {points}，"
            f"目录绑定返回 {bound_points}"
        )
    per_point = float(facts.get("per_point") or 0.0)
    declared_per_point = gate.get("per_point")
    if declared_per_point is not None:
        declared = _finite(declared_per_point, "definition.frequency.derivation.gate.per_point")
        if not math.isclose(declared, per_point, rel_tol=1e-9, abs_tol=1e-12):
            raise ActorCompileError(
                f"definition.frequency.derivation.gate.per_point={declared:g} "
                f"与 dataset 每点值 {per_point:g} 不一致（{tree}:{node} stat_index={stat_index}）"
            )
    probability = min(1.0, value)
    gate.pop("points_control", None)
    gate.pop("per_point", None)
    gate["probability"] = probability
    gate["points"] = int(points)
    gate["source"] = {
        "kind": "skill_node",
        "tree": tree,
        "node": node,
        "stat_index": stat_index,
    }
    evidence = [
        item
        for item in (gate.get("evidence") or [])
        if not (
            isinstance(item, Mapping)
            and str(item.get("locator") or "").startswith(f"dataset.skill_nodes#{tree}:{node}")
        )
    ]
    evidence.append(
        {
            "kind": "derived",
            "detail": (
                f"当前输入 {tree} 节点 {node} points={points}；dataset 每点 "
                f"{per_point:g} → gate_probability={probability:g}"
            ),
            "locator": f"dataset.skill_nodes#{tree}:{node}:stat_index={stat_index}",
        }
    )
    gate["evidence"] = evidence
    return resolved


def compile_actor_model(
    rows: list[model.Contribution],
    resolved_model: Any,
    definition: Mapping[str, Any],
    catalog: Any = None,
    *,
    calc: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """在 rows 组装完成后、组件装配前，生成 actor 行与 actor_model facts。

    返回 ``{"row_metas": [(row, meta)...], "actor_model": {...}, "stat_transfers": [...],
    "frequency": {...}, "attributes": {...}}``；调用方负责把 rows 追加进装配器并写入 facts。

    暴击契约（P1-D）：**不在编译期吸收/覆盖 crit**。actor crit 行原样保留为 effect 行，
    执行期由 ``dps_components.build_hit_components`` 的 crit 池按
    active/included/运行时条件/伤害标签逐行结算；``parameters.skill`` 只保留模板声明的
    基础 crit 值。编译期因此不产生任何 crit 聚合镜像。
    """
    actor_declared = definition.get("actor")
    if not actor_declared:
        raise ActorCompileError("compile_actor_model 需要 definition.actor")
    try:
        actor = actor_pool.validate_actor_declaration(actor_declared)
    except actor_pool.ActorPoolError as exc:
        raise ActorCompileError(f"actor 声明不合法：{exc}") from exc
    actor_id = actor["actor_id"]
    short = _actor_short(actor_id)
    actor_facts = _dataset_actor_facts(actor, catalog)
    try:
        native_stats = actor_pool.validate_native_stats(actor_declared.get("native_stats"))
    except actor_pool.ActorPoolError as exc:
        raise ActorCompileError(f"actor.native_stats 声明不合法：{exc}") from exc
    attributes = stats.compute_attributes(rows)
    row_metas: list[tuple[model.Contribution, dict[str, Any]]] = []

    # 0) owner 行统一标 scope（actor 行沿用已有标记）
    _copy_owner_scope(rows, [actor_id])

    # 1) actor base effects（数值来自定义；dataset 已强制校验身份 + base stats 对齐）
    for base_row in actor_pool.build_actor_base_rows([actor]):
        raw = dict(base_row.raw or {})
        raw["dataset"] = copy.deepcopy(actor_facts)
        base_row.raw = raw
        row_metas.append((base_row, _meta("actor_base-v1", f"actor_base:{base_row.raw.get('base_effect_index')}", source_facts={"kind": "rule_source", "actor_id": actor_id, "locator": actor_facts.get("dataset_minion", {}).get("locator")}, label=base_row.source_label)))

    # 2) native_stats：owner 行 100% 复制为 actor 行
    if native_stats is not None:
        for native_row in _native_stat_rows(rows, actor_id, native_stats):
            row_metas.append((native_row, _meta("minion_native_stat-v1", f"native:{native_row.raw.get('source_contribution_id')}", source_facts={"kind": "rule_source", "actor_id": actor_id, "origin": NATIVE_ORIGIN}, label=native_row.source_label)))

    # 3) 模板 effects 中显式 scope=actor / attribute_scaling(scope=actor)
    for declared_row in _annotate_declared_actor_effects(rows, definition, actor_id):
        meta = _meta("actor_effect_scope-v1", f"effect:{declared_row.raw.get('effect_id')}", source_facts={"kind": "rule_source", "actor_id": actor_id}, label=declared_row.source_label)
        meta["existing_row"] = True
        row_metas.append((declared_row, meta))
    scaling_row = _annotate_attribute_scaling(rows, definition, actor_id)
    if scaling_row is not None:
        meta = _meta("attribute_scaling_actor-v1", "attribute_scaling", source_facts={"kind": "rule_source", "actor_id": actor_id}, label=scaling_row.source_label)
        meta["existing_row"] = True
        row_metas.append((scaling_row, meta))

    # 4) rules.stat_transfer（节点来源 → ratio=每点值×点数）
    stat_transfer_facts: list[dict[str, Any]] = []
    stat_transfers: list[dict[str, Any]] = []
    transfer_rule = (definition.get("rules") or {}).get("stat_transfer")
    if transfer_rule:
        try:
            resolved_rule, node_facts = _resolve_transfer_rule(transfer_rule, definition, catalog, calc)
        except actor_pool.ActorPoolError as exc:
            raise ActorCompileError(f"rules.stat_transfer 声明不合法：{exc}") from exc
        produced = actor_pool.apply_stat_transfers(rows, resolved_rule, states={})
        for transfer_row in produced:
            row_metas.append((transfer_row, _meta("stat_transfer-v1", f"transfer:{transfer_row.raw.get('transfer_ref')}", source_facts={"kind": "rule_source", "actor_id": actor_id, "transfer_ref": transfer_row.raw.get("transfer_ref")}, label=transfer_row.source_label)))
        stat_transfer_facts = node_facts
        stat_transfers = [copy.deepcopy(resolved_rule)]
    elif (definition.get("rules") or {}).get("attribute_scaling"):
        declared_scaling = copy.deepcopy((definition.get("rules") or {}).get("attribute_scaling") or {})
        output = declared_scaling.get("output") or {}
        if str(output.get("scope") or "") == actor_pool.ACTOR_SCOPE:
            stat_transfers = [
                {
                    "target_actor": actor_id,
                    "transfers": [
                        {
                            "id": "attribute_scaling",
                            "input_pool": {"stat_key": "attribute", "operations": ["added"]},
                            "ratio": 1.0,
                            "output": dict(output),
                        }
                    ],
                }
            ]

    # 5) rules.node_attribute_scaled（节点每点值 × 点数 × 属性值）
    node_attr_rows, node_attr_details = ([], [])
    node_attr_declared = (definition.get("rules") or {}).get("node_attribute_scaled")
    if node_attr_declared:
        try:
            node_attr_rows, node_attr_details = _node_attribute_rows(
                node_attr_declared, actor_id, attributes, catalog, calc, definition
            )
        except actor_pool.ActorPoolError as exc:
            raise ActorCompileError(f"rules.node_attribute_scaled 声明不合法：{exc}") from exc
        for node_row in node_attr_rows:
            row_metas.append((node_row, _meta("node_attribute_scaled-v1", f"node_attr:{node_row.raw.get('node', {}).get('node')}", source_facts={"kind": "rule_source", "actor_id": actor_id, "node": node_row.raw.get("node")}, label=node_row.source_label)))

    # 6) 暴击：不吸收、不覆盖。actor crit 行保持为普通 effect 行，执行期结算。
    actor_model = {
        "actor_id": actor_id,
        "scope": actor_pool.ACTOR_SCOPE,
        "actors": [actor],
        "native_stats": native_stats,
        "attributes": dict(attributes),
        "dataset": actor_facts,
        "stat_transfer_facts": stat_transfer_facts,
        "node_attribute_scaled": node_attr_details,
        "crit_policy": "execution_time_pool",
        "assembly": ACTOR_ASSEMBLY_VERSION,
    }
    frequency = copy.deepcopy(definition.get("frequency") or {}) or None
    if frequency is not None:
        # 派生频率必须在此处绑定真实输入点数；缺 catalog/calc 或节点不可用即失败，
        # 不允许把 points_control/per_point 回退留给运行期。
        frequency = resolve_derived_frequency(frequency, catalog=catalog, calc=calc)
    return {
        "assembly": ACTOR_ASSEMBLY_VERSION,
        "row_metas": row_metas,
        "actor_model": actor_model,
        "stat_transfers": stat_transfers,
        "frequency": frequency,
        "attributes": dict(attributes),
    }


__all__ = [
    "ACTOR_ASSEMBLY_VERSION",
    "ActorCompileError",
    "NATIVE_ORIGIN",
    "compile_actor_model",
    "resolve_derived_frequency",
]
