# 机制证据与裁决索引（mechanisms）

本文件回答一个问题：**当前计算里每一条规则/每个数值，凭什么这么说？它的适用范围是什么？哪些还只是假设？**
> 2026-11-13 v0.4.2：新增 `skill-level-scope-and-point-budget`（编辑器技能等级作用域与点数预算），并新增 Throne of Ambition 编辑器证据（`docs/evidence/conditional/`）。

计算实现入口在 [`tools/phase3/`](../tools/phase3/README.md)，操作流程见 [workflow.md](workflow.md)，
支持范围与限制见 [current-status.md](current-status.md)。

> 本文件是**索引**，不是新证据。权威记录在规则包
> [`tools/phase3/rules/ruleset-bh-stage1-v4.json`](../tools/phase3/rules/ruleset-bh-stage1-v4.json)、
> [`tools/phase3/rules/ruleset-fr-hit-stage1-v2.json`](../tools/phase3/rules/ruleset-fr-hit-stage1-v2.json)、
> 条件效果注册表 [`tools/phase3/rules/conditional-effects-bh-stage1-v1.json`](../tools/phase3/rules/conditional-effects-bh-stage1-v1.json) 与证据文件本身；
> 机制/技能的元数据与参数绑定在 [`knowledge/`](../knowledge/README.md)；
> 可复用结论与作者归档定位在 [`docs/evidence/extracted/`](evidence/extracted/README.zh-CN.md)。
> 索引中的数字来自这些文件与当前 run 的 `rule-ledger.json` / `run-manifest.json`。

## 1. 证据类别与状态词表

| 类别 | 含义 | 典型载体 | 可否单独作为“已确认机制” |
|---|---|---|---|
| **游戏数据**（`game_data`） | 从来源数据或本地游戏资源直接读出的字段/数值 | `data/releases/<dataset-id>/data.sqlite`（`scripts/fetch_dataset.ps1` 获取）、本地 1.5.3 资源 | 可，但须注明数据版本（1.5.2 数据 vs 1.5.3 本地资源） |
| **反编译证据**（`reverse_engineering`） | 对本地 1.5.3 二进制/资源做只读逆向得到的字段与代码路径 | [`docs/evidence/reverse-engineering/`](evidence/reverse-engineering/README.zh-CN.md) | 可，但结论要区分“代码路径已读”与“运行时行为已观测” |
| **游戏内实测**（`user_in_game_measurement`） | 用户在游戏内受控测试得到的面板/日志数据 | [`user-in-game-measurements-2026-11-05.json`](evidence/reverse-engineering/user-in-game-measurements-2026-11-05.json)、[`user-heat-wave-validation-2026-11-05.json`](evidence/reverse-engineering/user-heat-wave-validation-2026-11-05.json) | 可，须绑定测试条件（目标抗性/DR/层数） |
| **用户裁决**（`user_rule`） | 用户对建模口径的决定（不是游戏事实） | 规则包中 `status=confirmed` 且证据含 `user_rule` 的条目 | **不可**。它是产品口径，须显式标注“用户裁决” |
| **实现假设**（`assumption` / `derived`） | 记账方式、集成顺序、派生展示值 | 规则包中 `assumed` 条目；注册表 `research.open_questions` | **绝对不可**写成已确认机制 |

状态词表（与规则包一致）：

| 状态 | 含义 | 报告中如何呈现 |
|---|---|---|
| `confirmed` | 有上述可用证据支持 | 正常计入 |
| `derived` | 由已确认规则算出的派生值（火抗、点燃伤害、冷却等），不是独立证据 | 正常计入，但不能再被当作新证据引用 |
| `assumed` | 已实现、口径未验证 | 计入并在结果顶部标注 `assumption` |
| `unresolved` | 未确认，默认不计入或单独列出 | 不计入主数值；在 `issues` / 报告“未解决”区展示 |
| 条件效果 `support.level` | `engine_supported` / `needs_rule_mapping` / `needs_action_or_time_simulation` / `mechanism_unresolved` | 条件 Buff 页逐项显示“已计入 / 未计入 / 仅单次 / 需规则映射 / 未支持” |

硬规则：**第三方参考模型（外部 planner 模型）不是游戏真值**，只能作为线索（`third_party_reference`）；
本地数据目录不是独立验证；只有证据类别与状态同时成立，才允许在文档里写“已确认”。

## 2. 适用范围与版本边界

- **数据层**：`data/current.json` 指向的 dataset（本轮为 `phase1-dataset-003`，游戏数据 1.5.2，内容 sha256 见该文件）。
  dataset 不随公开仓库分发，需先用 `scripts/fetch_dataset.ps1` 获取并校验 sha256。
- **机制证据**：逆向/实测证据来自作者本地 **1.5.3** 游戏文件与受控测试（[`docs/evidence/`](evidence/)）；
  1.5.3 逆向结论与 1.5.2 数据数值是两套来源，报告里保持双来源标注。
- **实例私有裁决**：神像/折射栏位选择、任务奖励、目标场景（怪物等级、抗性、Boss/稀有、双星覆盖、中心距离）等
  只对声明它的当前实例成立，写在当前 case/scenario 中，**不是通用默认值**。公开版不含案例输入，
  需用自己的存档重新绑定。
- **当前支持范围**：Void Cleave / Flame Reave / Black Hole 的 execution 切片；Ballista 为 partial。
  不是完整战斗模拟，见 [current-status.md](current-status.md)。
- **规则包版本**：当前正式版本为 BH `bh-stage1-v4`、FR `fr-hit-stage1-v2`；
  旧版本（BH v2/v2b/v3、FR v1）已从公开树移除，BH v1 仅因 legacy 引擎默认值保留。
  保留/删除依据见 [`docs/evidence/extracted/version-audit.json`](evidence/extracted/version-audit.json)。

## 3. 查询入口与索引

```powershell
.\scripts\le.ps1 knowledge search --query "node:fr11mv:16"   # 或 common-monster-dr、点燃、R13 …
.\scripts\le.ps1 knowledge show   --id fr-node-reflash
.\scripts\le.ps1 knowledge rebuild                            # 从 JSON 重建索引（可删除重建）
```

查询返回逐条匹配强度（exact/candidate）、适用性与未命中的查询项；只有 exact + satisfied 参与自动绑定。
`data/cache/knowledge.sqlite` 与 `knowledge/INDEX.generated.md` 是可重建生成物，不是知识源。
详见 [`knowledge/README.md`](../knowledge/README.md) 与 [workflow-interface §5.1](contracts/workflow-interface.md)。
规则条数：BH v4 39 条、FR v2 24 条
（来自规则包；`rule-ledger.json` 只包含本次 run 实际使用的规则）。

### 3.1 共享算子（`common-*`）

| mechanism_id | 标题 | evidence/implementation | 实现入口 |
|---|---|---|---|
| `common-conditional-state-eval` | 条件状态求值（条件效果通道） | derived/validated | `le_calc.conditional.evaluate`.`evaluate_scenario` |
| `common-frequency-cooldown` | 频率/冷却/施法时间共享算子 | derived/validated | `le_calc.cooldown`.`compute_cooldown` |
| `common-increased-aggregate` | increased/reduced 加算池 | confirmed/validated | `le_calc.damage`.`_collect_increased` |
| `common-ledger` | 来源账/规则账与一致性检查 | derived/validated | `le_calc.ledger`.`build_source_ledger` |
| `common-monster-dr` | 怪物等级减伤（Monster DR） | confirmed/validated | `le_calc.monster_dr`.`describe` |
| `common-more-groups` | more 分组与逐组独立相乘 | confirmed/validated | `le_calc.damage`.`group_more_rows` |
| `common-tag-match` | 伤害标签适用性匹配 | confirmed/validated | `le_calc.damage`.`tags_match` |
| `common-target-resistance-penetration` | 目标抗性/穿透结算（穿透可压到 0 以下） | confirmed/validated | `le_calc.damage`.`_target_multiplier` |

### 3.2 Black Hole（bh2）规则清单

权威源 [`tools/phase3/rules/ruleset-bh-stage1-v4.json`](../tools/phase3/rules/ruleset-bh-stage1-v4.json)，共 39 条。规则正文与 `evidence[]` 定位在该文件内；下表只做索引。

| rule_id | 摘要 | status | 责任机制条目（knowledge/mechanisms） |
|---|---|---|---|
| `rule-bh-component` | Black Hole 本体组件与单位 | confirmed | `bh-body-component` |
| `rule-bh-total-damage` | 本体总伤害口径（与 tick 模型一致） | confirmed | — |
| `rule-bh-tick-schedule` | 本体事件离散化（ceil 满跳；与参考模型的差异见 evidence） | confirmed | `bh-body-tick-model` |
| `rule-bh-duration` | 本体持续时间加算 | confirmed | `bh-duration` |
| `rule-bh-red-giant` | Red Giant 冷转火 | confirmed | `bh-red-giant` |
| `rule-bh-binary-system` | Binary System 双星模型 | confirmed | `bh-binary-system` |
| `rule-bh-binary-more-interpretation` | Binary System 的 +100% 是额外独立 more（已确认） | confirmed | — |
| `rule-bh-more-aggregation` | more 聚合规则 | confirmed | `bh-node-more-10`、`bh-node-more-2`、`bh-node-more-28`、`bh-node-more-7`、`bh-skill-tree-more-nodes`、`common-more-groups` |
| `rule-bh-attribute-scaling` | Intelligence 属性缩放（本体） | confirmed | `bh-attribute-scaling` |
| `rule-bh-weakened-reality` | Weakened Reality 仅本体 | confirmed | `bh-weakened-reality` |
| `rule-ignite-base` | Ignite 基础伤害与持续时间 | confirmed | `bh-ignite-core` |
| `rule-ignite-inheritance` | Ignite 继承 Black Hole 技能树增伤 | confirmed | `bh-ignite-inheritance` |
| `rule-ignite-binary-excluded` | Ignite 继承 Binary System 的 +100% more（已确认） | confirmed | — |
| `rule-ignite-duration` | Ignite 持续时间乘区 | confirmed | `bh-ignite-duration-sources` |
| `rule-ignite-heat-wave` | Heat Wave 每次判定的期望层数 | confirmed | `bh-ignite-heat-wave` |
| `rule-ignite-heat-wave-timing` | Heat Wave 判定时序（用户确认） | confirmed | — |
| `rule-ignite-scaling-pool` | Ignite 玩家侧 inc 池 | assumed | `bh-ignite-scaling-pool` |
| `rule-ignite-elemental-tag` | Elemental 标签增伤进入 Ignite（用户确认） | confirmed | — |
| `rule-ignite-attribute-scaling` | Intelligence 属性缩放传递给 Ignite（用户确认） | confirmed | `bh-ignite-attribute-scaling` |
| `rule-fire-penetration-with-ignite` | Fire Penetration with Ignite（装备来源；用户确认 + 数据集 003 修正） | confirmed | `bh-ignite-penetration` |
| `rule-penetration-dot` | 穿透作用于持续伤害且可压到 0 以下 | confirmed | `bh-target-side-settlement`、`common-target-resistance-penetration` |
| `rule-target-scenario` | 目标场景（用户规则 7） | confirmed | `bh-target-scenario-conditions` |
| `rule-cooldown-base` | 充能基础值与固定调整 | confirmed | `bh-cooldown-charges` |
| `rule-cooldown-icr` | 冷却公式与 Recurrence 映射（已确认） | confirmed | `bh-cooldown-cdr-icr` |
| `rule-cast-time` | 施法时间公式（公式形状已确认，基础常数来自游戏数据） | confirmed | `bh-cast-time` |
| `rule-black-hole-count-limit` | 每次施放 1 个黑洞；不同施放可并存（用户确认） | confirmed | `bh-black-hole-instances` |
| `rule-black-hole-recast` | 重施行为：每次施放创建独立黑洞（用户确认并存） | confirmed | — |
| `rule-pyromancer-ignite-chance` | Pyromancer 的 Ignite Chance 不参与 Heat Wave（已确认） | confirmed | — |
| `rule-crescendoing-rage` | Crescendoing Rage（本阶段不纳入，后续阶段处理） | unresolved | — |
| `rule-preparation` | Preparation 下一次施法状态（未确认） | unresolved | — |
| `rule-ignite-damage-slices` | Ignite 伤害的连续流记账与切片事件（v1） | assumed | `bh-ignite-damage-flow` |
| `rule-target-monster-dr-table` | 怪物等级减伤表（tunklab） | confirmed | `common-monster-dr` |
| `rule-target-monster-dr-application` | 怪物减伤应用方式（目标侧独立乘区） | assumed | `bh-monster-dr-integration` |
| `rule-bh-tidal-force` | Tidal Force（本体与 Ignite 的独立 more 乘区） | confirmed | — |
| `rule-bh-flaming` | Flaming（火焰分量的独立 more） | confirmed | — |
| `rule-bh-singularity` | Singularity（+100% more；单次施放 1 个黑洞） | confirmed | — |
| `rule-bh-supermassive` | Supermassive（Boss/稀有目标的独立 more） | confirmed | — |
| `rule-bh-umbral-core` | Umbral Core（本体与 Ignite 的独立 more） | confirmed | — |
| `rule-cooldown` | 技能树冷却/充能节点（Recurrence / Endless Maw / Pocket Dimension） | confirmed | — |

### 3.3 Flame Reave（fr11mv）规则清单

权威源 [`tools/phase3/rules/ruleset-fr-hit-stage1-v2.json`](../tools/phase3/rules/ruleset-fr-hit-stage1-v2.json)，共 24 条。规则正文与 `evidence[]` 定位在该文件内；下表只做索引。

| rule_id | 摘要 | status | 责任机制条目（knowledge/mechanisms） |
|---|---|---|---|
| `R1` | 每次施放对同一目标的命中次数 | confirmed | `fr-hit-events-weights`、`fr-node-flame-caller`、`fr-node-reflash` |
| `R2` | 双持合成 | confirmed | `fr-hit-dual-wield-added` |
| `R3` | 攻速基础公式 | confirmed | `fr-hit-attack-speed`、`fr-node-dancing-flames` |
| `R4` | added damage 缩放 | confirmed | `fr-hit-added-scaling`、`fr-hit-single-hit-chain` |
| `R5` | 无类型 added damage 的伤害类型 | confirmed | — |
| `R6` | mana 相关不建模（仅保留阈值估算） | confirmed | — |
| `R7` | Enchant Weapon 近战命中常驻激活 | confirmed | — |
| `R8` | 无伤害转换（fire_only 组件策略） | confirmed | — |
| `R9` | 标签过滤规则 | derived | `fr-hit-increased-pools`、`fr-hit-tag-matching` |
| `R10` | Firebrand 层数作用范围（仅 Firebrand 自身） | confirmed | — |
| `R11` | 额外命中 50% less 伤害权重（第 4 击） | confirmed | — |
| `R12` | 目标侧：抗性与穿透、怪物 DR | assumed | `fr-hit-target-side` |
| `R13` | 冷却与持续 DPS 口径 | assumed | `fr-hit-cooldown-icr` |
| `R14` | mana 条件估算 | confirmed | — |
| `R15` | Blade Weaver 条件 | derived | — |
| `R16` | 技能等级 27（只影响节点数量） | confirmed | `fr-skill-level-scaling` |
| `R17` | 组件口径：Fire 单类型 | assumed | — |
| `R18` | Rhythm of Fire 循环未做时间模拟 | assumed | `fr-hit-rhythm-rotation`、`fr-node-versatility` |
| `R19` | Enchant Weapon active/passive 参数与常驻假设 | derived | `fr-hit-more-groups` |
| `R20` | Army of Skin 高血量条件乘区 | assumed | — |
| `R21` | Reflash / Flame Caller 额外命中伤害权重 | derived | — |
| `R22` | Rhythm of Fire 纯 FR 循环平均 | assumed | — |
| `R24` | 多击次数条件化 | confirmed | — |
| `R25` | OQ6 暴击类被动计入暴击率 | assumed | `fr-hit-critical-expected` |

### 3.4 条件效果注册表

权威源 [`tools/phase3/rules/conditional-effects-bh-stage1-v1.json`](../tools/phase3/rules/conditional-effects-bh-stage1-v1.json)，共 17 个效果；dataset `phase1-dataset-003`。support.level 的语义见 §1；固定条件估算不模拟获取/维持成本，条件重算不改写主 DPS 基线。

**`engine_supported`**

| effect_id | 名称 | 责任机制条目 |
|---|---|---|
| `idol.fire_damage_mana_threshold` | Grand Arcane Omen Idol · 火伤翻倍（idols.2） | `bh-cond-idol-fire-doubling` |
| `idol.fire_damage_mana_threshold.idols_3` | Grand Arcane Omen Idol · 火伤翻倍（idols.3） | — |
| `main_skill.supermassive` | Supermassive | `bh-supermassive` |
| `main_skill.weakened_reality` | Weakened Reality | — |

**`needs_rule_mapping`**

| effect_id | 名称 | 责任机制条目 |
|---|---|---|
| `meteor.craterborn` | Craterborn | `bh-cond-craterborn` |
| `flame_ward.infusion` | Infusion | `bh-cond-infusion` |
| `frost_wall.chilled_touch` | Chilled Touch | `bh-cond-chilled-touch` |
| `enchant_weapon.eternal_flames.passive` | Eternal Flames（被动） | `bh-cond-eternal-flames-passive` |
| `enchant_weapon.eternal_flames.active` | Eternal Flames（激活） | `bh-cond-eternal-flames-active` |
| `idol.throne_of_ambition` | Throne of Ambition | `bh-cond-throne-of-ambition` |
| `passive.preparation` | Preparation（下一次元素法术） | `bh-cond-preparation` |
| `idol_altar.layout_cdr` | Pyramidal Altar · 布局条件 CDR | `bh-cond-idol-altar-layout-cdr` |
| `flame_ward.fire_aura` | Fire Aura（Flame Ward 自身伤害） | `bh-cond-flame-ward-fire-aura` |

**`needs_action_or_time_simulation`**

| effect_id | 名称 | 责任机制条目 |
|---|---|---|
| `enchant_weapon.conflagrate` | Conflagrate / Searing Conflagration（动作） | `bh-cond-conflagrate` |
| `ring.crescendoing_rage` | Crescendoing Rage | `bh-cond-crescendoing-rage` |

**`mechanism_unresolved`**

| effect_id | 名称 | 责任机制条目 |
|---|---|---|
| `passive.frozen_steel` | Frozen Steel（4 点门槛） | `bh-cond-frozen-steel` |
| `equipment.apex_glyph_conversion` | Apex of Thought · Glyph of Dominion 异常几率转换 | `bh-cond-apex-glyph-conversion` |


### 3.5 机制条目总表

共 127 条。JSON 是权威源；索引 `knowledge/INDEX.generated.md` 由 `le.ps1 knowledge rebuild` 生成。提取证据见 [`docs/evidence/extracted/`](evidence/extracted/README.zh-CN.md)。

| mechanism_id | 标题 | evidence/implementation | 主 rule_ref |
|---|---|---|---|
| `attribute-strength-to-brutality` | 力量转换为 Brutality 与按法耗独立近战增伤 | derived/reusable | ruleset_rule:attribute-conversions-v1:brutality |
| `ballista-death-explosion` | Ballista 到期/销毁爆炸与敏捷倍率 | confirmed/reusable | ruleset_rule:knowledge/definitions/ballista-armed-construct… |
| `ballista-dexterity-frequency-and-increased` | Ballista 敏捷伤害与放置/攻击速度 | derived/reusable | ruleset_rule:knowledge/definitions/ballista-armed-construct… |
| `ballista-explosion-damage-composition` | Ballista 爆炸伤害构成（ability_gen327） | derived/reusable | ruleset_rule:knowledge/definitions/ballista-armed-construct… |
| `ballista-potion-construction-window` | 药剂条件与 Ballista 召唤时状态 | derived/reusable | ruleset_rule:knowledge/definitions/ballista-armed-construct… |
| `ballista-projectile-explosion` | Ballista 命中爆炸与火伤/点燃 | confirmed/draft | ruleset_rule:本轮 DPS 口径不含 bolt 命中爆炸（用户指定只算 Armed Constructio… |
| `ballista-summon-limit-lifetime` | Ballista 召唤上限、存续与替换事件 | confirmed/reusable | ruleset_rule:knowledge/definitions/ballista-armed-construct… |
| `ballista-trap-triggered-summon` | Explosive Trap 触发 Ballista 的来源链 | derived/reusable | ruleset_rule:knowledge/definitions/ballista-armed-construct… |
| `bh-attribute-scaling` | Intelligence 属性缩放（本体） | confirmed/validated | ruleset_rule:rule-bh-attribute-scaling |
| `bh-baseline-profiles` | 双基线口径：legacy_single_skill 与 build_base | derived/validated | none:基线 profile 是案例/场景口径（conditional registry baseline_prof… |
| `bh-binary-system` | Binary System 双星结构 + 独立 more | confirmed/validated | ruleset_rule:rule-bh-binary-system |
| `bh-black-hole-instances` | 黑洞实例：每次施放 1 个、多次施放并存 | confirmed/validated | ruleset_rule:rule-black-hole-count-limit |
| `bh-body-component` | Black Hole 本体组件与单跳伤害 | confirmed/validated | ruleset_rule:rule-bh-component |
| `bh-body-tick-model` | 本体跳数离散化与总伤害口径 | confirmed/validated | ruleset_rule:rule-bh-tick-schedule |
| `bh-cast-time` | 施法时间（释放速度） | confirmed/validated | ruleset_rule:rule-cast-time |
| `bh-charge-schedule-multi-instance` | Black Hole 充能调度（charges/recharge/CDR/ICR/flat）与多实例并发 | derived/reusable | ruleset_rule:plan-v3 schedule.charges/action_policies |
| `bh-cond-apex-glyph-conversion` | Apex of Thought · Glyph of Dominion 异常几率转换 | unresolved/draft | conditional_effect:equipment.apex_glyph_conversion |
| `bh-cond-chilled-touch` | Chilled Touch（Frost Wall + 目标冰缓：+more 伤害） | assumed/validated | conditional_effect:frost_wall.chilled_touch |
| `bh-cond-conflagrate` | Conflagrate（Enchant Weapon 激活：消费点燃并立即结算） | assumed/not_implemented | conditional_effect:enchant_weapon.conflagrate |
| `bh-cond-craterborn` | Craterborn（Meteor 后：火穿 + 施法速度） | confirmed/validated | conditional_effect:meteor.craterborn |
| `bh-cond-crescendoing-rage` | Crescendoing Rage（每第三次施放 +more） | assumed/draft | conditional_effect:ring.crescendoing_rage |
| `bh-cond-eternal-flames-active` | Eternal Flames 激活（节点效果翻倍） | confirmed/validated | conditional_effect:enchant_weapon.eternal_flames.active |
| `bh-cond-eternal-flames-passive` | Eternal Flames 被动（+Ignite 持续时间） | confirmed/validated | conditional_effect:enchant_weapon.eternal_flames.passive |
| `bh-cond-flame-ward-fire-aura` | Flame Ward · Fire Aura（自身伤害，不进入 BH 计算） | confirmed/not_implemented | conditional_effect:flame_ward.fire_aura |
| `bh-cond-frozen-steel` | Frozen Steel（目标冰缓/冻结时的条件增伤，4 点门槛） | unresolved/not_implemented | conditional_effect:passive.frozen_steel |
| `bh-cond-idol-altar-layout-cdr` | Pyramidal Altar（布局条件 ICR） | unresolved/draft | conditional_effect:idol_altar.layout_cdr |
| `bh-cond-idol-fire-doubling` | Grand Arcane Omen Idol · 最大法力门槛火伤翻倍（idols.2 + idols.3，合并登记） | confirmed/validated | conditional_effect:idol.fire_damage_mana_threshold |
| `bh-cond-infusion` | Infusion（Flame Ward 生效期间 +increased 火伤） | confirmed/validated | conditional_effect:flame_ward.infusion |
| `bh-cond-preparation` | Preparation（下一次元素法术 +increased 伤害） | assumed/draft | conditional_effect:passive.preparation |
| `bh-cond-throne-of-ambition` | Throne of Ambition（Boss/稀有命中叠层：每层 +more 火/冰） | confirmed/validated | conditional_effect:idol.throne_of_ambition |
| `bh-cooldown-cdr-icr` | 冷却公式：CDR 乘算缩减 + ICR 独立分母 | confirmed/validated | ruleset_rule:rule-cooldown-icr |
| `bh-cooldown-charges` | 充能基础值、固定秒数与额外充能 | confirmed/validated | ruleset_rule:rule-cooldown-base |
| `bh-dot-tick-model` | Black Hole 本体纯 DoT 逐跳模型（ceil_full_ticks） | derived/reusable | ruleset_rule:plan-v3 components[].duration_s/tick_interval_… |
| `bh-duration` | 本体持续时间加算 | confirmed/validated | ruleset_rule:rule-bh-duration |
| `bh-heat-wave-ignite-clock` | Black Hole Ignite：Heat Wave 独立施加时钟与期望层数 | derived/reusable | ruleset_rule:plan-v3 schedule.ignite |
| `bh-ignite-attribute-scaling` | Intelligence 属性缩放传递给 Ignite | confirmed/validated | ruleset_rule:rule-ignite-attribute-scaling |
| `bh-ignite-core` | Ignite 基础伤害与持续时间乘区 | confirmed/validated | ruleset_rule:rule-ignite-base |
| `bh-ignite-damage-flow` | Ignite 连续流记账与尾部伤害 | assumed/validated | ruleset_rule:rule-ignite-damage-slices |
| `bh-ignite-duration-sources` | Ignite 持续时间乘区来源池 | confirmed/validated | ruleset_rule:rule-ignite-duration |
| `bh-ignite-heat-wave` | Heat Wave：周期判定与期望层数 | confirmed/validated | ruleset_rule:rule-ignite-heat-wave |
| `bh-ignite-inheritance` | Ignite 继承 Black Hole 技能树 more | confirmed/validated | ruleset_rule:rule-ignite-inheritance |
| `bh-ignite-penetration` | Ignite 专属火穿与 DoT 穿透 | confirmed/validated | ruleset_rule:rule-fire-penetration-with-ignite |
| `bh-ignite-scaling-pool` | Ignite 玩家侧 inc 池与标签口径 | assumed/validated | ruleset_rule:rule-ignite-scaling-pool |
| `bh-loop-action-policies` | eager/patient 循环动作策略与窗口 | derived/validated | none:eager/patient 是 DPS 计算器的动作策略（ruleset.action_policies），… |
| `bh-monster-dr-integration` | 怪物 DR 接入 BH 目标侧（独立乘区） | assumed/validated | ruleset_rule:rule-target-monster-dr-application |
| `bh-node-more-10` | Black Hole 节点 10：当前点数 more 映射 | confirmed/validated | ruleset_rule:rule-bh-more-aggregation |
| `bh-node-more-2` | Black Hole 节点 2：当前点数 more 映射 | confirmed/validated | ruleset_rule:rule-bh-more-aggregation |
| `bh-node-more-28` | Black Hole 节点 28：当前点数 more 映射 | confirmed/validated | ruleset_rule:rule-bh-more-aggregation |
| `bh-node-more-7` | Black Hole 节点 7：当前点数 more 映射 | confirmed/validated | ruleset_rule:rule-bh-more-aggregation |
| `bh-red-giant` | Red Giant 冷转火 | confirmed/validated | ruleset_rule:rule-bh-red-giant |
| `bh-skill-tree-more-nodes` | Black Hole 技能树 more 节点分组 | confirmed/validated | ruleset_rule:rule-bh-more-aggregation |
| `bh-supermassive` | Supermassive（Boss/稀有 more） | confirmed/validated | conditional_effect:main_skill.supermassive |
| `bh-target-scenario-conditions` | 目标场景与空间条件（Boss/双星/中心 1m） | confirmed/validated | ruleset_rule:rule-target-scenario |
| `bh-target-side-settlement` | BH 目标侧结算：抗性/穿透的 body 与 Ignite 分流 | confirmed/validated | ruleset_rule:rule-penetration-dot |
| `bh-weakened-reality` | Weakened Reality（中心范围 more，仅本体） | confirmed/validated | ruleset_rule:rule-bh-weakened-reality |
| `buff-blood-rage-common-speed` | Blood Rage 状态的公共攻速和移速 | assumed/reusable | ruleset_effect:buff-blood-rage-common-speed |
| `buff-crescendoing-rage-main-hit-cycle` | Crescendoing Rage 主技能命中计数 | assumed/reusable | ruleset_effect:buff-crescendoing-rage-main-hit-cycle |
| `buff-frenzy-speed` | Frenzy 攻击速度与效果缩放 | derived/reusable | ruleset_effect:buff-frenzy-speed |
| `buff-incisive-rage` | Incisive Rage 暴击率与 Deadly Strike | assumed/reusable | ruleset_effect:buff-incisive-rage |
| `common-conditional-state-eval` | 条件状态求值（条件效果通道） | derived/validated | none:条件求值语义由注册表 effects[].conditions/control 与场景契约定义，没有单条规则… |
| `common-frequency-cooldown` | 频率/冷却/施法时间共享算子 | derived/validated | none:共享算子没有单一规则 ID：BH 频率口径见 ruleset rule-cooldown-base / ru… |
| `common-increased-aggregate` | increased/reduced 加算池 | confirmed/validated | none:加算是共享聚合算子，无单条规则 ID；口径由 ruleset 各规则的 stat_key/operation… |
| `common-ledger` | 来源账/规则账与一致性检查 | derived/validated | none:账本结构是输出契约（docs/architecture.md §7/§9、result-v1 schema）… |
| `common-monster-dr` | 怪物等级减伤（Monster DR） | confirmed/validated | ruleset_rule:rule-target-monster-dr-table |
| `common-more-groups` | more 分组与逐组独立相乘 | confirmed/validated | ruleset_rule:rule-bh-more-aggregation |
| `common-tag-match` | 伤害标签适用性匹配 | confirmed/validated | none:标签适用性是共享判据，没有单条规则 ID：具体规则的标签写在 ruleset 各节点/词条 damage_t… |
| `common-target-resistance-penetration` | 目标抗性/穿透结算（穿透可压到 0 以下） | confirmed/validated | ruleset_rule:rule-penetration-dot |
| `damage-channel-added-and-multipliers` | 技能基础元素、附加点伤与分量增伤的独立匹配 | derived/reusable | none:公共标签/组件装配规则由受限 Python handler 实现，无独立规则 JSON；实际入口见 hand… |
| `fr-effect-ambition-gain-source` | Ambition 层数获取来源（idols.2：命中 boss/rare 得层，1s 冷却） | unresolved/not_implemented | ruleset_effect:ambition_gain_source |
| `fr-effect-ambition-stacks` | Throne of Ambition 层数增伤 | derived/reusable | ruleset_effect:fr-effect-ambition-stacks |
| `fr-effect-army-of-skin-high-health` | Army of Skin：高血量敌人 +49% more Melee（条件效果） | assumed/validated | ruleset_effect:global_conditional_damage_feet |
| `fr-effect-blade-weaving-consume` | Blade Weaver / Blade Weaving：消耗层数获得 more elemental | derived/validated | ruleset_effect:blade_weaving_consume |
| `fr-effect-chaos-flare-ignited-crit` | Chaos Flare：目标 ignited 时 crit chance ×1.2（条件效果） | assumed/validated | ruleset_effect:chaos_flare_ignited_crit |
| `fr-effect-enchant-weapon` | Enchant Weapon（sb44eQ）active/passive：more melee elemental、攻速、added fi… | derived/validated | ruleset_effect:enchant_weapon_active |
| `fr-effect-firebrand-scope` | Firebrand 层数/OQ7：只作用于 Firebrand 自身（FR 不消耗），仍存 +5 melee fire 的开放问题 | confirmed/not_implemented | ruleset_effect:firebrand_incineration |
| `fr-effect-frenzy-active` | Frenzy（ailment 34）active：increased attack speed 与 Frenzied Flame 的 ad… | derived/validated | ruleset_effect:frenzy_active |
| `fr-effect-haste` | Haste 移动速度及效果缩放 | derived/reusable | ruleset_effect:fr-effect-haste |
| `fr-effect-ignite-sources` | FR 命中模型相关的 Ignite 来源（Arcane Flames / Sunwreath / Firebrand Flare） | derived/not_implemented | ruleset_effect:ignite_source_ring2 |
| `fr-effect-mana-300-idols` | 神像词条 doubled if 300+ maximum mana（当前条件不满足，doubling 未建模） | confirmed/not_implemented | ruleset_effect:mana_ge_300_idols |
| `fr-effect-rhythm-of-fire-consume` | Rhythm of Fire 消耗 12 层：+130% more damage（条件效果） | assumed/validated | ruleset_effect:rhythm_of_fire_consume |
| `fr-effect-scorching-tide-ignited` | Scorching Tide：目标 ignited 时 +60% more melee（条件效果） | assumed/validated | ruleset_effect:scorching_tide_ignited |
| `fr-effect-surge-pyroclasm` | Surge Pyroclasm：额外施放 Flame Reave（未建模） | unresolved/not_implemented | ruleset_effect:surge_pyroclasm |
| `fr-hit-added-scaling` | Flame Reave added damage 池与缩放（400% effectiveness，fire_only） | derived/validated | ruleset_rule:R4 |
| `fr-hit-attack-speed` | Flame Reave 攻速模型（技能 as × speedMultiplier 基率，increased 加算/more 分组） | confirmed/validated | ruleset_rule:R3 |
| `fr-hit-conditional-state-eval` | FR 条件状态求值（scenario states 分层覆盖与 fail-closed） | derived/validated | conditional_effect:hit-state-evaluation |
| `fr-hit-cooldown-icr` | Flame Reave Reflash 冷却与 ICR、cooldown_limited 主口径 | assumed/validated | ruleset_rule:R13 |
| `fr-hit-critical-expected` | Flame Reave 暴击率/暴伤与 avg crit 期望 | derived/validated | ruleset_rule:R25 |
| `fr-hit-dual-wield-added` | 双持武器 added damage 求和与武器 attackRate 不参与 | confirmed/validated | ruleset_rule:R2 |
| `fr-hit-events-weights` | Flame Reave 每次施放命中次数与伤害权重（4 击） | confirmed/validated | ruleset_rule:R1 |
| `fr-hit-increased-pools` | Flame Reave increased 加算池（damage / attack speed / 属性缩放） | derived/validated | ruleset_rule:R9 |
| `fr-hit-more-groups` | Flame Reave more 乘区分组与组间相乘 | derived/validated | ruleset_rule:R19 |
| `fr-hit-offhand-exalted` | Whetstone Gavel 副手 Exalted：副手全部 stats +97% effectiveness | confirmed/validated | ruleset_effect:offhand_exalted_effect |
| `fr-hit-rhythm-rotation` | Rhythm of Fire 循环平均（每 4 次施放 1 次消耗，周期平均 more） | assumed/validated | ruleset_rule:R18 |
| `fr-hit-single-hit-chain` | Flame Reave 单次命中乘区链（Fire 单组件） | derived/validated | ruleset_rule:R4 |
| `fr-hit-tag-matching` | Flame Reave 伤害标签匹配与组件标签闭包 | derived/validated | ruleset_rule:R9 |
| `fr-hit-target-side` | Flame Reave 目标侧结算（穿透 / 火抗 / 怪物 DR） | assumed/validated | ruleset_rule:R12 |
| `fr-multi-hit-return-hits` | Flame Reave 每次施放多次返回命中与 50% less 伤害权重 | derived/reusable | none:plan-v2 rules.hit_events（定义规则数据，取代旧 ruleset.hit_events） |
| `fr-node-arcane-severance` | Flame Reave 树节点 12 Arcane Severance（more melee + added spell 转换） | derived/validated | ruleset_effect:arcane_severance_melee_more |
| `fr-node-dancing-flames` | Flame Reave 树节点 2 Dancing Flames（攻速/法力/射程） | derived/validated | ruleset_rule:R3 |
| `fr-node-ember-infusion` | Flame Reave 树节点 13 Ember Infusion（more melee damage） | derived/validated | ruleset_effect:ember_infusion_melee_more |
| `fr-node-flame-caller` | Flame Reave 树节点 18 Flame Caller（返回命中 + 法力消耗） | derived/validated | ruleset_rule:R1 |
| `fr-node-frenzied-flame` | Flame Reave 树节点 6 Frenzied Flame（Frenzy 触发与加成） | derived/validated | ruleset_effect:frenzy_active |
| `fr-node-heat-wave` | Flame Reave 树节点 9 Heat Wave（more fire damage） | derived/validated | ruleset_effect:heat_wave_fire_more |
| `fr-node-precise-destruction` | Flame Reave 树节点 20 Precise Destruction（added crit chance） | derived/validated | dataset_entity:node:letools:fr11mv:20 |
| `fr-node-reflash` | Flame Reave 树节点 16 Reflash（额外命中与 3s 冷却） | confirmed/validated | ruleset_rule:R1 |
| `fr-node-rolling-flames` | Flame Reave 树节点 25 Rolling Flames（射程，不影响命中伤害） | derived/validated | dataset_entity:node:letools:fr11mv:25 |
| `fr-node-versatility` | Flame Reave 树节点 4 Versatility（其它技能叠 Rhythm 层） | derived/not_implemented | ruleset_rule:R18 |
| `fr-skill-level-scaling` | Flame Reave 技能等级（实际 27 级只影响节点数量，不缩放伤害） | confirmed/not_implemented | ruleset_rule:R16 |
| `item-offhand-exalted-effectiveness` | Whetstone Gavel：副手 Exalted 时全部副手 stats 获得 +97% effectiveness | derived/reusable | none:plan-v2 rules.item_effectiveness（迁移规则数据；旧 hit_inputs._… |
| `item-sword-catcher-frenzy-added-melee` | Sword Catcher 的 Frenzy 附加近战伤害 | derived/reusable | ruleset_effect:item-sword-catcher-frenzy-added-melee |
| `item-vaions-chariot-movement-to-damage` | Vaion's Chariot 移速转加算伤害 | derived/reusable | ruleset_effect:item-vaions-chariot-movement-to-damage |
| `minion-attribute-scaling-dexterity` | Ballista 召唤的敏捷属性缩放（玩家 Dex → minion 增伤） | derived/reusable | ruleset_rule:knowledge/definitions/ballista-armed-construct… |
| `minion-explicit-owner-stat-transfer` | 节点驱动的玩家→召唤物属性转移 | confirmed/reusable | ruleset_rule:knowledge/definitions/ballista-armed-construct… |
| `minion-level-damage-scaling` | Minion 等级伤害缩放（公式已核、应用未确认） | unresolved/not_implemented | dataset_entity:skill:letools:classInfoForSources:4 extensio… |
| `minion-owner-stat-isolation` | 召唤物与玩家属性池隔离 | confirmed/reusable | ruleset_rule:knowledge/definitions/ballista-armed-construct… |
| `mourningfrost-cold-per-dexterity` | Mourningfrost：每点敏捷 +1 冰霜伤害（攻击/法术） | confirmed/reusable | ruleset_rule:knowledge/definitions/ballista-armed-construct… |
| `phantom-grip-crit-chance-per-dexterity` | Phantom Grip：每点敏捷 +1% increased Critical Strike Chance（腐化词条） | confirmed/reusable | ruleset_rule:knowledge/definitions/ballista-armed-construct… |
| `skill-abyssal-retribution` | Abyssal Retribution 后续主技能增伤 | assumed/reusable | ruleset_effect:skill-abyssal-retribution |
| `skill-added-damage-spell-to-melee` | Arcane Severance：每 4 点附加法术伤害转为附加近战伤害 | assumed/reusable | none:plan-v2 rules.added_damage_conversion（迁移规则数据） |
| `skill-dark-duellist` | Dark Duellist 准备反击时的全局增伤 | derived/reusable | ruleset_effect:skill-dark-duellist |
| `skill-entropic-blows` | Entropic Blows 的 Time Rot 层数增伤 | derived/reusable | ruleset_effect:skill-entropic-blows |
| `skill-hellish-chasm-next-fire` | Hellish Chasm 后续火焰技能增伤 | assumed/reusable | ruleset_effect:skill-hellish-chasm-next-fire |
| `skill-nether-coating` | Nether Coating 攻速与致盲增伤 | derived/reusable | ruleset_effect:skill-nether-coating |
| `skill-void-supremacy` | Void Supremacy 对减速目标增伤 | assumed/reusable | ruleset_effect:skill-void-supremacy |
| `skill-vorpal-force` | Vorpal Force 的 Bleed 层数增伤及上限 | derived/reusable | ruleset_effect:skill-vorpal-force |
| `target-critical-vulnerability` | Critical Vulnerability 目标被暴击率 | assumed/reusable | ruleset_effect:target-critical-vulnerability |
| `urzil-pride-all-attributes-corrupted-condition` | Urzil's Pride：≥7 件 corrupted 非神像装备时 +8 All Attributes | confirmed/reusable | ruleset_rule:knowledge/definitions/ballista-armed-construct… |

### 3.6 共享定义候选

定义正文是计算权威；`preferred` 表示同一技能多版本中的当前候选。v3 与 v4 同时登记的原因是 plan 版本不同（v3 用于 plan-v1/v2/v3 执行语义，v4 用于 plan-v4），由 selector 按技能/职业/标签择一，不按版本后缀判定。

| template_id | ability | plan_version | 定义正文 | 适用说明 |
|---|---|---|---|---|
| `bh-black-hole-v3` | `bh2` | le-calculation-plan-v3 | [`knowledge/definitions/black-hole-v3.json`](../knowledge/definitions/black-hole-v3.json) | Black Hole 纯 DoT/独立 Ignite/充能调度；要求当前实例确为 Mage 且专精 bh2。 |
| `fr-flame-reave-v3` | `fr11mv` | le-calculation-plan-v2 | [`knowledge/definitions/fr-flame-reave-v3.json`](../knowledge/definitions/fr-flame-reave-v3.json) | Flame Reave 火焰近战多命中；其它技能变体不自动套用。 |
| `void-cleave-fire-main-v3` | `v01cv` | le-calculation-plan-v1 | [`knowledge/definitions/void-cleave-fire-main-v3.json`](../knowledge/definitions/void-cleave-fire-main-v3.json) | Void Cleave 转火由真实专精节点 Riftflame/Hellish Chasm 前提驱动；不按 Fire 标签写死。 |
| `void-cleave-fire-main-v4` | `v01cv` | le-calculation-plan-v4（候选默认） | [`knowledge/definitions/void-cleave-fire-main-v4.json`](../knowledge/definitions/void-cleave-fire-main-v4.json) | Void Cleave 转火由真实专精节点 Riftflame/Hellish Chasm 前提驱动；不按 Fire 标签写死。 |
| `fr-flame-reave-v4` | `fr11mv` | le-calculation-plan-v4（候选默认） | [`knowledge/definitions/fr-flame-reave-v4.json`](../knowledge/definitions/fr-flame-reave-v4.json) | Flame Reave 火焰近战多命中；其它技能变体不自动套用。 |
| `black-hole-v4` | `bh2` | le-calculation-plan-v4（候选默认） | [`knowledge/definitions/black-hole-v4.json`](../knowledge/definitions/black-hole-v4.json) | Black Hole 纯 DoT/独立 Ignite/充能调度；要求当前实例确为 Mage 且专精 bh2。 |
| `ballista-armed-construction-v4` | `ba1574` | le-calculation-plan-v4 | [`knowledge/definitions/ballista-armed-construction-v4.json`](../knowledge/definitions/ballista-armed-construction-v4.json) | Ballista 主技能专精；DPS 口径只含 Armed Construction 到期/销毁爆炸。其它 Minion/召唤技能不自动套… |

## 4. 未支持与未解决（必须保持可见）

完整逐 scope 清单见 [`docs/evidence/extracted/coverage-boundaries.json`](evidence/extracted/coverage-boundaries.json)
（由 skill 模板 `unsupported`、共享定义 `limitations/coverage` 与条件注册表 `support.level` 生成）。概要：

- **其他技能的主动/被动树自身伤害**：不进主 DPS；只作为条件 Buff 候选被发现/配置/部分重算。
- **编织者天赋树、队伍/外部 Buff、法力消耗与恢复、非伤害状态（Chill/Blind 等）**：未建模。
- **条件效果**：只做固定条件估算，不模拟叠层/资源/操作序列；`needs_action_or_time_simulation` 与
  `mechanism_unresolved` 的条目不进入基线。
- **hit 模型**：技能等级缩放、双持合成口径、暴击缩放被动等残留未验证项，按规则包 `assumed/unresolved` 处理。
- **Ballista/minion**：只覆盖已声明的 actor 与爆炸口径；陷阱引爆率等是场景假设，未做游戏内实测。
- 机制未知可以显式假设或缩小范围继续，但**已知来源、执行或账本错误不能用 partial 掩盖**，也不能据此更新可用 canonical。

## 5. 如何登记一条新的未知机制（规程）

1. **先定位来源**：从当前构筑的计算输入 / 快照 / catalog 找到候选（`tools/phase3/src/le_calc/conditional/discover.py` 的检查分母）。
2. **选证据类别**：游戏数据 → 反编译 → 游戏内实测 → 用户裁决 → 实现假设，按 §1 标注，不得跳级。
3. **写进注册表**（条件类）或 **规则包**（计算口径类），带 `status` 与 `evidence[].locator`；
   未验证的写法只有两种：规则包 `assumed`/`unresolved`，或注册表 `mechanism_unresolved` + `research.open_questions`。
4. **同步本索引**：在 §3 增加/更新一行，写清“证据类别 / 状态 / 入口 / 适用范围”。
5. **登记到知识库**：以 `knowledge/schemas/mechanism-v1.json` 写入 `knowledge/mechanisms/<mechanism_id>.json`，
   再执行 `le.ps1 knowledge rebuild`；条目须带 `rule_ref`、`handler_ref`、`evidence_refs` 与
   `evidence_state`/`implementation_state`。只登记文字、不改变当前计算默认值。
6. **证据落盘**：逆向证据放 `docs/evidence/reverse-engineering/`；实测/日志结论写进 `docs/evidence/` 或 `reports/<case>/<run-id>/`
   （`reports/` 是用户本地输出目录，不随仓库分发）；不要只留在临时目录或聊天记录里。
7. **作者本地归档**：不随公开仓库分发的材料（历史案例输入、历史 run、历史评审文档）在条目/规则/文档中
   统一用 `archive://<原路径>` 引用，约定见 [`docs/evidence/extracted/README.zh-CN.md`](evidence/extracted/README.zh-CN.md)。
8. **升级状态**需同时给出：新证据文件路径、受影响的规则 ID、原状态与新状态、未覆盖项；
   不要让“实验假设”直接变成 `confirmed`。

## 6. 证据导航

- 逆向证据总目录与轮次结论：[`docs/evidence/reverse-engineering/README.zh-CN.md`](evidence/reverse-engineering/README.zh-CN.md)
- 工具链与版本/哈希：[`toolchain-and-version-1.5.3.json`](evidence/reverse-engineering/toolchain-and-version-1.5.3.json)
- 复现命令与 RVA/offset 索引：[`reproduction-1.5.3.zh-CN.md`](evidence/reverse-engineering/reproduction-1.5.3.zh-CN.md)
- 提取证据（可复用结论 + 作者归档定位）：[`docs/evidence/extracted/`](evidence/extracted/README.zh-CN.md)
- 规则/定义版本审计：[`version-audit.json`](evidence/extracted/version-audit.json)
- 覆盖边界：[`coverage-boundaries.json`](evidence/extracted/coverage-boundaries.json)
- 知识库入口：[`knowledge/README.md`](../knowledge/README.md)；查询命令见 §3
- 支持范围与限制：[`current-status.md`](current-status.md)

## 7. 2026-11-13 知识同步（v0.4.1）

本版同步作者仓库 2026-11-13 的知识/证据修复（独立复核后的修复轮与补遗）。修复/评审记录属作者本地归档
（`archive://docs/reviews/…`），不随公开仓库分发；结构化证据随仓库提供于 `docs/evidence/ballista/`。
逐条结论、来源定位与限制见 [`docs/evidence/extracted/`](evidence/extracted/README.zh-CN.md)。

### 7.1 新增机制（4）

| mechanism_id | 标题 | evidence/implementation | 入口 |
|---|---|---|---|
| `mourningfrost-cold-per-dexterity` | Mourningfrost：每点敏捷 +1 冰霜伤害（攻击/法术） | confirmed/reusable | 共享定义模板（见 §7.3）；`le_calc.plan_compiler._apply_per_attribute_scaling` |
| `phantom-grip-crit-chance-per-dexterity` | Phantom Grip：每点敏捷 +1% increased Critical Strike Chance（腐化词条） | confirmed/reusable | 共享定义模板（见 §7.3）；`le_calc.plan_compiler._apply_per_attribute_scaling` |
| `urzil-pride-all-attributes-corrupted-condition` | Urzil's Pride：≥7 件 corrupted 非神像装备时 +8 All Attributes | confirmed/reusable | 共享定义模板（见 §7.3）；`le_calc.effect_binding.count_equipment_condition_items` |
| `damage-channel-added-and-multipliers` | 技能基础元素、附加点伤与分量增伤的独立匹配 | derived/reusable | `le_calc.mechanics.tag_match.damage_channel_tags` |

### 7.2 更新机制（3）

| mechanism_id | revision | evidence/implementation | 主要更新 |
|---|---|---|---|
| `ballista-trap-triggered-summon` | rev3 | derived/reusable | 点数只从当前输入专精绑定，场景 points 控件移除；points=0/专精缺失时编译拒绝；陷阱引爆率控件 min=1。 |
| `ballista-explosion-damage-composition` | rev4 | derived/reusable | actor 命中按伤害通道隔离（Fire added 不再串入其它分量）；暴击改为执行期逐行结算，crit_absorbed 废弃。 |
| `minion-explicit-owner-stat-transfer` | rev3 | confirmed/reusable | 属性转移按当前输入 Contribution 逐行复制（added/increased/more 按比例、保留 tags、召唤时快照）；同 tag 多条分别进入匹配。 |

### 7.3 共享定义

`knowledge/definitions/ballista-armed-construction-v4.json` 新增装备条件与 per-attribute 规则段：

- 新增 per-attribute effects：`effects[4]=mourningfrost_cold_per_dexterity`、`effects[5]=phantom_grip_crit_chance_per_dexterity`（Mourningfrost / Phantom Grip）。
- 原有 effects 保持不变：effects[0]=heavy_bolts_more、effects[1]=practical_build_more、effects[2]=elixir_more、effects[3]=sharpened_tips_crit。
- 新增装备条件规则段：`equipment_condition_effects[0]=urzil_all_attributes_with_7_corrupted_items`（Urzil's Pride）。
- `definitions/index.json` 的 `sha256`/`revision` 已按同步后的实际定义文件重算（7 个候选一致）。
- 装备页完整词条/`unique_description` 投影属实现层修复（非游戏机制）；自动完整性门禁尚未实现，
  不宣称报告契约 §6.9（UR-15）自动通过。

### 7.4 共享伤害分量匹配

`damage-channel-added-and-multipliers`（derived/reusable，入口 `le_calc.mechanics.tag_match.damage_channel_tags`）：基础类型不构成点伤白名单；行为/主体/条件仍需匹配。其它类型点伤生成对应分量，typed 修正只匹配最终分量，Elemental 仅火冰电。转换或排除需另有显式规则。

覆盖范围限制：未宣称任意技能的特殊转换、伤害排除与事件生命周期均已支持；特殊规则仍需单独绑定与验证。

### 7.5 证据文件

`docs/evidence/ballista/` 新增：`ballista-fix-verification-20261113.json`、`ballista-mourningfrost-20261113.json`、`ballista-remaining-equipment-audit-20261113.json`（含 `.zh-CN.md`）、`ballista-remaining-equipment-fixes-20261113.json`、`damage-channel-contract-verification-20261113.json`。
提取证据目录同步新增 7 个机制条目、更新 1 个共享定义条目（`_index.json` 共 113 条）。
