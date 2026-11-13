<!-- GENERATED: 本文件由脚本生成，请勿手工修改；权威源是 JSON，重建命令见下表。 -->

# knowledge 索引（生成物）

> **GENERATED** — 本文件由 `le.ps1 knowledge rebuild` 从 `knowledge/mechanisms/*.json`、`knowledge/skills/*.json` 生成，禁止手改；手工修改会在下次重建时被覆盖并告警。查询入口：`knowledge search` / `knowledge show`（详见 `knowledge/README.md`）。

| 项 | 值 |
|---|---|
| index_format | `le-knowledge-index-v1` |
| source_digest | `sha256:07ac7a3faa76dbf9bafdbea2421967b629d74fda8434cf5598578d21d7e7202f` |
| 生成命令 | `.\scripts\le.ps1 knowledge rebuild` |
| 内容文件 | 130（mechanisms 127 / skills 3）+ schema 4 |
| 条目 | mechanisms 127 / skill_templates 3 |
| 索引明细 | selectors 403 / aliases 592 |

## 机制条目（knowledge/mechanisms）

| ID | 标题 | rev | evidence_state | implementation_state | 适用组件 | 命中来源（选择器） | 规则引用 | 来源文件 |
|---|---|---|---|---|---|---|---|---|
| attribute-strength-to-brutality | 力量转换为 Brutality 与按法耗独立近战增伤 | 1 | derived | reusable | hit | stat:letools:coreDB.playerPropertyList:650, stat:650, item:affix:letools:affixList.multiAffixes:1083, item:1083 | ruleset_rule:attribute-conversions-v1:brutality | knowledge/mechanisms/attribute-strength-to-brutality.json |
| ballista-death-explosion | Ballista 到期/销毁爆炸与敏捷倍率 | 2 | confirmed | reusable | death_event, explosion | ability:ba1574, node:ba1574:5 | ruleset_rule:knowledge/definitions/ballista-armed-construction-v4.json#definition.frequency | knowledge/mechanisms/ballista-death-explosion.json |
| ballista-dexterity-frequency-and-increased | Ballista 敏捷伤害与放置/攻击速度 | 2 | derived | reusable | attribute_scaling, frequency | ability:ba1574, node:ba1574:18 | ruleset_rule:knowledge/definitions/ballista-armed-construction-v4.json#definition.rules.node_attribute_scaled | knowledge/mechanisms/ballista-dexterity-frequency-and-increased.json |
| ballista-explosion-damage-composition | Ballista 爆炸伤害构成（ability_gen327） | 4 | derived | reusable | hit, frequency | ability:ba1574, node:ba1574:5, node:ba1574:14, node:ba1574:23, node:ba1574:25, node:ba1574:26 | ruleset_rule:knowledge/definitions/ballista-armed-construction-v4.json#definition.skill | knowledge/mechanisms/ballista-explosion-damage-composition.json |
| ballista-potion-construction-window | 药剂条件与 Ballista 召唤时状态 | 2 | derived | reusable | condition, snapshot | ability:ba1574, node:ba1574:25, node:ba1574:31 | ruleset_rule:knowledge/definitions/ballista-armed-construction-v4.json#definition.effects#elixir_more | knowledge/mechanisms/ballista-potion-construction-window.json |
| ballista-projectile-explosion | Ballista 命中爆炸与火伤/点燃 | 2 | confirmed | draft | minion_hit, explosion, ailment | ability:ba1574, node:ba1574:10, node:ba1574:12 | ruleset_rule:本轮 DPS 口径不含 bolt 命中爆炸（用户指定只算 Armed Construction 的 Explodes） | knowledge/mechanisms/ballista-projectile-explosion.json |
| ballista-summon-limit-lifetime | Ballista 召唤上限、存续与替换事件 | 2 | confirmed | reusable | summon, lifecycle | ability:ba1574, node:ba1574:2, node:ba1574:14, node:ba1574:15 | ruleset_rule:knowledge/definitions/ballista-armed-construction-v4.json#definition.frequency#derivation.lifecycle | knowledge/mechanisms/ballista-summon-limit-lifetime.json |
| ballista-trap-triggered-summon | Explosive Trap 触发 Ballista 的来源链 | 3 | derived | reusable | trigger, summon, frequency | ability:ba1574, node:ex4tp:29, node:ex4tp:30, node:ex4tp:31 | ruleset_rule:knowledge/definitions/ballista-armed-construction-v4.json#definition.frequency#derivation | knowledge/mechanisms/ballista-trap-triggered-summon.json |
| bh-attribute-scaling | Intelligence 属性缩放（本体） | 2 | confirmed | validated | body | stat:damage, node:bh2:0 | ruleset_rule:rule-bh-attribute-scaling | knowledge/mechanisms/bh-attribute-scaling.json |
| bh-baseline-profiles | 双基线口径：legacy_single_skill 与 build_base | 1 | derived | validated | loop, ledger, conditional | mechanism:bh-baseline-profiles | none:基线 profile 是案例/场景口径（conditional registry baseline_profiles + conditional-scenario），没有游戏规则 ID。 | knowledge/mechanisms/bh-baseline-profiles.json |
| bh-binary-system | Binary System 双星结构 + 独立 more | 2 | confirmed | validated | body, body_fire, body_cold, ignite | node:bh2:9 | ruleset_rule:rule-bh-binary-system | knowledge/mechanisms/bh-binary-system.json |
| bh-black-hole-instances | 黑洞实例：每次施放 1 个、多次施放并存 | 2 | confirmed | validated | frequency, loop, body, ignite | node:bh2:28 | ruleset_rule:rule-black-hole-count-limit | knowledge/mechanisms/bh-black-hole-instances.json |
| bh-body-component | Black Hole 本体组件与单跳伤害 | 2 | confirmed | validated | body, body_fire, body_cold, tick | ability:bh2, node:bh2:0 | ruleset_rule:rule-bh-component | knowledge/mechanisms/bh-body-component.json |
| bh-body-tick-model | 本体跳数离散化与总伤害口径 | 2 | confirmed | validated | body, tick, frequency | ability:bh2 | ruleset_rule:rule-bh-tick-schedule | knowledge/mechanisms/bh-body-tick-model.json |
| bh-cast-time | 施法时间（释放速度） | 2 | confirmed | validated | cast, frequency, loop | stat:cast_speed, ability:bh2 | ruleset_rule:rule-cast-time | knowledge/mechanisms/bh-cast-time.json |
| bh-charge-schedule-multi-instance | Black Hole 充能调度（charges/recharge/CDR/ICR/flat）与多实例并发 | 2 | derived | reusable | frequency | ability:bh2, node:bh2:17, node:bh2:25, node:bh2:30 | ruleset_rule:plan-v3 schedule.charges/action_policies | knowledge/mechanisms/bh-charge-schedule-multi-instance.json |
| bh-cond-apex-glyph-conversion | Apex of Thought · Glyph of Dominion 异常几率转换 | 2 | unresolved | draft | ignite_application, penetration, target_side | item:letools:uniqueList.uniques:316 | conditional_effect:equipment.apex_glyph_conversion | knowledge/mechanisms/bh-cond-apex-glyph-conversion.json |
| bh-cond-chilled-touch | Chilled Touch（Frost Wall + 目标冰缓：+more 伤害） | 3 | assumed | validated | body_fire, body_cold, ignite | node:fr4wl:16 | conditional_effect:frost_wall.chilled_touch | knowledge/mechanisms/bh-cond-chilled-touch.json |
| bh-cond-conflagrate | Conflagrate（Enchant Weapon 激活：消费点燃并立即结算） | 2 | assumed | not_implemented | ignite, ignite_application | node:sb44eQ:10 | conditional_effect:enchant_weapon.conflagrate | knowledge/mechanisms/bh-cond-conflagrate.json |
| bh-cond-craterborn | Craterborn（Meteor 后：火穿 + 施法速度） | 3 | confirmed | validated | body_fire, ignite, cast, cooldown +1 | node:me27:25 | conditional_effect:meteor.craterborn | knowledge/mechanisms/bh-cond-craterborn.json |
| bh-cond-crescendoing-rage | Crescendoing Rage（每第三次施放 +more） | 1 | assumed | draft | body, body_fire, body_cold, ignite +1 | item:equipment:ring2, item:letools:uniqueList.uniques:477, condition:player.crescendoing_rage_third_cast | conditional_effect:ring.crescendoing_rage | knowledge/mechanisms/bh-cond-crescendoing-rage.json |
| bh-cond-eternal-flames-active | Eternal Flames 激活（节点效果翻倍） | 2 | confirmed | validated | ignite, duration | node:sb44eQ:9 | conditional_effect:enchant_weapon.eternal_flames.active | knowledge/mechanisms/bh-cond-eternal-flames-active.json |
| bh-cond-eternal-flames-passive | Eternal Flames 被动（+Ignite 持续时间） | 2 | confirmed | validated | ignite, duration | node:sb44eQ:9 | conditional_effect:enchant_weapon.eternal_flames.passive | knowledge/mechanisms/bh-cond-eternal-flames-passive.json |
| bh-cond-flame-ward-fire-aura | Flame Ward · Fire Aura（自身伤害，不进入 BH 计算） | 2 | confirmed | not_implemented | target_side | node:fw3d:12 | conditional_effect:flame_ward.fire_aura | knowledge/mechanisms/bh-cond-flame-ward-fire-aura.json |
| bh-cond-frozen-steel | Frozen Steel（目标冰缓/冻结时的条件增伤，4 点门槛） | 3 | unresolved | not_implemented | body_fire, body_cold | node:mg-1:36 | conditional_effect:passive.frozen_steel | knowledge/mechanisms/bh-cond-frozen-steel.json |
| bh-cond-idol-altar-layout-cdr | Pyramidal Altar（布局条件 ICR） | 1 | unresolved | draft | frequency, cooldown, conditional | item:equipment:idol_altar, condition:player.idol_layout_condition_met | conditional_effect:idol_altar.layout_cdr | knowledge/mechanisms/bh-cond-idol-altar-layout-cdr.json |
| bh-cond-idol-fire-doubling | Grand Arcane Omen Idol · 最大法力门槛火伤翻倍（idols.2 + idols.3，合并登记） | 1 | confirmed | validated | body_fire, ignite | idol:equipment:idols.2, idol:equipment:idols.3, condition:auto.max_mana_gte_300 | conditional_effect:idol.fire_damage_mana_threshold | knowledge/mechanisms/bh-cond-idol-fire-doubling.json |
| bh-cond-infusion | Infusion（Flame Ward 生效期间 +increased 火伤） | 3 | confirmed | validated | body_fire, ignite | node:fw3d:2 | conditional_effect:flame_ward.infusion | knowledge/mechanisms/bh-cond-infusion.json |
| bh-cond-preparation | Preparation（下一次元素法术 +increased 伤害） | 3 | assumed | draft | body_fire, body_cold | node:mg-1:4 | conditional_effect:passive.preparation | knowledge/mechanisms/bh-cond-preparation.json |
| bh-cond-throne-of-ambition | Throne of Ambition（Boss/稀有命中叠层：每层 +more 火/冰） | 2 | confirmed | validated | body_fire, body_cold, ignite | item:letools:uniqueList.uniques:211 | conditional_effect:idol.throne_of_ambition | knowledge/mechanisms/bh-cond-throne-of-ambition.json |
| bh-cooldown-cdr-icr | 冷却公式：CDR 乘算缩减 + ICR 独立分母 | 2 | confirmed | validated | frequency, cooldown, loop | node:bh2:17, stat:cooldown_reduction, stat:cooldown_recovery_speed | ruleset_rule:rule-cooldown-icr | knowledge/mechanisms/bh-cooldown-cdr-icr.json |
| bh-cooldown-charges | 充能基础值、固定秒数与额外充能 | 2 | confirmed | validated | frequency, cooldown, loop | node:bh2:25, node:bh2:30, stat:charges, stat:cooldown_flat | ruleset_rule:rule-cooldown-base | knowledge/mechanisms/bh-cooldown-charges.json |
| bh-dot-tick-model | Black Hole 本体纯 DoT 逐跳模型（ceil_full_ticks） | 2 | derived | reusable | dot, tick | ability:bh2, node:bh2:4, node:bh2:9 | ruleset_rule:plan-v3 components[].duration_s/tick_interval_s/remainder_policy | knowledge/mechanisms/bh-dot-tick-model.json |
| bh-duration | 本体持续时间加算 | 2 | confirmed | validated | body, frequency, duration | node:bh2:4, node:bh2:15, node:bh2:24, node:bh2:25 | ruleset_rule:rule-bh-duration | knowledge/mechanisms/bh-duration.json |
| bh-heat-wave-ignite-clock | Black Hole Ignite：Heat Wave 独立施加时钟与期望层数 | 2 | derived | reusable | dot, ailment | ability:bh2, node:bh2:6, stat:ailment_duration, stat:ignite | ruleset_rule:plan-v3 schedule.ignite | knowledge/mechanisms/bh-heat-wave-ignite-clock.json |
| bh-ignite-attribute-scaling | Intelligence 属性缩放传递给 Ignite | 1 | confirmed | validated | ignite | stat:damage | ruleset_rule:rule-ignite-attribute-scaling | knowledge/mechanisms/bh-ignite-attribute-scaling.json |
| bh-ignite-core | Ignite 基础伤害与持续时间乘区 | 2 | confirmed | validated | ignite, dot, duration | stat:ailment_duration, ability:bh2 | ruleset_rule:rule-ignite-base | knowledge/mechanisms/bh-ignite-core.json |
| bh-ignite-damage-flow | Ignite 连续流记账与尾部伤害 | 2 | assumed | validated | ignite, ignite_tick, ledger | ability:bh2 | ruleset_rule:rule-ignite-damage-slices | knowledge/mechanisms/bh-ignite-damage-flow.json |
| bh-ignite-duration-sources | Ignite 持续时间乘区来源池 | 1 | confirmed | validated | ignite, duration | stat:ailment_duration | ruleset_rule:rule-ignite-duration | knowledge/mechanisms/bh-ignite-duration-sources.json |
| bh-ignite-heat-wave | Heat Wave：周期判定与期望层数 | 2 | confirmed | validated | ignite, ignite_application, frequency | node:bh2:6, ability:bh2 | ruleset_rule:rule-ignite-heat-wave | knowledge/mechanisms/bh-ignite-heat-wave.json |
| bh-ignite-inheritance | Ignite 继承 Black Hole 技能树 more | 2 | confirmed | validated | ignite | node:bh2:2, node:bh2:7, node:bh2:9, node:bh2:10, node:bh2:23, node:bh2:27 +1 | ruleset_rule:rule-ignite-inheritance | knowledge/mechanisms/bh-ignite-inheritance.json |
| bh-ignite-penetration | Ignite 专属火穿与 DoT 穿透 | 2 | confirmed | validated | ignite, target_side | stat:penetration, stat:penetration_with_ignite, ability:bh2 | ruleset_rule:rule-fire-penetration-with-ignite | knowledge/mechanisms/bh-ignite-penetration.json |
| bh-ignite-scaling-pool | Ignite 玩家侧 inc 池与标签口径 | 2 | assumed | validated | ignite | stat:damage, ability:bh2 | ruleset_rule:rule-ignite-scaling-pool | knowledge/mechanisms/bh-ignite-scaling-pool.json |
| bh-loop-action-policies | eager/patient 循环动作策略与窗口 | 2 | derived | validated | loop, frequency, ledger | ability:bh2 | none:eager/patient 是 DPS 计算器的动作策略（ruleset.action_policies），没有对应游戏规则 ID；并存的合法性见 bh-black-hole-instances。 | knowledge/mechanisms/bh-loop-action-policies.json |
| bh-monster-dr-integration | 怪物 DR 接入 BH 目标侧（独立乘区） | 1 | assumed | validated | target_side, body, ignite | condition:target.monster_level | ruleset_rule:rule-target-monster-dr-application | knowledge/mechanisms/bh-monster-dr-integration.json |
| bh-node-more-10 | Black Hole 节点 10：当前点数 more 映射 | 1 | confirmed | validated | body, body_fire, ignite | node:bh2:10 | ruleset_rule:rule-bh-more-aggregation | knowledge/mechanisms/bh-node-more-10.json |
| bh-node-more-2 | Black Hole 节点 2：当前点数 more 映射 | 1 | confirmed | validated | body, body_fire, ignite | node:bh2:2 | ruleset_rule:rule-bh-more-aggregation | knowledge/mechanisms/bh-node-more-2.json |
| bh-node-more-28 | Black Hole 节点 28：当前点数 more 映射 | 1 | confirmed | validated | body, body_fire, ignite | node:bh2:28 | ruleset_rule:rule-bh-more-aggregation | knowledge/mechanisms/bh-node-more-28.json |
| bh-node-more-7 | Black Hole 节点 7：当前点数 more 映射 | 1 | confirmed | validated | body, body_fire, ignite | node:bh2:7 | ruleset_rule:rule-bh-more-aggregation | knowledge/mechanisms/bh-node-more-7.json |
| bh-red-giant | Red Giant 冷转火 | 2 | confirmed | validated | body, body_fire, body_cold | node:bh2:4 | ruleset_rule:rule-bh-red-giant | knowledge/mechanisms/bh-red-giant.json |
| bh-skill-tree-more-nodes | Black Hole 技能树 more 节点分组 | 2 | confirmed | validated | body, body_fire, ignite | node:bh2:2, node:bh2:7, node:bh2:9, node:bh2:10, node:bh2:23, node:bh2:27 +1 | ruleset_rule:rule-bh-more-aggregation | knowledge/mechanisms/bh-skill-tree-more-nodes.json |
| bh-supermassive | Supermassive（Boss/稀有 more） | 2 | confirmed | validated | body, body_fire, body_cold, ignite | node:bh2:23, condition:target.boss_or_rare | conditional_effect:main_skill.supermassive | knowledge/mechanisms/bh-supermassive.json |
| bh-target-scenario-conditions | 目标场景与空间条件（Boss/双星/中心 1m） | 2 | confirmed | validated | target_side, body, ignite, loop | condition:target.boss_or_rare, condition:target.both_stars_cover, condition:target.within_center_1m, ability:bh2 | ruleset_rule:rule-target-scenario | knowledge/mechanisms/bh-target-scenario-conditions.json |
| bh-target-side-settlement | BH 目标侧结算：抗性/穿透的 body 与 Ignite 分流 | 2 | confirmed | validated | target_side, body, body_fire, body_cold +1 | stat:penetration, stat:penetration_with_ignite, condition:target.resistances | ruleset_rule:rule-penetration-dot | knowledge/mechanisms/bh-target-side-settlement.json |
| bh-weakened-reality | Weakened Reality（中心范围 more，仅本体） | 2 | confirmed | validated | body, body_fire, body_cold | node:bh2:27, condition:target.within_center_1m | ruleset_rule:rule-bh-weakened-reality | knowledge/mechanisms/bh-weakened-reality.json |
| buff-blood-rage-common-speed | Blood Rage 状态的公共攻速和移速 | 1 | assumed | reusable | frequency, derived | item:affix:affix:letools:affixList.singleAffixes:1117, item:affix:affix:letools:affixList.singleAffixes:1124 | ruleset_effect:buff-blood-rage-common-speed | knowledge/mechanisms/buff-blood-rage-common-speed.json |
| buff-crescendoing-rage-main-hit-cycle | Crescendoing Rage 主技能命中计数 | 1 | assumed | reusable | hit, frequency | item:affix:affix:letools:affixList.singleAffixes:1124 | ruleset_effect:buff-crescendoing-rage-main-hit-cycle | knowledge/mechanisms/buff-crescendoing-rage-main-hit-cycle.json |
| buff-frenzy-speed | Frenzy 攻击速度与效果缩放 | 1 | derived | reusable | frequency | item:letools:uniqueList.uniques:446, item:letools:uniqueList.uniques:338, node:st31io:28, mechanism:letools:LEAbilities.ailmentList:34, mechanism:34 | ruleset_effect:buff-frenzy-speed | knowledge/mechanisms/buff-frenzy-speed.json |
| buff-incisive-rage | Incisive Rage 暴击率与 Deadly Strike | 1 | assumed | reusable | hit | item:affix:affix:letools:affixList.singleAffixes:1117 | ruleset_effect:buff-incisive-rage | knowledge/mechanisms/buff-incisive-rage.json |
| common-conditional-state-eval | 条件状态求值（条件效果通道） | 2 | derived | validated | conditional, condition_state | mechanism:common-conditional-state-eval, mechanism:bh-baseline-profiles | none:条件求值语义由注册表 effects[].conditions/control 与场景契约定义，没有单条规则 ID；注册表 tools/phase3/rules/conditional-effects-bh-stage1-v1.json 是权威源。 | knowledge/mechanisms/common-conditional-state-eval.json |
| common-frequency-cooldown | 频率/冷却/施法时间共享算子 | 2 | derived | validated | frequency, cooldown, cast, loop | mechanism:common-frequency-cooldown, stat:cooldown_recovery_speed, stat:cooldown_reduction, stat:cooldown_flat, stat:charges, stat:cast_speed | none:共享算子没有单一规则 ID：BH 频率口径见 ruleset rule-cooldown-base / rule-cooldown-icr / rule-cast-time；FR 频率口径见 ruleset-fr-hit-stage1-v2 与 hit-simulate 政策。 | knowledge/mechanisms/common-frequency-cooldown.json |
| common-increased-aggregate | increased/reduced 加算池 | 2 | confirmed | validated | hit, dot, tick, ignite +1 | mechanism:common-increased-aggregate | none:加算是共享聚合算子，无单条规则 ID；口径由 ruleset 各规则的 stat_key/operation/标签字段驱动。 | knowledge/mechanisms/common-increased-aggregate.json |
| common-ledger | 来源账/规则账与一致性检查 | 2 | derived | validated | ledger | mechanism:common-ledger | none:账本结构是输出契约（docs/architecture.md §7/§9、result-v1 schema），不是游戏机制规则。 | knowledge/mechanisms/common-ledger.json |
| common-monster-dr | 怪物等级减伤（Monster DR） | 2 | confirmed | validated | target_side | mechanism:common-monster-dr, stat:monster_damage_reduction, condition:target.monster_level | ruleset_rule:rule-target-monster-dr-table | knowledge/mechanisms/common-monster-dr.json |
| common-more-groups | more 分组与逐组独立相乘 | 2 | confirmed | validated | hit, dot, tick, ignite +1 | mechanism:common-more-groups | ruleset_rule:rule-bh-more-aggregation | knowledge/mechanisms/common-more-groups.json |
| common-tag-match | 伤害标签适用性匹配 | 2 | confirmed | validated | hit, dot, tick, ignite +2 | mechanism:common-tag-match, mechanism:bh-ignite-scaling-pool, stat:damage, stat:added_damage, stat:penetration, stat:ailment_duration | none:标签适用性是共享判据，没有单条规则 ID：具体规则的标签写在 ruleset 各节点/词条 damage_tags 与 dataset stats 的 tags 位图中。 | knowledge/mechanisms/common-tag-match.json |
| common-target-resistance-penetration | 目标抗性/穿透结算（穿透可压到 0 以下） | 2 | confirmed | validated | target_side, body, ignite, hit +1 | mechanism:common-target-resistance-penetration, stat:penetration, stat:penetration_with_ignite, stat:resistance, condition:target.resistances | ruleset_rule:rule-penetration-dot | knowledge/mechanisms/common-target-resistance-penetration.json |
| damage-channel-added-and-multipliers | 技能基础元素、附加点伤与分量增伤的独立匹配 | 1 | derived | reusable | hit | stat:added_damage, stat:damage, stat:crit_chance, stat:crit_multiplier, stat:penetration | none:公共标签/组件装配规则由受限 Python handler 实现，无独立规则 JSON；实际入口见 handler_ref | knowledge/mechanisms/damage-channel-added-and-multipliers.json |
| fr-effect-ambition-gain-source | Ambition 层数获取来源（idols.2：命中 boss/rare 得层，1s 冷却） | 2 | unresolved | not_implemented | hit | idol:equipment:idols.2, condition:boss_or_rare | ruleset_effect:ambition_gain_source | knowledge/mechanisms/fr-effect-ambition-gain-source.json |
| fr-effect-ambition-stacks | Throne of Ambition 层数增伤 | 3 | derived | reusable | hit, dot | item:letools:uniqueList.uniques:211, mechanism:letools:UIBuffs:79, mechanism:79 | ruleset_effect:fr-effect-ambition-stacks | knowledge/mechanisms/fr-effect-ambition-stacks.json |
| fr-effect-army-of-skin-high-health | Army of Skin：高血量敌人 +49% more Melee（条件效果） | 2 | assumed | validated | hit | item:letools:uniqueList.uniques:443 | ruleset_effect:global_conditional_damage_feet | knowledge/mechanisms/fr-effect-army-of-skin-high-health.json |
| fr-effect-blade-weaving-consume | Blade Weaver / Blade Weaving：消耗层数获得 more elemental | 2 | derived | validated | hit | node:mg-1:50, mechanism:letools:UIBuffs:29, stat:damage, condition:consume_blade_weaving | ruleset_effect:blade_weaving_consume | knowledge/mechanisms/fr-effect-blade-weaving-consume.json |
| fr-effect-chaos-flare-ignited-crit | Chaos Flare：目标 ignited 时 crit chance ×1.2（条件效果） | 2 | assumed | validated | hit | node:fr11mv:24, condition:ignited | ruleset_effect:chaos_flare_ignited_crit | knowledge/mechanisms/fr-effect-chaos-flare-ignited-crit.json |
| fr-effect-enchant-weapon | Enchant Weapon（sb44eQ）active/passive：more melee elemental、攻速、added fire 与 increased | 2 | derived | validated | hit, frequency | ability:sb44eQ, node:sb44eQ:1, node:sb44eQ:7, node:sb44eQ:14, node:sb44eQ:16, node:sb44eQ:17 +7 | ruleset_effect:enchant_weapon_active | knowledge/mechanisms/fr-effect-enchant-weapon.json |
| fr-effect-firebrand-scope | Firebrand 层数/OQ7：只作用于 Firebrand 自身（FR 不消耗），仍存 +5 melee fire 的开放问题 | 2 | confirmed | not_implemented | hit | ability:f1b4d, node:f1b4d:7, node:f1b4d:8, node:f1b4d:9, node:f1b4d:18, node:f1b4d:23 +2 | ruleset_effect:firebrand_incineration | knowledge/mechanisms/fr-effect-firebrand-scope.json |
| fr-effect-frenzy-active | Frenzy（ailment 34）active：increased attack speed 与 Frenzied Flame 的 added fire | 3 | derived | validated | hit, frequency | mechanism:letools:ailmentList:34, node:fr11mv:6, stat:attack_speed, stat:added_damage, condition:frenzy | ruleset_effect:frenzy_active | knowledge/mechanisms/fr-effect-frenzy-active.json |
| fr-effect-haste | Haste 移动速度及效果缩放 | 3 | derived | reusable | derived | item:letools:uniqueList.uniques:413, mechanism:letools:LEAbilities.ailmentList:33, mechanism:33 | ruleset_effect:fr-effect-haste | knowledge/mechanisms/fr-effect-haste.json |
| fr-effect-ignite-sources | FR 命中模型相关的 Ignite 来源（Arcane Flames / Sunwreath / Firebrand Flare） | 2 | derived | not_implemented | hit | item:letools:uniqueList.uniques:150, node:mg-1:7, node:f1b4d:23, condition:ignited | ruleset_effect:ignite_source_ring2 | knowledge/mechanisms/fr-effect-ignite-sources.json |
| fr-effect-mana-300-idols | 神像词条 doubled if 300+ maximum mana（当前条件不满足，doubling 未建模） | 2 | confirmed | not_implemented | hit | idol:equipment:idols.0, idol:equipment:idols.1, idol:equipment:idols.3, idol:equipment:idols.4, stat:mana, condition:mana_ge_300 | ruleset_effect:mana_ge_300_idols | knowledge/mechanisms/fr-effect-mana-300-idols.json |
| fr-effect-rhythm-of-fire-consume | Rhythm of Fire 消耗 12 层：+130% more damage（条件效果） | 2 | assumed | validated | hit | node:fr11mv:3, condition:consume_rhythm_of_fire | ruleset_effect:rhythm_of_fire_consume | knowledge/mechanisms/fr-effect-rhythm-of-fire-consume.json |
| fr-effect-scorching-tide-ignited | Scorching Tide：目标 ignited 时 +60% more melee（条件效果） | 2 | assumed | validated | hit | node:fr11mv:23, condition:ignited | ruleset_effect:scorching_tide_ignited | knowledge/mechanisms/fr-effect-scorching-tide-ignited.json |
| fr-effect-surge-pyroclasm | Surge Pyroclasm：额外施放 Flame Reave（未建模） | 2 | unresolved | not_implemented | frequency | ability:su5g3, node:su5g3:28 | ruleset_effect:surge_pyroclasm | knowledge/mechanisms/fr-effect-surge-pyroclasm.json |
| fr-hit-added-scaling | Flame Reave added damage 池与缩放（400% effectiveness，fire_only） | 2 | derived | validated | hit | ability:fr11mv, stat:added_damage | ruleset_rule:R4 | knowledge/mechanisms/fr-hit-added-scaling.json |
| fr-hit-attack-speed | Flame Reave 攻速模型（技能 as × speedMultiplier 基率，increased 加算/more 分组） | 2 | confirmed | validated | frequency | ability:fr11mv, stat:attack_speed, mechanism:flame-reave:attack-rate-alternatives | ruleset_rule:R3 | knowledge/mechanisms/fr-hit-attack-speed.json |
| fr-hit-conditional-state-eval | FR 条件状态求值（scenario states 分层覆盖与 fail-closed） | 2 | derived | validated | hit, frequency | condition:ignited, condition:enchant_weapon_active, condition:frenzy_active, condition:consume_rhythm_of_fire, condition:consume_blade_weaving, condition:ambit… | conditional_effect:hit-state-evaluation | knowledge/mechanisms/fr-hit-conditional-state-eval.json |
| fr-hit-cooldown-icr | Flame Reave Reflash 冷却与 ICR、cooldown_limited 主口径 | 2 | assumed | validated | frequency | ability:fr11mv, node:fr11mv:16, stat:cooldown_recovery_speed | ruleset_rule:R13 | knowledge/mechanisms/fr-hit-cooldown-icr.json |
| fr-hit-critical-expected | Flame Reave 暴击率/暴伤与 avg crit 期望 | 2 | derived | validated | hit | ability:fr11mv, node:fr11mv:20, node:fr11mv:24, stat:crit_chance, stat:crit_multiplier, condition:ignited | ruleset_rule:R25 | knowledge/mechanisms/fr-hit-critical-expected.json |
| fr-hit-dual-wield-added | 双持武器 added damage 求和与武器 attackRate 不参与 | 2 | confirmed | validated | hit | ability:fr11mv, item:equipment:weapon1, item:equipment:weapon2 | ruleset_rule:R2 | knowledge/mechanisms/fr-hit-dual-wield-added.json |
| fr-hit-events-weights | Flame Reave 每次施放命中次数与伤害权重（4 击） | 2 | confirmed | validated | hit, frequency | ability:fr11mv, node:fr11mv:16, node:fr11mv:18 | ruleset_rule:R1 | knowledge/mechanisms/fr-hit-events-weights.json |
| fr-hit-increased-pools | Flame Reave increased 加算池（damage / attack speed / 属性缩放） | 2 | derived | validated | hit, frequency | ability:fr11mv, stat:damage, stat:attack_speed, stat:intelligence, stat:dexterity | ruleset_rule:R9 | knowledge/mechanisms/fr-hit-increased-pools.json |
| fr-hit-more-groups | Flame Reave more 乘区分组与组间相乘 | 2 | derived | validated | hit | ability:fr11mv, stat:damage | ruleset_rule:R19 | knowledge/mechanisms/fr-hit-more-groups.json |
| fr-hit-offhand-exalted | Whetstone Gavel 副手 Exalted：副手全部 stats +97% effectiveness | 2 | confirmed | validated | hit | item:letools:uniqueList.uniques:438 | ruleset_effect:offhand_exalted_effect | knowledge/mechanisms/fr-hit-offhand-exalted.json |
| fr-hit-rhythm-rotation | Rhythm of Fire 循环平均（每 4 次施放 1 次消耗，周期平均 more） | 1 | assumed | validated | frequency, hit | ability:fr11mv, node:fr11mv:3, node:fr11mv:4 | ruleset_rule:R18 | knowledge/mechanisms/fr-hit-rhythm-rotation.json |
| fr-hit-single-hit-chain | Flame Reave 单次命中乘区链（Fire 单组件） | 2 | derived | validated | hit | ability:fr11mv, node:fr11mv:2, node:fr11mv:3, node:fr11mv:6, node:fr11mv:9, node:fr11mv:12 +6 | ruleset_rule:R4 | knowledge/mechanisms/fr-hit-single-hit-chain.json |
| fr-hit-tag-matching | Flame Reave 伤害标签匹配与组件标签闭包 | 2 | derived | validated | hit | ability:fr11mv, stat:damage, stat:added_damage, stat:attack_speed, stat:crit_chance, stat:crit_multiplier +1 | ruleset_rule:R9 | knowledge/mechanisms/fr-hit-tag-matching.json |
| fr-hit-target-side | Flame Reave 目标侧结算（穿透 / 火抗 / 怪物 DR） | 2 | assumed | validated | hit, target_side | ability:fr11mv, stat:penetration, condition:boss_or_rare | ruleset_rule:R12 | knowledge/mechanisms/fr-hit-target-side.json |
| fr-multi-hit-return-hits | Flame Reave 每次施放多次返回命中与 50% less 伤害权重 | 1 | derived | reusable | hit, frequency | node:fr11mv:16, node:fr11mv:18, stat:hits_per_cast, ability:fr11mv | none:plan-v2 rules.hit_events（定义规则数据，取代旧 ruleset.hit_events） | knowledge/mechanisms/fr-multi-hit-return-hits.json |
| fr-node-arcane-severance | Flame Reave 树节点 12 Arcane Severance（more melee + added spell 转换） | 2 | derived | validated | hit | node:fr11mv:12 | ruleset_effect:arcane_severance_melee_more | knowledge/mechanisms/fr-node-arcane-severance.json |
| fr-node-dancing-flames | Flame Reave 树节点 2 Dancing Flames（攻速/法力/射程） | 2 | derived | validated | hit, frequency | node:fr11mv:2 | ruleset_rule:R3 | knowledge/mechanisms/fr-node-dancing-flames.json |
| fr-node-ember-infusion | Flame Reave 树节点 13 Ember Infusion（more melee damage） | 2 | derived | validated | hit | node:fr11mv:13 | ruleset_effect:ember_infusion_melee_more | knowledge/mechanisms/fr-node-ember-infusion.json |
| fr-node-flame-caller | Flame Reave 树节点 18 Flame Caller（返回命中 + 法力消耗） | 2 | derived | validated | hit, frequency | node:fr11mv:18 | ruleset_rule:R1 | knowledge/mechanisms/fr-node-flame-caller.json |
| fr-node-frenzied-flame | Flame Reave 树节点 6 Frenzied Flame（Frenzy 触发与加成） | 2 | derived | validated | hit, frequency | node:fr11mv:6 | ruleset_effect:frenzy_active | knowledge/mechanisms/fr-node-frenzied-flame.json |
| fr-node-heat-wave | Flame Reave 树节点 9 Heat Wave（more fire damage） | 2 | derived | validated | hit | node:fr11mv:9 | ruleset_effect:heat_wave_fire_more | knowledge/mechanisms/fr-node-heat-wave.json |
| fr-node-precise-destruction | Flame Reave 树节点 20 Precise Destruction（added crit chance） | 2 | derived | validated | hit | node:fr11mv:20 | dataset_entity:node:letools:fr11mv:20 | knowledge/mechanisms/fr-node-precise-destruction.json |
| fr-node-reflash | Flame Reave 树节点 16 Reflash（额外命中与 3s 冷却） | 2 | confirmed | validated | frequency, hit | node:fr11mv:16 | ruleset_rule:R1 | knowledge/mechanisms/fr-node-reflash.json |
| fr-node-rolling-flames | Flame Reave 树节点 25 Rolling Flames（射程，不影响命中伤害） | 2 | derived | validated | area | node:fr11mv:25 | dataset_entity:node:letools:fr11mv:25 | knowledge/mechanisms/fr-node-rolling-flames.json |
| fr-node-versatility | Flame Reave 树节点 4 Versatility（其它技能叠 Rhythm 层） | 2 | derived | not_implemented | frequency | node:fr11mv:4 | ruleset_rule:R18 | knowledge/mechanisms/fr-node-versatility.json |
| fr-skill-level-scaling | Flame Reave 技能等级（实际 27 级只影响节点数量，不缩放伤害） | 2 | confirmed | not_implemented | hit | ability:fr11mv, mechanism:flame-reave:skill-level | ruleset_rule:R16 | knowledge/mechanisms/fr-skill-level-scaling.json |
| item-offhand-exalted-effectiveness | Whetstone Gavel：副手 Exalted 时全部副手 stats 获得 +97% effectiveness | 1 | derived | reusable | hit | item:letools:uniqueList.uniques:438, item:slot:equipment.weapon1, item:equipment.weapon1, item:slot:equipment.weapon2, item:equipment.weapon2, stat:added_damag… | none:plan-v2 rules.item_effectiveness（迁移规则数据；旧 hit_inputs._apply_offhand_exalted_effect 退役） | knowledge/mechanisms/item-offhand-exalted-effectiveness.json |
| item-sword-catcher-frenzy-added-melee | Sword Catcher 的 Frenzy 附加近战伤害 | 1 | derived | reusable | hit | item:letools:uniqueList.uniques:338 | ruleset_effect:item-sword-catcher-frenzy-added-melee | knowledge/mechanisms/item-sword-catcher-frenzy-added-melee.json |
| item-vaions-chariot-movement-to-damage | Vaion's Chariot 移速转加算伤害 | 1 | derived | reusable | hit, dot | item:letools:uniqueList.uniques:264 | ruleset_effect:item-vaions-chariot-movement-to-damage | knowledge/mechanisms/item-vaions-chariot-movement-to-damage.json |
| minion-attribute-scaling-dexterity | Ballista 召唤的敏捷属性缩放（玩家 Dex → minion 增伤） | 1 | derived | reusable | hit, frequency | ability:ba1574, node:ba1574:5, node:ba1574:18 | ruleset_rule:knowledge/definitions/ballista-armed-construction-v4.json#definition.rules.attribute_scaling | knowledge/mechanisms/minion-attribute-scaling-dexterity.json |
| minion-explicit-owner-stat-transfer | 节点驱动的玩家→召唤物属性转移 | 3 | confirmed | reusable | attribute_transfer, crit | ability:ba1574, node:ba1574:22, node:ba1574:27 | ruleset_rule:knowledge/definitions/ballista-armed-construction-v4.json#definition.rules.stat_transfer | knowledge/mechanisms/minion-explicit-owner-stat-transfer.json |
| minion-level-damage-scaling | Minion 等级伤害缩放（公式已核、应用未确认） | 1 | unresolved | not_implemented | hit | ability:ba1574 | dataset_entity:skill:letools:classInfoForSources:4 extensions.minion level scaling | knowledge/mechanisms/minion-level-damage-scaling.json |
| minion-owner-stat-isolation | 召唤物与玩家属性池隔离 | 2 | confirmed | reusable | actor_binding, source_scope | ability:ba1574 | ruleset_rule:knowledge/definitions/ballista-armed-construction-v4.json#definition.actor | knowledge/mechanisms/minion-owner-stat-isolation.json |
| mourningfrost-cold-per-dexterity | Mourningfrost：每点敏捷 +1 冰霜伤害（攻击/法术） | 1 | confirmed | reusable | hit, frequency | item:letools:uniqueList.uniques:19, mechanism:mourningfrost-cold-per-dexterity | ruleset_rule:knowledge/definitions/ballista-armed-construction-v4.json#definition.effects | knowledge/mechanisms/mourningfrost-cold-per-dexterity.json |
| phantom-grip-crit-chance-per-dexterity | Phantom Grip：每点敏捷 +1% increased Critical Strike Chance（腐化词条） | 1 | confirmed | reusable | hit | item:letools:uniqueList.uniques:348, mechanism:phantom-grip-crit-chance-per-dexterity | ruleset_rule:knowledge/definitions/ballista-armed-construction-v4.json#definition.effects | knowledge/mechanisms/phantom-grip-crit-chance-per-dexterity.json |
| skill-abyssal-retribution | Abyssal Retribution 后续主技能增伤 | 1 | assumed | reusable | hit, dot | node:gs15de:21 | ruleset_effect:skill-abyssal-retribution | knowledge/mechanisms/skill-abyssal-retribution.json |
| skill-added-damage-spell-to-melee | Arcane Severance：每 4 点附加法术伤害转为附加近战伤害 | 1 | assumed | reusable | hit | node:fr11mv:12, stat:added_damage, ability:fr11mv | none:plan-v2 rules.added_damage_conversion（迁移规则数据） | knowledge/mechanisms/skill-added-damage-spell-to-melee.json |
| skill-dark-duellist | Dark Duellist 准备反击时的全局增伤 | 1 | derived | reusable | hit, dot | node:gs15de:3 | ruleset_effect:skill-dark-duellist | knowledge/mechanisms/skill-dark-duellist.json |
| skill-entropic-blows | Entropic Blows 的 Time Rot 层数增伤 | 1 | derived | reusable | hit | node:v01cv:25 | ruleset_effect:skill-entropic-blows | knowledge/mechanisms/skill-entropic-blows.json |
| skill-hellish-chasm-next-fire | Hellish Chasm 后续火焰技能增伤 | 1 | assumed | reusable | hit, dot | node:v01cv:3 | ruleset_effect:skill-hellish-chasm-next-fire | knowledge/mechanisms/skill-hellish-chasm-next-fire.json |
| skill-nether-coating | Nether Coating 攻速与致盲增伤 | 1 | derived | reusable | hit, dot, frequency | node:ab0lh:14 | ruleset_effect:skill-nether-coating | knowledge/mechanisms/skill-nether-coating.json |
| skill-void-supremacy | Void Supremacy 对减速目标增伤 | 1 | assumed | reusable | hit, dot | node:ab0lh:19 | ruleset_effect:skill-void-supremacy | knowledge/mechanisms/skill-void-supremacy.json |
| skill-vorpal-force | Vorpal Force 的 Bleed 层数增伤及上限 | 1 | derived | reusable | hit | node:v01cv:29 | ruleset_effect:skill-vorpal-force | knowledge/mechanisms/skill-vorpal-force.json |
| target-critical-vulnerability | Critical Vulnerability 目标被暴击率 | 1 | assumed | reusable | hit | node:v01cv:11, mechanism:letools:LEAbilities.ailmentList:85, mechanism:85 | ruleset_effect:target-critical-vulnerability | knowledge/mechanisms/target-critical-vulnerability.json |
| urzil-pride-all-attributes-corrupted-condition | Urzil's Pride：≥7 件 corrupted 非神像装备时 +8 All Attributes | 1 | confirmed | reusable | hit, frequency | item:letools:uniqueList.uniques:10, mechanism:urzil-pride-all-attributes-corrupted-condition | ruleset_rule:knowledge/definitions/ballista-armed-construction-v4.json#definition.rules.equipment_condition_effects | knowledge/mechanisms/urzil-pride-all-attributes-corrupted-condition.json |

## 技能模板（knowledge/skills）

| ID | 标题 | ability_id | 组件 | 所需计算块 | 调度 driver | 关联机制 | 来源文件 |
|---|---|---|---|---|---|---|---|
| bh-black-hole | Black Hole（黑洞）· 本体 + 自然 Ignite 事件模型 | bh2 | body, ignite | tag_match, added_scaling, increased_aggregate, more_groups +3 | event_simulation | bh-body-component, bh-skill-tree-more-nodes, bh-red-giant, bh-ignite-heat-wave +21 | knowledge/skills/bh-black-hole.json |
| fr-flame-reave | Flame Reave 命中模型技能模板（Mage / Spellblade，case flame-reave） | fr11mv | hit | tag_match, added_scaling, increased_aggregate, more_groups +4 | cooldown_limited | fr-hit-single-hit-chain, fr-hit-events-weights, fr-hit-attack-speed, fr-hit-cooldown-icr +28 | knowledge/skills/fr-flame-reave.json |
| void-cleave-fire-main | Void Cleave 火焰转换主技能 | v01cv | hit, ignite, dot | increased_aggregate, crit, target_side, tag_match +1 | cooldown_limited | — | knowledge/skills/void-cleave-fire-main.json |

## 未解决项（摘要）

| ID | evidence_state | 未解决项数 |
|---|---|---|
| ballista-death-explosion | confirmed | 2 |
| ballista-dexterity-frequency-and-increased | derived | 1 |
| ballista-explosion-damage-composition | derived | 1 |
| ballista-potion-construction-window | derived | 1 |
| ballista-projectile-explosion | confirmed | 1 |
| ballista-summon-limit-lifetime | confirmed | 1 |
| ballista-trap-triggered-summon | derived | 2 |
| bh-attribute-scaling | confirmed | 1 |
| bh-baseline-profiles | derived | 2 |
| bh-binary-system | confirmed | 2 |
| bh-black-hole-instances | confirmed | 2 |
| bh-body-component | confirmed | 2 |
| bh-body-tick-model | confirmed | 2 |
| bh-cast-time | confirmed | 2 |
| bh-cond-apex-glyph-conversion | unresolved | 4 |
| bh-cond-chilled-touch | assumed | 2 |
| bh-cond-conflagrate | assumed | 4 |
| bh-cond-craterborn | confirmed | 3 |
| bh-cond-crescendoing-rage | assumed | 3 |
| bh-cond-eternal-flames-active | confirmed | 2 |
| bh-cond-eternal-flames-passive | confirmed | 1 |
| bh-cond-flame-ward-fire-aura | confirmed | 1 |
| bh-cond-frozen-steel | unresolved | 3 |
| bh-cond-idol-altar-layout-cdr | unresolved | 3 |
| bh-cond-idol-fire-doubling | confirmed | 1 |
| bh-cond-infusion | confirmed | 2 |
| bh-cond-preparation | assumed | 3 |
| bh-cond-throne-of-ambition | confirmed | 3 |
| bh-cooldown-cdr-icr | confirmed | 2 |
| bh-cooldown-charges | confirmed | 2 |
| bh-duration | confirmed | 2 |
| bh-ignite-attribute-scaling | confirmed | 1 |
| bh-ignite-core | confirmed | 1 |
| bh-ignite-damage-flow | assumed | 2 |
| bh-ignite-duration-sources | confirmed | 2 |
| bh-ignite-heat-wave | confirmed | 3 |
| bh-ignite-inheritance | confirmed | 2 |
| bh-ignite-penetration | confirmed | 2 |
| bh-ignite-scaling-pool | assumed | 2 |
| bh-loop-action-policies | derived | 3 |
| bh-monster-dr-integration | assumed | 2 |
| bh-red-giant | confirmed | 2 |
| bh-skill-tree-more-nodes | confirmed | 2 |
| bh-supermassive | confirmed | 2 |
| bh-target-scenario-conditions | confirmed | 3 |
| bh-target-side-settlement | confirmed | 3 |
| bh-weakened-reality | confirmed | 2 |
| common-conditional-state-eval | derived | 2 |
| common-frequency-cooldown | derived | 2 |
| common-increased-aggregate | confirmed | 2 |
| common-ledger | derived | 2 |
| common-monster-dr | confirmed | 2 |
| common-more-groups | confirmed | 3 |
| common-tag-match | confirmed | 2 |
| common-target-resistance-penetration | confirmed | 3 |
| damage-channel-added-and-multipliers | derived | 1 |
| fr-effect-ambition-gain-source | unresolved | 3 |
| fr-effect-army-of-skin-high-health | assumed | 3 |
| fr-effect-blade-weaving-consume | derived | 3 |
| fr-effect-chaos-flare-ignited-crit | assumed | 3 |
| fr-effect-enchant-weapon | derived | 3 |
| fr-effect-firebrand-scope | confirmed | 3 |
| fr-effect-frenzy-active | derived | 3 |
| fr-effect-ignite-sources | derived | 3 |
| fr-effect-mana-300-idols | confirmed | 3 |
| fr-effect-rhythm-of-fire-consume | assumed | 3 |
| fr-effect-scorching-tide-ignited | assumed | 3 |
| fr-effect-surge-pyroclasm | unresolved | 3 |
| fr-hit-added-scaling | derived | 3 |
| fr-hit-attack-speed | confirmed | 3 |
| fr-hit-conditional-state-eval | derived | 3 |
| fr-hit-cooldown-icr | assumed | 2 |
| fr-hit-critical-expected | derived | 3 |
| fr-hit-dual-wield-added | confirmed | 2 |
| fr-hit-events-weights | confirmed | 2 |
| fr-hit-increased-pools | derived | 3 |
| fr-hit-more-groups | derived | 3 |
| fr-hit-offhand-exalted | confirmed | 2 |
| fr-hit-rhythm-rotation | assumed | 3 |
| fr-hit-single-hit-chain | derived | 2 |
| fr-hit-tag-matching | derived | 1 |
| fr-hit-target-side | assumed | 3 |
| fr-node-arcane-severance | derived | 3 |
| fr-node-dancing-flames | derived | 2 |
| fr-node-ember-infusion | derived | 2 |
| fr-node-flame-caller | derived | 2 |
| fr-node-frenzied-flame | derived | 2 |
| fr-node-heat-wave | derived | 2 |
| fr-node-precise-destruction | derived | 2 |
| fr-node-reflash | confirmed | 2 |
| fr-node-rolling-flames | derived | 1 |
| fr-node-versatility | derived | 2 |
| fr-skill-level-scaling | confirmed | 3 |
| minion-attribute-scaling-dexterity | derived | 1 |
| minion-explicit-owner-stat-transfer | confirmed | 1 |
| minion-level-damage-scaling | unresolved | 1 |
| minion-owner-stat-isolation | confirmed | 1 |
| mourningfrost-cold-per-dexterity | confirmed | 2 |
| phantom-grip-crit-chance-per-dexterity | confirmed | 1 |
| urzil-pride-all-attributes-corrupted-condition | confirmed | 1 |
| bh-black-hole |  | 12 |
| fr-flame-reave |  | 16 |
| void-cleave-fire-main |  | 2 |

## 登记告警（重建时输出）

（无）

---

本文件由脚本生成；详细条目内容用 `knowledge show --id <ID>` 获取，不要在此复制完整 JSON。
