# 数据字典（schema v1）

本文描述规范化数据库 `data/normalized/<dataset-id>/data.sqlite` 的表、字段、值状态与单位约定。DDL 在 `tools/ingest/migrations/0001_init.sql`，结构以 DDL 为准，本文件解释语义。

## 1. 版本与身份

| 概念 | 含义 |
|---|---|
| `dataset_id` | 一组固定原始快照 + 解析/映射版本。一份 `.sqlite` 对应一个 dataset。 |
| `run_id` | 一次执行（discover/fetch/parse/normalize）。同一 run 可以产出多个 dataset。 |
| `source_snapshot_id` | 一个来源在某一时点的原始抓取集合，如 `letools-v152-20261101`。 |
| `entity_id` | 内部稳定 ID，格式 `<kind>:<source>:<scope>:<source_id>`，如 `skill:letools:LEAbilities.abilityList:wo42`。不使用名称、显示顺序或 SQLite rowid。 |
| `record_id` | 来源记录 ID = sha256(source, snapshot, resource, kind, scope, source_id) 前 32 位。 |

跨来源合并只通过 `data/mappings/crosswalk.json` 显式登记；没有登记的记录是两个独立实体，不因同名自动合并。允许的映射方法：`source_authoritative`、`crosswalk`（以及后续扩展的 `name_in_scope`，需附证据）。

## 2. 值状态（关键约定）

未知值绝不写成 `0` / `""` / `[]`。字段状态通过 `field_evidence.evidence_status` 表达：

| 状态 | 含义 |
|---|---|
| `known` | 来源明确给出且已解析；数值确实可以为 0。 |
| `absent` | 已检查来源，该字段没有提供。 |
| `not_applicable` | 依据明确规则该字段不适用。 |
| `unparsed` | 原文存在，解析器尚不支持。 |
| `conflict` | 多个候选值冲突。 |
| `inferred` | 有记录的推导，不作为直接来源事实。 |

类型化表中的可空列（如 `skills.mana_cost`）为 `NULL` 时表示“未知”，具体是 absent/unparsed/conflict 需查 `field_evidence`（按 `entity_id + field_path`）。`source_records.parse_status` 与 `fetch_status` 是独立枚举，不与字段状态混用。

单位约定：

- 时间统一毫秒（`*_ms`）；来源为秒时按 ×1000 转换，原始值与单位写入 evidence。
- 百分比/倍率统一存规范化比率（例如来源 `18%` → `0.18`，`更多伤害 20%` → `0.2`）；`increased`/`more` 等类别写在 `effect_values.value_kind`。
- 附加伤害效能（added damage effectiveness）存来源原始百分数数值（`100` 表示 100%），`field_evidence.unit` 记录 `percent_raw`。
- 逐级/阶级数值存 `effect_values.level/tier`；范围存 `value_min/value_max`。
- 表达式只存文本/结构化参数，不做 eval。

## 3. 表说明

### 3.1 元数据与来源

| 表 | 说明 |
|---|---|
| `dataset_meta` | 单行；schema/normalizer/mapping 版本、状态（staging/valid/partial/published）、目标游戏版本与兼容性结论。 |
| `dataset_source_snapshots` | dataset 引用了哪些来源快照（多对多）。 |
| `source_snapshots` | 每个来源快照：声称版本、版本证据 JSON、fetch 模式、起止时间、状态（ok/partial/failed/blocked）。 |
| `raw_resources` | 每个原始资源：URL、本地路径、SHA-256、大小、Content-Type、HTTP 状态、ETag/Last-Modified、抓取时间、fetch 状态、role。 |
| `source_records` | 来源自身记录：kind/scope/source_id、定位、**完整原始 JSON**、parse_status。这是重解析的唯一输入。 |
| `mapping_registry` | 映射注册表（保留字段；当前 crosswalk 在文件中维护）。 |

### 3.2 实体与名称

| 表 | 说明 |
|---|---|
| `entities` | 内部实体：`kind`、`canonical_name`、`record_status`（active/provisional/unresolved/merged）、`primary_source`、`extensions`。 |
| `entity_names` | 名称/别名，按语言分开（en/zh/und），保留 `name_key`（本地化 key）与来源。 |
| `source_id_map` | 来源 ID → 内部实体；唯一键 (snapshot, kind, scope, source_id)，带映射方法与证据。 |
| `classes` / `masteries` | 职业/专精：源 ID、解锁说明、原始扩展。 |
| `skills` | 技能标量：类型、职业/专精、施放/引导/冷却/法力、原始标签位掩码。 |
| `skill_components` | 技能组件：类型、序号、伤害类型、基础伤害、附加伤害效能、完整配置 JSON。 |
| `trees` | 技能树/被动树/编织树：类型、拥有者、职业、源树 ID、节点数。 |
| `nodes` | 节点：所属树、类型、等级上限、序号、布局坐标、描述。 |
| `node_edges` | 前置边：from/to、条件类型、所需点数、原始要求。 |

### 3.3 标签、属性、效果

| 表 | 说明 |
|---|---|
| `tags` / `entity_tags` | 标签语义与实体关联，保留原始位值。 |
| `stats` | 属性定义：名称、单位、原始枚举。 |
| `effects` / `effect_values` | 效果原文与解析状态；数值按 kind/level/tier/range 拆行。 |
| `field_evidence` | **字段级证据**：对象、字段路径、来源记录、原值、规范值、单位、提取方法、状态、定位。 |
| `field_selections` | 多候选值时选中了哪条 evidence 及理由，其余候选保留。 |
| `data_issues` | 数据问题：类型、严重性、来源、实体、期望/实际、定位、状态。 |

### 3.4 状态、异常、装备

| 表 | 说明 |
|---|---|
| `buffs` | Buff/Debuff：类别、持续、最大层数、目标范围、生命周期规则 JSON（刷新/消耗/叠层等）。 |
| `ailments` | 异常：伤害类型、基础伤害、持续、叠层规则、缩放标签。 |
| `items` | 装备/基底/暗金/套装：类别、类型、等级/职业要求、固有文本。 |
| `affixes` / `affix_tiers` | 词缀：前缀/后缀/神像等类别、源 ID、适用类型、逐阶等级与 roll 范围。 |
| `item_affixes` | 装备与固有/特殊词缀关系（定义层，不是玩家装备实例）。 |
| `set_memberships` | 套装成员关系。 |
| `idols` / `blessings` / `season_entities` | 神像、祝福、赛季实体。 |
| `minions` | 召唤物：类型、基础属性 JSON、继承规则。 |

### 3.5 关系

`entity_relationships`：起点实体、关系类型、终点实体或原始未解析引用、`resolution_status`（resolved/unresolved/ambiguous）、证据、限定条件（标签/等级/条件 JSON）。

关系类型（受控词表，可扩展）：`belongs_to_class`、`has_tree`、`contains_node`、`requires_node`、`grants_buff`、`applies_buff`、`modifies_skill`、`triggers_skill`、`replaces_skill`、`applies_ailment`、`summons_minion`、`uses_skill`、`modifies_stat`、`converts_damage`、`consumes_buff`、`has_affix`、`set_member`、`drops_from`、`has_component`。

`unresolved` 关系必须保留 `raw_target_ref` 原文并出现在质量报告中；禁止伪造目标实体绕过外键。

## 4. 索引与约束

- 全部外键启用 `PRAGMA foreign_keys=ON`；`source_id_map` 四元组唯一；`affix_tiers (affix_entity_id, tier_index)` 唯一；关系去重键含 `raw_target_ref`。
- 完整性要求：`PRAGMA integrity_check = ok`、`PRAGMA foreign_key_check` 为空。
- 逻辑指纹：`dataset_fingerprints.logical_sha256`，按稳定内容序列化后哈希，不含抓取时间/自增 ID 等非内容字段；文件 SHA-256 另存于 manifest，用于搬运损坏检测。

## 5. 已知限制（v1）

- `effects` 层只对来源中结构化的属性/修饰符建模；自然语言效果保留原文并标 `unparsed`，不自动转公式。
- 生命周期规则（刷新/消耗/互斥）多数来源不提供，`buffs.lifecycle` 为空并附 absent evidence，属正常状态而非缺失实现。
- 数值缩放若来源只给描述文本，规范值为 `NULL` + `inferred/unparsed` evidence。
- 中文名称只在来源提供中文时入库；当前两站以英文为主，`entity_names` 保留 `name_key` 供后续本地化。


## 6. 2026-11-01 审查修复新增字段与语义（migration 0003）

审查（作者本地审查记录，不随仓库分发）后新增以下列/语义；旧库仍可读，新列在旧 dataset 中为 NULL。

### 6.1 Buff：UI 展示值与机制值分离（P1-6）

| 列 | 含义 |
|---|---|
| `buffs.ui_max_instances` | 来源 UI 展示的 maxInstances 原值（如 0），**不代表无限层数** |
| `buffs.max_stacks_basis` | 机制值来源：`node_text`（节点参数证据）/ `raw_ui`（仅 UI 值）/ `confirmed` / `unknown` |
| `buffs.base_duration_ms` | 机制基础持续时间（如有节点/参数证据） |
| `buffs.duration_basis` | 同上来源标记 |
| `buffs.ui_flags` | noFixedDuration/newStacksRefreshDuration 等原始 UI 标志 JSON |

规则：`max_stacks`/`duration_ms` 只在有机制证据时写入；UI 0 写 `ui_max_instances=0` + `max_stacks_basis='raw_ui'`，不得解释为 uncapped；节点文本参数写 `node_text` 并把 locator/qualifiers 写入 evidence。`lifecycle` 不含 `uncapped`。

### 6.2 effect→stat 关联（P1-2）

| 列 | 含义 |
|---|---|
| `effect_values.stat_id` | 解析到的属性实体（linked/composite 时非空） |
| `effect_values.stat_link_status` | `linked`（来源内唯一直接 id）/ `composite`（基础属性 + specialTag/tags/extraTag 限定）/ `unknown_namespace`（该来源命名空间无对应属性）/ `ambiguous`（多候选）/ `unparsed` / `not_applicable` |
| `effect_values.stat_link_evidence` | JSON：来源、命名空间、原始 property、匹配规则、限定符、候选列表 |
| `effects.effect_scope` | effect 上下文（node/skill/item/affix/buff…） |

规则：只按来源自带的数字 property id 解析，不按名称模糊合并；letools 与 tunklab 命名空间隔离；组合属性保留限定符，不拆成假属性。

### 6.3 装备/词缀/专精语义（P2-1）

| 表/列 | 含义 |
|---|---|
| `items.is_set` | 是否套装件（来源 isSetItem） |
| `items.gear_category` | `weapon`/`armor`/`accessory`/`idol`/`blessing`/`lens`/`other`（由来源 item_type 推导） |
| `items.equip_slot` | `helm`/`body`/`belt`/`boots`/`gloves`/`weapon`/`offhand`/`amulet`/`ring`/`relic`/`idol`…（可追溯时） |
| `affixes.affix_slot` | `prefix`/`suffix`/`unknown`；与来源类别 `affix_type`（singleAffixes/multiAffixes）分开；当前由来源 type 0/1 推断（inferred），不是官方枚举 |
| `masteries.branch_kind` | `base_class`/`mastery`/`unknown` |
| `masteries.is_base` | 1=来源中的基础职业分支行（如 `N.0`），不代表可选专精 |

### 6.4 关系与证据扩展

- `entity_relationships.resolution_status` 允许 `resolved` / `unresolved` / `ambiguous` / `candidate`；candidate 表示来源文本/名称启发式得到的候选关联，附 `raw_target_ref`/`qualifiers`，不得当作已确认机制。
- 新增关系类型示例：`grants_buff_candidate`、`modifies_buff_candidate`、`modifies_buff_parameter_candidate`、`node_grants_ability_candidate`、`skill_has_item_source_candidate`、`tree_node_modifies_skill`、`tree_node_applies_ailment`、`passive_node_applies_ailment`、`belongs_to_mastery`、`item_mod_key_*`（unresolved 原样保留）。
- Buff 参数候选使用 `effects.effect_category`=`buff_parameter_candidate`/`buff_parameter_modifier`，数值在 `effect_values`，节点→Buff 关系在 `entity_relationships`。
- `node_edges` 是节点前置关系的唯一事实来源；关系表不再重复 `requires_node`。

### 6.5 版本与发布语义（P1-4/P1-5/P2-2）

- `dataset_meta.compatibility_conclusion` 必须是 JSON：`{"target_game_version","target_season","sources":{source:version},"decision":"compatible","evidence":...}`；Validator 同时比对 `source_snapshots.claimed_version` 与 `version_evidence` 中的 page/module 版本；不一致须在 `data/mappings/version-compat.json` 有带证据的批准对。
- release 不可变：同 dataset_id 同内容幂等、不同内容拒绝；新内容使用新 dataset id；`data/current.json` 最后原子切换；partial release 默认不切 current。
- snapshot id 含 run 内容后缀；已存在快照内容变化时自动新建 `-rN` 目录，不覆盖发布依据。

### 6.6 数据层级与限制

- `known` evidence = 来源明确且已解析；UI 展示值不等于机制值；`inferred`/`candidate` 明确弱于 `known`/`resolved`。
- 仍需机制研究/资源补充：技能私有 property id（6,199 条 unknown_namespace）、item mod 不透明 key、tooltip 特殊效果、部分 Buff 来源与生命周期、season_entities 结构化来源。

> 2026-11-04 数据修正：Fiery Dragon Shoes 词条实际为 "Fire Penetration with Ignite"（property 43 + specialTag 1）。已发布 phase1-dataset-003（stats.extensions 新增 special_tag_display / special_tag_display_source，来源为单属性 tag 中性 affix 显示名）并重新导入本地 BD（rev-c746411ccebee9c0, seq 8）。详细修正记录属作者本地材料（不随公开仓库分发）。
