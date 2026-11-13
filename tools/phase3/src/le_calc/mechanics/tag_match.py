"""标签适用性共享算子（P2）。

两个模型各有一套 matcher，数学核心相同，差异只在“遇到不认识的标签怎么处理”：

* BH ``damage.tags_match``：标签按类型（伤害类型 / 类别）检查组件
  ``types`` / ``categories``；metadata 标签（LowLife/Curse/...）直接忽略；
  完全未知的标签也忽略（不能凭无证据的标签限制适用性）。
* FR ``hit_damage.tags_apply``：组件标签闭包做子集匹配；``Elemental`` 按
  元素展开；其它标签一律要求出现在闭包里（未知标签 = 限制）。

统一签名 ::

    tags_match(damage_tags, component_tags, policy) -> bool

``component_tags`` 统一传“组件标签闭包”。BH 侧用
:func:`bh_component_tags` 把 ``ComponentTags.types | categories`` 折叠成闭包；
FR 侧直接传 ``hit_damage._component_tags(component)``。差异用
:class:`TagPolicy` 参数化，不把两套语义强行合并。

两条生产调用点在 P2 保持原实现不变（本适配器先做等价性测试）；P2b 才考虑
把 ``damage.tags_match`` / ``hit_damage.tags_apply`` 换成共享实现。BH 策略从
生产模块读取常量（懒导入，避免 import 环），因此不会与生产常量漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable

SCHEMA_VERSION = "le-mechanics-tag-match-v1"

#: 未知标签策略
UNKNOWN_IGNORE = "ignore"
UNKNOWN_RESTRICT = "restrict"
UNKNOWN_TAG_POLICIES = (UNKNOWN_IGNORE, UNKNOWN_RESTRICT)


class TagPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class TagPolicy:
    """一个模型对标签适用性的显式策略。

    ``ignored_tags``:   直接跳过、不参与适用性判断的标签集合；
    ``known_tags``:     已知标签集合；已知标签必须出现在组件闭包里；
    ``unknown_tag_policy``: 不在 ``known_tags ∪ ignored_tags`` 的标签：
                        ``ignore`` = 不限制适用性，``restrict`` = 必须出现在
                        组件闭包里。
    """

    ignored_tags: frozenset[str]
    known_tags: frozenset[str]
    unknown_tag_policy: str
    name: str = ""

    def __post_init__(self) -> None:
        if self.unknown_tag_policy not in UNKNOWN_TAG_POLICIES:
            raise TagPolicyError(
                f"unknown_tag_policy={self.unknown_tag_policy!r} 不在 {UNKNOWN_TAG_POLICIES}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "name": self.name,
            "ignored_tags": sorted(self.ignored_tags),
            "known_tags": sorted(self.known_tags),
            "unknown_tag_policy": self.unknown_tag_policy,
        }


DAMAGE_CHANNEL_TYPES = frozenset({"Physical", "Fire", "Cold", "Lightning", "Void", "Necrotic", "Poison"})


def damage_channel_tags(tags: Iterable[str], damage_type: str) -> list[str]:
    """分量携带自身类型和行为标签；基础技能元素不是其它点伤的白名单。"""
    dtype = next((t for t in DAMAGE_CHANNEL_TYPES if t.lower() == str(damage_type).lower()), str(damage_type))
    result = [dtype]
    type_names = {item.lower() for item in DAMAGE_CHANNEL_TYPES} | {"elemental"}
    for tag in tags or ():
        if str(tag).lower() not in type_names and tag not in result:
            result.append(str(tag))
    if dtype in {"Fire", "Cold", "Lightning"}:
        result.append("Elemental")
    return result


def tags_match(
    damage_tags: Iterable[str],
    component_tags: Iterable[str],
    policy: TagPolicy,
) -> bool:
    """统一签名：一条修正行的标签是否适用于一个伤害组件。"""
    component = frozenset(component_tags or ())
    for tag in damage_tags or ():
        if tag in policy.ignored_tags:
            continue
        restrict = tag in policy.known_tags or policy.unknown_tag_policy == UNKNOWN_RESTRICT
        if restrict and tag not in component:
            return False
    return True


# ---------------------------------------------------------------------------
# 两个模型的策略与组件标签适配
# ---------------------------------------------------------------------------


def bh_component_tags(component: Any) -> frozenset[str]:
    """把 BH ``ComponentTags`` 折叠成闭包（types | categories）。"""
    return frozenset(component.types) | frozenset(component.categories)


#: BH 标签口径的共享常量（plan-v3 执行器不再 import 旧 damage 模块）。
PLAN_DAMAGE_TYPE_TAGS = frozenset({"Physical", "Lightning", "Cold", "Fire", "Void", "Necrotic", "Poison"})
PLAN_ELEMENT_TYPE_TAGS = frozenset({"Fire", "Cold", "Lightning"})
PLAN_CATEGORY_TAGS = frozenset(
    {
        "Spell", "DoT", "Hit", "Ailment", "Melee", "Throwing", "Bow",
        "Channelling", "Minion", "Totem", "Buff",
    }
)
PLAN_IGNORED_TAGS = frozenset(
    {
        "Elemental", "LowLife", "HighLife", "FullLife", "Curse", "Transform",
        "Potion", "PetResisted", "Crit_deprecated", "Kill_deprecated",
        "Die_deprecated",
    }
)


@lru_cache(maxsize=1)
def bh_plan_policy() -> TagPolicy:
    """plan-v3 执行器的 BH 标签口径（共享常量，无旧 calculator import）。"""
    return TagPolicy(
        ignored_tags=PLAN_IGNORED_TAGS,
        known_tags=frozenset(PLAN_DAMAGE_TYPE_TAGS | PLAN_ELEMENT_TYPE_TAGS | PLAN_CATEGORY_TAGS),
        unknown_tag_policy=UNKNOWN_IGNORE,
        name="bh-plan-v3.tag-match",
    )


@lru_cache(maxsize=1)
def bh_tag_policy() -> TagPolicy:
    """BH 当前口径；常量懒取自生产模块，避免与新 revision 漂移。"""
    from .. import damage  # 局部导入，规避 damage -> mechanics 的 import 环

    return TagPolicy(
        ignored_tags=frozenset(damage.IGNORED_TAGS),
        known_tags=frozenset(
            damage.ELEMENT_TYPE_TAGS | damage.DAMAGE_TYPE_TAGS | damage.CATEGORY_TAGS
        ),
        unknown_tag_policy=UNKNOWN_IGNORE,
        name="bh-damage.tags_match",
    )


@lru_cache(maxsize=1)
def hit_tag_policy() -> TagPolicy:
    """FR 当前口径：Elemental 展开等价于跳过，其余标签一律要求出现在闭包内。"""
    return TagPolicy(
        ignored_tags=frozenset({"Elemental"}),
        known_tags=frozenset(),
        unknown_tag_policy=UNKNOWN_RESTRICT,
        name="fr-hit_damage.tags_apply",
    )


# ---------------------------------------------------------------------------
# 差异分类（供等价性测试/registry 记录，不在运行时改变任何结论）
# ---------------------------------------------------------------------------

DIFF_NONE = "none"
DIFF_BH_METADATA_IGNORED = "bh_metadata_ignored"
DIFF_BH_UNKNOWN_IGNORED = "bh_unknown_tag_ignored"
DIFFERENCE_CLASSES = (DIFF_NONE, DIFF_BH_METADATA_IGNORED, DIFF_BH_UNKNOWN_IGNORED)


def classify_difference(
    tag: str,
    component_tags: Iterable[str],
    *,
    bh_policy: TagPolicy,
    hit_policy: TagPolicy,
) -> str:
    """单标签层面两个策略的差异类；返回 :data:`DIFFERENCE_CLASSES` 之一。

    前提：两个策略作用于**同一个组件闭包**，因此差异只来自策略本身
    （metadata 忽略 / 规则外标签忽略）。两个模型组件定义不同（BH 本体组件含
    ``Spell``/``DoT``，FR 命中组件含 ``Melee``/``Hit``）导致的差异属于输入
    差异，由等价性测试单独记录，不在这里归类。
    """
    component = frozenset(component_tags or ())
    bh_result = tags_match([tag], component, bh_policy)
    hit_result = tags_match([tag], component, hit_policy)
    if bh_result == hit_result:
        return DIFF_NONE
    if tag in bh_policy.ignored_tags:
        return DIFF_BH_METADATA_IGNORED
    return DIFF_BH_UNKNOWN_IGNORED


def classify_tag_set_differences(
    tags: Iterable[str],
    component_tags: Iterable[str],
    *,
    bh_policy: TagPolicy,
    hit_policy: TagPolicy,
) -> list[dict[str, str]]:
    """逐个标签记录差异类（``none`` 也保留，便于复盘差异面）。"""
    component = frozenset(component_tags or ())
    out: list[dict[str, str]] = []
    for tag in tags:
        out.append(
            {
                "tag": tag,
                "difference_class": classify_difference(
                    tag, component, bh_policy=bh_policy, hit_policy=hit_policy
                ),
            }
        )
    return out


__all__ = [
    "DIFFERENCE_CLASSES",
    "DIFF_BH_METADATA_IGNORED",
    "DIFF_BH_UNKNOWN_IGNORED",
    "DIFF_NONE",
    "SCHEMA_VERSION",
    "TagPolicy",
    "TagPolicyError",
    "UNKNOWN_IGNORE",
    "UNKNOWN_RESTRICT",
    "UNKNOWN_TAG_POLICIES",
    "bh_component_tags",
    "bh_plan_policy",
    "bh_tag_policy",
    "classify_difference",
    "classify_tag_set_differences",
    "hit_tag_policy",
    "tags_match",
]
