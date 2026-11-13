"""通用 actor 池与显式 owner→actor 转移（数据声明式）。

设计边界（与 ``docs/contracts/calculation-plan-v4.md`` §7/§8 的通用 execution 契约一致）：

* **actor 声明**：``definition.actor`` 描述一个独立属性池的伤害 actor（例如召唤物）。
  actor 的起始数值来自 ``base_effects``（每条带 dataset 证据锚点），默认
  ``inherits="none"``，即**不**隐式继承玩家属性；玩家→actor 的共享只能通过显式
  ``rules.stat_transfer`` 或技能级 ``attribute_scaling`` 声明。
* **转移规则**：``rules.stat_transfer`` 的每条 transfer 声明输入池
  （stat_key/operation/tag 闭包）、比例（固定或每点 × 点数 control）与输出
  （stat/operation/tags/scope）。装配期把玩家 ``Contribution`` 行按声明过滤、
  乘比例后生成 actor 行；运行期只读已序列化的行，不再回读规则。
* **scope**：行级 ``raw["scope"]`` 取 ``owner`` / ``actor`` / ``all``；伤害组件装配
  只在组件声明的 scope 内取行（``dps_components.build_hit_components``），
  不匹配的行进入应用账本并标注原因（不冒充生效）。

本模块只做数据校验与行构造，不做 skill/ability 分支，不读文件、不访问目录。
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional

from . import model
from .mechanics.tag_match import UNKNOWN_RESTRICT, TagPolicy, tags_match

ACTOR_SCOPE = "actor"
OWNER_SCOPE = "owner"
ALL_SCOPE = "all"
ROW_SCOPES = (ACTOR_SCOPE, OWNER_SCOPE, ALL_SCOPE)

ACTOR_KINDS = ("summon",)
INHERITS_MODES = ("none", "explicit_transfers")
DAMAGE_SOURCE_KINDS = ("dataset_skill",)
BASE_EFFECT_OPERATIONS = ("added", "increased", "more", "base")
STAT_TRANSFER_OPERATIONS = ("added", "increased", "more", "base")
TRANSFER_TAG_POLICIES = ("component-closure", "none")
STAT_TRANSFER_SCHEMA = "le-stat-transfer-v1"

TAG_POLICIES: dict[str, TagPolicy] = {
    "component-closure": TagPolicy(
        ignored_tags=frozenset({"Elemental"}),
        known_tags=frozenset(),
        unknown_tag_policy=UNKNOWN_RESTRICT,
        name="actor-pool-closure",
    ),
}
#: ``tag_policy="none"`` 表示不做 tag 过滤（仅按 stat/operation/scope 过滤）。
TAG_POLICY_NONE = "none"

_ACTOR_ALLOWED = {
    "actor_id", "kind", "source_entity", "damage_source", "inherits", "base_effects",
    "native_stats", "label", "evidence", "notes",
}
_NATIVE_STATS_ALLOWED = {"stat_keys", "tags_include", "output_tags", "label", "evidence", "notes"}
_ACTOR_EFFECT_ALLOWED = {
    "id", "stat", "operation", "value", "tags", "scope", "source", "evidence", "notes",
}
_DAMAGE_SOURCE_ALLOWED = {"kind", "ability_entity", "evidence", "notes"}
_TRANSFER_RULE_ALLOWED = {"schema", "label", "target_actor", "transfers", "evidence", "notes"}
_TRANSFER_ALLOWED = {
    "id", "source", "input_pool", "ratio", "ratio_per_point", "points_control",
    "output", "evidence", "notes",
}
_INPUT_POOL_ALLOWED = {
    "stat_key", "operations", "tag_closure", "tag_policy", "include_scopes", "exclude_tags",
    "include_source_kinds",
}
#: 转移输入默认只允许玩家来源（skill_node 行是 actor 自身效果，不能再被转移重复计入）。
#: ``unique_description`` 是暗金特殊说明词条（玩家侧 Stat 行），属于玩家来源；
#: 是否真正进入某条转移仍由该 transfer 的 stat/operation/tag/scope 过滤决定。
DEFAULT_TRANSFER_SOURCE_KINDS = (
    "base_character", "equipment", "idol", "idol_altar", "blessing", "passive_node",
    "unique_description", "derived",
)
_OUTPUT_ALLOWED = {"stat", "operation", "tags", "scope", "notes"}


class ActorPoolError(ValueError):
    """actor/stat_transfer 声明不合法。"""


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ActorPoolError(f"{label} 必须是对象")
    return value


def _reject_unknown(payload: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ActorPoolError(f"{label} 含未知字段：{unknown}")


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ActorPoolError(f"{label} 必须是有限数值")
    return float(value)


def _nonempty(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ActorPoolError(f"{label} 缺失")
    return text


def _scope(value: Any, label: str, *, default: str = ACTOR_SCOPE) -> str:
    scope = str(value or default)
    if scope not in ROW_SCOPES:
        raise ActorPoolError(f"{label} scope 不受支持：{scope!r}")
    return scope


def validate_actor_declaration(actor: Any) -> dict[str, Any]:
    """结构校验一条 ``definition.actor``；未知字段/枚举拒绝。"""
    actor = _require_mapping(actor, "actor")
    _reject_unknown(actor, _ACTOR_ALLOWED, "actor")
    actor_id = _nonempty(actor.get("actor_id"), "actor.actor_id")
    kind = _nonempty(actor.get("kind"), "actor.kind")
    if kind not in ACTOR_KINDS:
        raise ActorPoolError(f"actor.kind 不受支持：{kind!r}")
    source_entity = _nonempty(actor.get("source_entity"), "actor.source_entity")
    inherits = str(actor.get("inherits") or "none")
    if inherits not in INHERITS_MODES:
        raise ActorPoolError(f"actor.inherits 不受支持：{inherits!r}")
    damage_source = actor.get("damage_source")
    if damage_source is not None:
        damage_source = _require_mapping(damage_source, "actor.damage_source")
        _reject_unknown(damage_source, _DAMAGE_SOURCE_ALLOWED, "actor.damage_source")
        ds_kind = _nonempty(damage_source.get("kind"), "actor.damage_source.kind")
        if ds_kind not in DAMAGE_SOURCE_KINDS:
            raise ActorPoolError(f"actor.damage_source.kind 不受支持：{ds_kind!r}")
        _nonempty(damage_source.get("ability_entity"), "actor.damage_source.ability_entity")
    native_stats = actor.get("native_stats")
    if native_stats is not None:
        _reject_unknown(_require_mapping(native_stats, "actor.native_stats"), _NATIVE_STATS_ALLOWED, "actor.native_stats")
        stat_keys = native_stats.get("stat_keys")
        if not isinstance(stat_keys, list) or not stat_keys:
            raise ActorPoolError("actor.native_stats.stat_keys 必须是非空数组")
        for key in stat_keys:
            _nonempty(key, "actor.native_stats.stat_keys[]")
        tags_include = native_stats.get("tags_include") or []
        if not isinstance(tags_include, list):
            raise ActorPoolError("actor.native_stats.tags_include 必须是数组")
        output_tags = native_stats.get("output_tags")
        if output_tags is not None and not isinstance(output_tags, list):
            raise ActorPoolError("actor.native_stats.output_tags 必须是数组")
    base_effects = actor.get("base_effects") or []
    if not isinstance(base_effects, list):
        raise ActorPoolError("actor.base_effects 必须是数组")
    for index, effect in enumerate(base_effects):
        label = f"actor.base_effects[{index}]"
        effect = _require_mapping(effect, label)
        _reject_unknown(effect, _ACTOR_EFFECT_ALLOWED, label)
        _nonempty(effect.get("id"), f"{label}.id")
        _nonempty(effect.get("stat"), f"{label}.stat")
        operation = str(effect.get("operation") or "")
        if operation not in BASE_EFFECT_OPERATIONS:
            raise ActorPoolError(f"{label}.operation 不受支持：{operation!r}")
        _finite(effect.get("value"), f"{label}.value")
        if effect.get("tags") is not None and not isinstance(effect.get("tags"), list):
            raise ActorPoolError(f"{label}.tags 必须是数组")
        _scope(effect.get("scope"), label)
    return {
        "actor_id": actor_id,
        "kind": kind,
        "source_entity": source_entity,
        "inherits": inherits,
        "damage_source": dict(damage_source) if isinstance(damage_source, dict) else None,
        "base_effects": [dict(item) for item in base_effects],
    }


def validate_stat_transfer_rule(rule: Any) -> dict[str, Any]:
    """结构校验 ``rules.stat_transfer``；转移条目与输出字段未知即拒绝。"""
    rule = _require_mapping(rule, "rules.stat_transfer")
    _reject_unknown(rule, _TRANSFER_RULE_ALLOWED, "rules.stat_transfer")
    target_actor = _nonempty(rule.get("target_actor"), "rules.stat_transfer.target_actor")
    transfers = rule.get("transfers")
    if not isinstance(transfers, list) or not transfers:
        raise ActorPoolError("rules.stat_transfer.transfers 必须是非空数组")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, transfer in enumerate(transfers):
        label = f"rules.stat_transfer.transfers[{index}]"
        transfer = _require_mapping(transfer, label)
        _reject_unknown(transfer, _TRANSFER_ALLOWED, label)
        transfer_id = _nonempty(transfer.get("id"), f"{label}.id")
        if transfer_id in seen:
            raise ActorPoolError(f"{label}.id 重复：{transfer_id}")
        seen.add(transfer_id)
        input_pool = _require_mapping(transfer.get("input_pool"), f"{label}.input_pool")
        _reject_unknown(input_pool, _INPUT_POOL_ALLOWED, f"{label}.input_pool")
        _nonempty(input_pool.get("stat_key"), f"{label}.input_pool.stat_key")
        operations = input_pool.get("operations") or ["added", "increased"]
        if not isinstance(operations, list) or not operations:
            raise ActorPoolError(f"{label}.input_pool.operations 必须是非空数组")
        for operation in operations:
            if str(operation) not in STAT_TRANSFER_OPERATIONS:
                raise ActorPoolError(f"{label}.input_pool.operations 含未知 operation：{operation!r}")
        tag_closure = input_pool.get("tag_closure") or []
        if not isinstance(tag_closure, list):
            raise ActorPoolError(f"{label}.input_pool.tag_closure 必须是数组")
        exclude_tags = input_pool.get("exclude_tags") or []
        if not isinstance(exclude_tags, list):
            raise ActorPoolError(f"{label}.input_pool.exclude_tags 必须是数组")
        include_source_kinds = input_pool.get("include_source_kinds")
        if include_source_kinds is not None:
            if not isinstance(include_source_kinds, list) or not include_source_kinds:
                raise ActorPoolError(f"{label}.input_pool.include_source_kinds 必须是非空数组")
            for kind in include_source_kinds:
                _nonempty(kind, f"{label}.input_pool.include_source_kinds[]")
        tag_policy = str(input_pool.get("tag_policy") or "component-closure")
        if tag_policy not in TRANSFER_TAG_POLICIES:
            raise ActorPoolError(f"{label}.input_pool.tag_policy 不受支持：{tag_policy!r}")
        include_scopes = input_pool.get("include_scopes") or [OWNER_SCOPE, ALL_SCOPE]
        if not isinstance(include_scopes, list):
            raise ActorPoolError(f"{label}.input_pool.include_scopes 必须是数组")
        for scope in include_scopes:
            if str(scope) not in ROW_SCOPES:
                raise ActorPoolError(f"{label}.input_pool.include_scopes 含未知 scope：{scope!r}")
        transfer_source = transfer.get("source") or {}
        node_sourced = str(transfer_source.get("kind") or "") == "skill_node"
        if node_sourced:
            for key in ("tree", "node"):
                _nonempty(transfer_source.get(key), f"{label}.source.{key}")
        has_ratio = transfer.get("ratio") is not None
        has_per_point = transfer.get("ratio_per_point") is not None
        if node_sourced and not has_ratio and not has_per_point:
            # 节点来源：比例 = dataset 每点值 × 当前点数，在编译期绑定（不要求模板给 ratio）。
            ratio = None
            per_point = None
            points_control = None
        else:
            if has_ratio == has_per_point:
                raise ActorPoolError(f"{label} 必须且只能声明 ratio 或 ratio_per_point 之一")
            if has_ratio:
                ratio = _finite(transfer.get("ratio"), f"{label}.ratio")
                if ratio < 0:
                    raise ActorPoolError(f"{label}.ratio 不能为负")
                per_point = None
                points_control = None
            else:
                per_point = _finite(transfer.get("ratio_per_point"), f"{label}.ratio_per_point")
                if per_point < 0:
                    raise ActorPoolError(f"{label}.ratio_per_point 不能为负")
                declared_points_control = str(transfer.get("points_control") or "").strip()
                if not declared_points_control and not node_sourced:
                    raise ActorPoolError(f"{label}.points_control 缺失")
                # skill_node 来源的每点比例：点数在编译期从专精绑定（不要求模板声明 control）。
                points_control = declared_points_control or None
                ratio = None
        output = transfer.get("output")
        if not isinstance(output, dict):
            raise ActorPoolError(f"{label} 必须声明 output")
        _reject_unknown(output, _OUTPUT_ALLOWED, f"{label}.output")
        _nonempty(output.get("stat"), f"{label}.output.stat")
        output_operation = str(output.get("operation") or "")
        if output_operation not in STAT_TRANSFER_OPERATIONS:
            raise ActorPoolError(f"{label}.output.operation 不受支持：{output_operation!r}")
        if output.get("tags") is not None and not isinstance(output.get("tags"), list):
            raise ActorPoolError(f"{label}.output.tags 必须是数组")
        output_scope = _scope(output.get("scope"), f"{label}.output")
        if output_scope != ACTOR_SCOPE:
            raise ActorPoolError(f"{label}.output.scope 必须是 actor（转移的输出属于 actor 池）")
        normalized.append(
            {
                "id": transfer_id,
                "source": dict(transfer.get("source") or {}),
                "input_pool": {
                    "stat_key": str(input_pool["stat_key"]),
                    "operations": [str(item) for item in operations],
                    "tag_closure": [str(item) for item in tag_closure],
                    "tag_policy": tag_policy,
                    "include_scopes": [str(item) for item in include_scopes],
                    "exclude_tags": [str(item) for item in exclude_tags],
                    "include_source_kinds": (
                        [str(item) for item in include_source_kinds]
                        if include_source_kinds is not None
                        else list(DEFAULT_TRANSFER_SOURCE_KINDS)
                    ),
                },
                "ratio": ratio,
                "ratio_per_point": per_point,
                "points_control": str(points_control) if points_control else None,
                "output": {
                    "stat": str(output["stat"]),
                    "operation": output_operation,
                    "tags": [str(item) for item in output.get("tags") or []],
                    "scope": output_scope,
                },
                "evidence": list(transfer.get("evidence") or []),
                "notes": transfer.get("notes"),
            }
        )
    return {"schema": STAT_TRANSFER_SCHEMA, "target_actor": target_actor, "transfers": normalized}


def _actor_short(actor_id: str) -> str:
    return str(actor_id).rsplit(":", 1)[-1]


def build_actor_base_rows(actors: list[Mapping[str, Any]]) -> list[model.Contribution]:
    """actor ``base_effects`` → 起始 Contribution 行（scope=actor）。"""
    rows: list[model.Contribution] = []
    seen: set[str] = set()
    for actor in actors or []:
        declaration = validate_actor_declaration(actor)
        actor_id = declaration["actor_id"]
        short = _actor_short(actor_id)
        for index, effect in enumerate(declaration["base_effects"]):
            effect_id = str(effect["id"])
            contribution_id = f"actor:{short}:base:{effect_id}"
            if contribution_id in seen:
                raise ActorPoolError(f"actor base effect id 重复：{contribution_id}")
            seen.add(contribution_id)
            operation = str(effect["operation"])
            unit = "percent" if operation in ("increased", "more") else "flat"
            rows.append(
                model.Contribution(
                    contribution_id=contribution_id,
                    source_kind="derived",
                    source_label=f"{declaration['actor_id']} base",
                    source_path="definition.actor.base_effects",
                    stat_name=str(effect["stat"]),
                    stat_key=str(effect["stat"]),
                    operation=operation,
                    value=float(effect["value"]),
                    unit=unit,
                    damage_tags=[str(item) for item in effect.get("tags") or []],
                    group=f"actor:{actor_id}:base:{effect_id}",
                    included=True,
                    value_source="source",
                    status=model.STATUS_DERIVED,
                    evidence=[
                        model.Evidence(kind="derived", detail=f"actor base effect {effect_id}", locator=str(effect.get("source") or ""))
                    ],
                    notes=effect.get("notes"),
                    raw={
                        "scope": ACTOR_SCOPE,
                        "owner": actor_id,
                        "actor_id": actor_id,
                        "base_effect_index": index,
                        "source": (
                            dict(effect["source"])
                            if isinstance(effect.get("source"), dict)
                            else {"ref": str(effect.get("source"))}
                        ),
                    },
                )
            )
    return rows


def mark_owner_scope(rows: list[model.Contribution], *, actor_ids: Optional[list[str]] = None) -> None:
    """把已有（玩家）行标为 owner scope；actor 行保持自己的 scope。

    只在显式声明 actor 的装配中使用；未标记 scope 的行在无 scope 过滤时行为不变。
    """
    actor_set = {str(item) for item in actor_ids or []}
    for row in rows:
        raw = row.raw if isinstance(row.raw, dict) else {}
        raw = dict(raw)
        actor = str(raw.get("owner") or raw.get("actor_id") or "")
        if actor in actor_set:
            raw.setdefault("scope", ACTOR_SCOPE)
        else:
            raw.setdefault("scope", OWNER_SCOPE)
        row.raw = raw


def _row_scope(row: model.Contribution) -> str:
    raw = row.raw if isinstance(row.raw, dict) else {}
    return str(raw.get("scope") or ALL_SCOPE)


def _pool_rows(
    rows: list[model.Contribution],
    input_pool: Mapping[str, Any],
    *,
    operation_only: Optional[str] = None,
) -> list[model.Contribution]:
    """按 input_pool 过滤待逐行复制的输入行（保留原 operation/damage_tags）。"""
    operations = {str(item) for item in input_pool["operations"]}
    if operation_only is not None:
        operations = {str(operation_only)}
    scopes = {str(item) for item in input_pool["include_scopes"]}
    closure = [str(item) for item in input_pool["tag_closure"]]
    exclude = {str(item) for item in input_pool.get("exclude_tags") or []}
    source_kinds = {
        str(item)
        for item in (input_pool.get("include_source_kinds") or DEFAULT_TRANSFER_SOURCE_KINDS)
    }
    tag_policy = str(input_pool["tag_policy"])
    policy = TAG_POLICIES.get(tag_policy)
    selected: list[model.Contribution] = []
    for row in rows:
        if not row.active:
            continue
        if str(row.stat_key) != str(input_pool["stat_key"]):
            continue
        if str(row.operation) not in operations:
            continue
        if str(row.source_kind) not in source_kinds:
            # 模板 effects 的 skill_node 行属于 actor 自身效果，不得进入转移输入池。
            continue
        if _row_scope(row) not in scopes:
            continue
        if exclude and set(row.damage_tags) & exclude:
            continue
        if closure and policy is not None and not tags_match(row.damage_tags, closure, policy):
            continue
        selected.append(row)
    return selected


def _short_row_id(contribution_id: str) -> str:
    """输入 contribution_id 的稳定短 id（用于派生行 id/group）。"""
    import hashlib  # noqa: PLC0415
    import re  # noqa: PLC0415

    cleaned = re.sub(r"[^0-9A-Za-z_]+", "_", str(contribution_id)).strip("_")
    if len(cleaned) <= 40:
        return cleaned
    digest = hashlib.sha256(str(contribution_id).encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:32]}_{digest}"


def apply_stat_transfers(
    rows: list[model.Contribution],
    rule: Mapping[str, Any],
    *,
    states: Optional[Mapping[str, Any]] = None,
) -> list[model.Contribution]:
    """按 ``rules.stat_transfer`` **逐输入行复制**为 actor 行。

    ``GetStatsFromPlayer`` 语义：输入池每条 Contribution 生成一条 actor 输出行，
    值 = 输入值 × ratio，operation 与 damage_tags 保持输入行；``more`` 行使用逐行唯一
    group 以保证多个 more 叠乘；输入默认排除 ``skill_node`` 来源（模板 effects 的
    actor 行，不能被转移重复计入）。
    """
    normalized = validate_stat_transfer_rule(rule)
    states = dict(states or {})
    actor_id = normalized["target_actor"]
    short = _actor_short(actor_id)
    produced: list[model.Contribution] = []
    for transfer in normalized["transfers"]:
        if transfer["ratio"] is not None:
            ratio = float(transfer["ratio"])
            points = None
        else:
            control = str(transfer["points_control"] or "")
            raw_points = states.get(control, 0) if control else 0
            if isinstance(raw_points, bool) or not isinstance(raw_points, int):
                raise ActorPoolError(f"stat_transfer {transfer['id']!r} 的 points control {control!r} 必须是整数")
            if raw_points < 0:
                raise ActorPoolError(f"stat_transfer {transfer['id']!r} 的 points control {control!r} 不能为负")
            points = raw_points
            ratio = float(transfer["ratio_per_point"]) * float(raw_points)
        output = transfer["output"]
        output_operation = output.get("operation")
        for input_row in _pool_rows(rows, transfer["input_pool"]):
            operation = str(input_row.operation)
            if output_operation and operation != str(output_operation):
                continue
            emitted_tags = (
                [str(item) for item in output["tags"]]
                if output.get("tags")
                else [str(item) for item in input_row.damage_tags]
            )
            row_id = _short_row_id(input_row.contribution_id)
            group = (
                f"actor:{short}:more:{row_id}"
                if operation == "more"
                else f"actor:{short}:transfer:{transfer['id']}:{row_id}"
            )
            unit = "percent" if operation in ("increased", "more") else "flat"
            produced.append(
                model.Contribution(
                    contribution_id=f"actor:{short}:transfer:{transfer['id']}:{row_id}",
                    source_kind="derived",
                    source_label=f"owner→actor 转移 {transfer['id']} ← {input_row.contribution_id}",
                    source_path="rules.stat_transfer",
                    stat_name=str(output["stat"]),
                    stat_key=str(output["stat"]),
                    operation=operation,
                    value=float(input_row.value) * ratio,
                    unit=unit,
                    damage_tags=emitted_tags,
                    target=input_row.target,
                    group=group,
                    included=bool(input_row.included),
                    value_source="derived",
                    status=model.STATUS_DERIVED,
                    evidence=[
                        model.Evidence(
                            kind="derived",
                            detail=(
                                f"逐行复制 {input_row.contribution_id} × 比例 {ratio:.6g} "
                                f"(operation={operation})"
                            ),
                            locator="rules.stat_transfer",
                        )
                    ],
                    notes=transfer.get("notes"),
                    raw={
                        "scope": ACTOR_SCOPE,
                        "owner": actor_id,
                        "actor_id": actor_id,
                        "transfer_ref": transfer["id"],
                        "transfer_operation": operation,
                        "transfer_source": dict(transfer.get("source") or {}),
                        "ratio": ratio,
                        "points": points,
                        "input_pool": dict(transfer["input_pool"]),
                        "input_contribution_id": input_row.contribution_id,
                        "input_value": float(input_row.value),
                        "input_damage_tags": [str(item) for item in input_row.damage_tags],
                    },
                )
            )
    return produced


_NODE_ATTRIBUTE_ALLOWED = {"id", "source", "attribute", "output", "evidence", "notes", "label"}
_NODE_ATTRIBUTE_OUTPUT_ALLOWED = {"stat", "operation", "tags", "scope"}


# ---------------------------------------------------------------------------
# dataset minion base stats 对齐（actor base_effects 的证据/数值来源约束）
# ---------------------------------------------------------------------------

#: dataset minion ``base_stats.stats.stats[]`` 的 ``property`` → 本工程 stat_key。
#: 只收录可参与伤害结算的稳定映射；未收录属性不参与 base_effect 对齐（不报错，
#: 也不进入 actor 数值），避免把防御/功能属性误当伤害来源。
DATASET_MINION_STAT_PROPERTIES: dict[int, str] = {
    0: model.STAT_DAMAGE,
    2: model.STAT_ATTACK_SPEED,
    3: model.STAT_CAST_SPEED,
    4: model.STAT_CRIT_CHANCE,
    5: model.STAT_CRIT_MULTIPLIER,
    59: model.STAT_PENETRATION,
}
#: actor base_effect.source 声明 dataset minion base stats 的稳定标记。
DATASET_MINION_BASE_STAT_SOURCES = ("dataset:minion.base_stats",)


def is_dataset_minion_base_effect(effect: Mapping[str, Any]) -> bool:
    """base_effect 是否把 dataset minion base stats 声明为数值来源。"""
    source = effect.get("source")
    if isinstance(source, str):
        return source.strip() in DATASET_MINION_BASE_STAT_SOURCES
    if isinstance(source, Mapping):
        for ref in (source.get("ref"), source.get("kind"), source.get("source")):
            if isinstance(ref, str) and ref.strip() in DATASET_MINION_BASE_STAT_SOURCES:
                return True
    return False


def decode_minion_base_stats(minion: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把 dataset minion 的 ``base_stats.stats`` 解码成 (stat, operation, value) 行。

    结构无法解析时抛 ``ActorPoolError``（不静默通过）；未映射的属性跳过并保留在
    ``unmapped_properties`` 说明中，不参与 base_effect 对齐。
    """
    if not isinstance(minion, Mapping):
        raise ActorPoolError("dataset minion 必须是对象，才能校验 actor base_effects")
    entity_id = str(minion.get("entity_id") or "")
    base_stats = minion.get("base_stats")
    if not isinstance(base_stats, Mapping):
        raise ActorPoolError(
            f"dataset minion {entity_id!r} 的 base_stats 不是对象，无法校验 actor base_effects"
        )
    stats_block = base_stats.get("stats")
    if not isinstance(stats_block, Mapping) or not isinstance(stats_block.get("stats"), list):
        raise ActorPoolError(
            f"dataset minion {entity_id!r} 的 base_stats.stats.stats 缺失或不是数组，"
            "无法校验 actor base_effects"
        )
    rows: list[dict[str, Any]] = []
    for index, entry in enumerate(stats_block["stats"]):
        if not isinstance(entry, Mapping):
            raise ActorPoolError(
                f"dataset minion {entity_id!r} 的 base_stats.stats.stats[{index}] 不是对象，无法解析"
            )
        property_id = entry.get("property")
        if isinstance(property_id, bool) or not isinstance(property_id, int):
            raise ActorPoolError(
                f"dataset minion {entity_id!r} 的 base_stats.stats.stats[{index}].property "
                f"不是整数：{property_id!r}"
            )
        stat = DATASET_MINION_STAT_PROPERTIES.get(property_id)
        if stat is None:
            continue
        more_values = entry.get("moreValues")
        if more_values is None:
            more_values = []
        if not isinstance(more_values, list):
            raise ActorPoolError(
                f"dataset minion {entity_id!r} 的 base_stats.stats.stats[{index}].moreValues 不是数组"
            )
        for operation_key, operation in (
            ("addedValue", "added"),
            ("increasedValue", "increased"),
        ):
            if entry.get(operation_key) is None:
                continue
            value = _finite(entry.get(operation_key), f"dataset minion base_stats.stats[{index}].{operation_key}")
            rows.append(
                {"stat": stat, "operation": operation, "value": value,
                 "property": property_id, "stat_index": index}
            )
        for more_index, raw_more in enumerate(more_values):
            value = _finite(
                raw_more,
                f"dataset minion base_stats.stats[{index}].moreValues[{more_index}]",
            )
            rows.append(
                {"stat": stat, "operation": "more", "value": value,
                 "property": property_id, "stat_index": index}
            )
    return rows


def validate_actor_base_effects_against_dataset(
    actor: Mapping[str, Any],
    minion: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """校验 dataset 来源的 base_effect 在 minion base stats 中有同 stat/operation/value 项。

    返回匹配证据列表；任一 dataset 来源 base_effect 无法对齐即抛 ``ActorPoolError``，
    不允许模板文本自证。没有 dataset 来源 base_effect 时返回空列表（不要求结构）。
    """
    effects = actor.get("base_effects") or []
    dataset_effects = [
        (index, effect)
        for index, effect in enumerate(effects)
        if isinstance(effect, Mapping) and is_dataset_minion_base_effect(effect)
    ]
    if not dataset_effects:
        return []
    rows = decode_minion_base_stats(minion)
    entity_id = str(minion.get("entity_id") or "")
    matched: list[dict[str, Any]] = []
    for index, effect in dataset_effects:
        effect_id = str(effect.get("id") or f"base_effects[{index}]")
        stat = str(effect.get("stat") or "")
        operation = str(effect.get("operation") or "")
        value = _finite(effect.get("value"), f"actor.base_effects[{index}].value")
        hit = next(
            (
                row
                for row in rows
                if row["stat"] == stat
                and row["operation"] == operation
                and math.isclose(row["value"], value, rel_tol=1e-12, abs_tol=1e-12)
            ),
            None,
        )
        if hit is None:
            available = sorted(
                {
                    f"{row['stat']}/{row['operation']}={row['value']:g}"
                    for row in rows
                }
            )
            raise ActorPoolError(
                f"actor.base_effects[{index}] {effect_id!r} 声明 {stat}/{operation}/{value:g} "
                f"来自 dataset minion {entity_id!r} 的 base_stats，但该 minion 解码结果中"
                f"不存在同 stat/operation/value 项；可用项：{available}。"
                "不允许仅由模板文本自证该数值"
            )
        matched.append(
            {
                "base_effect_id": effect_id,
                "entity_id": entity_id,
                "stat": stat,
                "operation": operation,
                "value": value,
                "property": hit["property"],
                "stat_index": hit["stat_index"],
                "locator": (
                    f"dataset.minions#{entity_id}:base_stats.stats[{hit['stat_index']}]"
                ),
            }
        )
    return matched


def validate_native_stats(declared: Any) -> dict[str, Any] | None:
    """校验并规范化 ``actor.native_stats``（owner 行 100% 复制为 actor 行）。"""
    if declared is None:
        return None
    declared = _require_mapping(declared, "actor.native_stats")
    _reject_unknown(declared, _NATIVE_STATS_ALLOWED, "actor.native_stats")
    stat_keys = [_nonempty(item, "actor.native_stats.stat_keys[]") for item in declared.get("stat_keys") or []]
    if not stat_keys:
        raise ActorPoolError("actor.native_stats.stat_keys 必须是非空数组")
    tags_include = [str(item) for item in declared.get("tags_include") or []]
    output_tags = declared.get("output_tags")
    return {
        "stat_keys": stat_keys,
        "tags_include": tags_include,
        "output_tags": [str(item) for item in output_tags] if output_tags is not None else None,
        "label": declared.get("label"),
    }


def validate_node_attribute_scaled_rule(declared: Any) -> list[dict[str, Any]]:
    """校验 ``rules.node_attribute_scaled``（单条或列表）并规范化。"""
    items = declared if isinstance(declared, list) else [declared]
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        label = f"rules.node_attribute_scaled[{index}]" if isinstance(declared, list) else "rules.node_attribute_scaled"
        item = _require_mapping(item, label)
        _reject_unknown(item, _NODE_ATTRIBUTE_ALLOWED, label)
        rule_id = _nonempty(item.get("id"), f"{label}.id")
        if rule_id in seen:
            raise ActorPoolError(f"{label}.id 重复：{rule_id}")
        seen.add(rule_id)
        source = _require_mapping(item.get("source"), f"{label}.source")
        if str(source.get("kind") or "") != "skill_node":
            raise ActorPoolError(f"{label}.source.kind 只支持 skill_node")
        for key in ("tree", "node"):
            _nonempty(source.get(key), f"{label}.source.{key}")
        int(source.get("stat_index") or 0)
        attribute = _nonempty(item.get("attribute"), f"{label}.attribute")
        output = _require_mapping(item.get("output"), f"{label}.output")
        _reject_unknown(output, _NODE_ATTRIBUTE_OUTPUT_ALLOWED, f"{label}.output")
        _nonempty(output.get("stat"), f"{label}.output.stat")
        operation = str(output.get("operation") or "")
        if operation not in STAT_TRANSFER_OPERATIONS:
            raise ActorPoolError(f"{label}.output.operation 不受支持：{operation!r}")
        output_scope = _scope(output.get("scope"), f"{label}.output")
        if output_scope != ACTOR_SCOPE:
            raise ActorPoolError(f"{label}.output.scope 必须是 actor")
        normalized.append({
            "id": rule_id,
            "source": {
                "kind": "skill_node",
                "tree": str(source["tree"]),
                "node": str(source["node"]),
                "stat_index": int(source.get("stat_index") or 0),
            },
            "attribute": attribute,
            "output": {
                "stat": str(output["stat"]),
                "operation": operation,
                "tags": [str(t) for t in output.get("tags") or []],
                "scope": output_scope,
            },
            "evidence": list(item.get("evidence") or []),
            "label": item.get("label"),
            "notes": item.get("notes"),
        })
    return normalized


__all__ = [
    "ACTOR_SCOPE",
    "ACTOR_KINDS",
    "ALL_SCOPE",
    "ActorPoolError",
    "BASE_EFFECT_OPERATIONS",
    "DAMAGE_SOURCE_KINDS",
    "DATASET_MINION_BASE_STAT_SOURCES",
    "DATASET_MINION_STAT_PROPERTIES",
    "INHERITS_MODES",
    "OWNER_SCOPE",
    "ROW_SCOPES",
    "STAT_TRANSFER_OPERATIONS",
    "STAT_TRANSFER_SCHEMA",
    "TAG_POLICIES",
    "TRANSFER_TAG_POLICIES",
    "apply_stat_transfers",
    "build_actor_base_rows",
    "decode_minion_base_stats",
    "is_dataset_minion_base_effect",
    "mark_owner_scope",
    "TAG_POLICY_NONE",
    "validate_actor_base_effects_against_dataset",
    "validate_actor_declaration",
    "validate_native_stats",
    "validate_node_attribute_scaled_rule",
    "validate_stat_transfer_rule",
]
