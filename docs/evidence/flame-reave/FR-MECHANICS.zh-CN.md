# Flame Reave hit 机制研究（fr-hit-stage1-v1）

> **公开版说明（2026-11-13）**：本文件是作者本地研究记录，保留当时记录的路径与数值作为历史依据；文中 `archive://examples/…`、`archive://reports/…`、`archive://docs/history/…` 与已从公开版移除的历史规则包均为作者本地归档（统一以 `archive://` 前缀标记），不随公开仓库分发。可复用结论与其来源定位见 [docs/evidence/extracted/](../extracted/README.zh-CN.md)。

作者：fr-researcher。日期：2026-11-07。对应机器可读事实：`tools/phase3/rules/fr-hit-facts-v1.json`（schema `le-hit-facts-v1`）。
BD：`archive://examples/flame-reave/inputs/bd.json`（MGThunder，Mage/Spellblade，100 级，save sha256 `ee970886…3475d`）。

## 0. 来源、边界与状态口径

| 来源 | 用途 | 性质 |
|---|---|---|
| `data/releases/phase1-dataset-003/data.sqlite`（letools 1.5.2 归一化） | 技能/组件/技能树/节点数值/buff/ailment/被动/物品/枚举/翻译 | 数据（1.5.2），可引用 locator |
| `archive://reports/discovery/phase-2-o3ZRm9my/planner-runtime.js` | 公式/聚合/池/暴击/异常 buff 的第三方实现 | **第三方参考，不是游戏真值**；locator 为 byte offset + 行号 |
| `archive://examples/flame-reave/inputs/bd.json` / `calculation-input.affix-db.corrected.json` | 本 BD 的选择与 160 条 mod | 本轮导入产物 |
| `tools/save_import/scripts/source-data/szefo94/uniques.json` | 暗金 mod 的英文原文（条件描述） | 项目内置来源数据 |
| `archive://docs/history/reference-le-tools-planner-model.zh-CN.md` | 参考模型的既有结论（属性/施速/聚合/异常） | 历史文档 |

- **没有** 1.5.3 IL2CPP dump / 游戏内实测数据可用（本 workspace 无 `<author-archive>/cpp2rev` 等逆向产物），因此涉及"游戏真值"的机制（双持合成、武器 attackRate、mana 公式顺序、命中次数）只能给数据/参考推导 + `assumed|unresolved`。
- 状态口径：`confirmed`=数据/代码直接可见且无歧义；`derived`=由数据+描述/参考模型推导（写明推导链）；`assumed`=模型假设（缺证据，默认值写在 facts）；`unresolved`=证据冲突或缺失。
- 数据集里技能树节点头的 `property` 是**技能树内命名空间**（5xxx/10001…），不是全局 SP；语义以游戏内文本（description/altText）为准。全局 SP 枚举见 `window.SP`（dataset `enum source_id='SP'`）。

## 1. R1 命中次数（结论：默认 3；命中伤害权重 [1.0, 0.5, 0.5]）

**结论**：Flame Reave 每次施放对同一目标命中 **3 次**：初始 1 次 + Reflash 的 2 次额外命中（Flame Caller 的返回是这 2 次额外的实现基础，不另加 1 次）。额外命中固定 **50% less damage**；本体命中按距离从 0 到 50% less。默认模型：目标贴身 → 权重 `[1.0, 0.5, 0.5]`，单次施放总伤害 = 2.0 × 单次完整命中。状态：命中数 `derived`，伤害权重 `derived`，'Flame Caller 不额外加一次' 为 `assumed`。

证据：

1. Reflash（node 16）description：`Flame Reave now has a cooldown, but it expands and returns a second time. This can hit the same enemy two additional times.`
   altText：`The additional expanding and returning hits from {Reflash} always have the 50% less damage penalty.`
   locator：dataset 003 `source_records entity_kind='skill_tree_node' source_scope='fr11mv' source_id='16'`（= `window.LESkillTrees['fr11mv'].nodes['16']`）；`bd.json` → `specializations[0].nodes[node_id=16]`。
2. Flame Caller（node 18）description：`Flame Reave returns to you. This can hit the same enemy again, but it costs more mana.`
   altText：`Flame Reave's baseline damage penalty from expansion still applies, meaning a returning hit always has 50% less damage.`
   locator：同上 `source_id='18'`。
3. 前置关系：Reflash 的 `requirements=[{nodeId:18, requirement:1}]`（node 16 raw payload）→ Flame Caller 是 Reflash 的前置，说明"返回"由 Flame Caller 提供，Reflash 把它升级为"再返回一次"。
4. 本体组件 `DamageEnemyOnHit.canDamageSameEnemyAgain=false`（dataset `skill_components`，component_index=0）→ 多命中只能由技能树 flag 实现；但 `partialskillBonuses['fr11mv']` 只含 node 8 的 `dealsLightningDamage`，**没有** node 16/18 的 flag（第三方 runtime 也不建模多命中）。
5. 本体 description：`Deals less damage as it expands, up to 50% less at maximum distance.`（dataset `translations key='Abilities.Ability_FlameReave_Description'`）→ 第 1 段也有距离衰减，模型默认贴身=1.0。

**Open question（OQ1/OQ2）**：若游戏实现把 Flame Caller 的返回与 Reflash 的两次额外分开计，则可能是 4 次（DPS +33%）；距离衰减曲线未知。需要游戏内实测（命中数字/战斗日志）。

## 2. R2 双持（结论：武器数据 confirmed；合成口径 unresolved，默认按"求和 + 技能 as 基率"）

**武器数据（confirmed）**：

| 槽 | 基底 | 类型 | attackRate | implicit added melee | 其他 |
|---|---|---|---|---|---|
| weapon1 | Rune Hammer（暗金 Whetstone Gavel） | One-Handed Mace | **0.94** | **+64**（property 0, tags 512 Melee, type added） | +1 Fire Skills / +1 Physical Skills / +8-18 All Attributes / armor% / DoT mitigation；offhand-Exalted 条件不满足 |
| weapon2 | Katana | One-Handed Sword | **1.2** | **+38**（同结构） | implicit crit multi +50%（roll 0.5/0.5） |

locator：dataset `items` 行 `item:letools:itemList.equippable_7:10`（Rune Hammer，`extensions.attackRate=0.94`）、`item:letools:itemList.equippable_9:9`（Katana，`attackRate=1.2`）；暗金 `item:letools:uniqueList.uniques:438`；文本 `szefo94/uniques.json` key `438`。

**合成口径（unresolved，两个候选都要可开关）**：

- 候选 A（参考 runtime 实际行为）：**不做双持合并**。所有已装备物品的 added damage stat 全部进池相加（本 BD：两把 implicit 64+38 + 武器词条 added fire 61+57 + sealed 5 + 被动/神像等），基础速率只由技能 `as`/`useDuration` 决定（1.375/s），武器 `attackRate` 只出现在物品卡 tooltip。
  locator：`planner-runtime.js` byte 39161/101549/131823（`attackRate` 只在 `X()` 显示函数）；byte 569845(L1413) `rrlnr()`；byte 574120-574580(L1424-1425) `rrlzr()`。
- 候选 B（社区常见口径，assumed）：`aps = avg(0.94, 1.2) = 1.07 × (1+Σinc)`，每击伤害取两把平均；交替武器则每击按武器分别取 64/38。
- **无任何 dataset/参考代码证据**证明交替或平均；请在 ruleset 里 `status: unresolved`，并给 `assumed` 的默认值。facts 里 `R2.composition` 记录了三问（交替?/平均?/攻速?）与两候选。

## 3. R3 近战攻速（结论：参考口径 base = as × speedMultiplier；武器 attackRate 不参与 — 游戏真值未验证）

**参考模型公式（derived，reference_third_party）**：

```
base_aps  = 0.75 / useDuration * (speedMultiplier||1) * 1.46667
          = 1.1 * speedMultiplier / useDuration   (=as * speedMultiplier)
rate      = base_aps * (1 + Σ increased attack speed) * Π(1 + more attack speed)
```

- Flame Reave：`as=1.3750031`、`useDuration=0.8`、`speedMultiplier=1`、`speedScaler=2`（=SP.AttackSpeed）、`useDelay=0.2`。
- `speedScaler` 选池：2=AttackSpeed、3=CastSpeed，其它不选池（`rrlor()`，byte 577023/L1430；`Ia()` byte 668200-669470/L1623-1624）。
- increased 池加算（`rrl8o` byte 436600-439200/L1117-1123，`ignoreAdded:true` → `(1+Σinc)×Π(1+more)`）；more 各自相乘。
- `useDelay` 只用于技能卡显示，不参与计时（参考文档 §Q4）。
- `as` 在 runtime 中无读取点；全库交叉验证 `as = 1.1/cast_time_s`（750ms→1.46667、800ms→1.375、1000ms→1.1…）→ `as` 是预计算基础速率。
- **武器 attackRate 不在任何速率公式中**（只显示）；真实游戏里近战技能很可能以武器攻速为基率，但无游戏真值 → `assumed/unresolved`（OQ3/OQ4）。

**本 BD 基线（derived）**：
`Σ increased attack speed = 1.29`（装备 30%+24%+13%=67%；被动 Mage Flurry +15%、Volka's Razor +12%、Essence Duel +15%、Gemini +20%[dualWield 条件满足]）

| 场景 | 参考口径 APS | 武器平均口径 APS（assumed） |
|---|---|---|
| 无 Buff 基线 | **3.149**（1.375×2.29） | 2.450（1.07×2.29） |
| +Enchant Weapon active（+50%） | 3.836 | — |
| +Frenzy（+45.6%） | 3.776 | — |
| EW + Frenzy | 4.463 | — |

locator：`calculation-input.affix-db.corrected.json` mods `property=2 modifier_type=1`；`passive_bonus mg-1` stats `5/48/77/84`；`calc.passive_tree.nodes`。

## 4. R4 条件 Buff 参数（结论见下；Enchant Weapon 是本次最重的一块）

### 4.1 Enchant Weapon（sb44eQ，22 点 / 8 节点，selected）— derived

| 参数 | 值 | 依据 |
|---|---|---|
| base mana | 20 | `skills sb44eQ.manaCost=20` |
| 选中节点后 mana | **5**（20 − 5×3 Efficacy −5/点） | skill_bonus `mods['16'].value=-5`；节点 3 点 |
| base active duration | **5 s** | ability prefab `DestroyAfterDuration.duration=5` |
| 选中节点后 duration | **9 s**（5 + 1×4 Concentration） | skill_bonus `mods['14'].addedDuration=1`；4 点 |
| base cooldown | **15 s**（`round2(1/chargesGainedPerSecond)`, 0.06667） | ability `maxCharges=1, chargesGainedPerSecond=0.06667`；参考模型 Q5 |
| 有效冷却 | **8.33 s**（15/(1+ΣICR 0.80)） | mods `property=70`：waist +30%、feet +30%、ring1 +10%、idol_altar +10% |
| **Passive（常驻）**：melee **50% more** elemental? → 实际 **15% more** melee elemental | 0.15 more（Damage, More, tags Elemental\|Melee） | 技能描述 `Passive: Your melee attacks deal 15% more elemental damage.`；runtime `rrljt.sa()` 硬编码 `this.s.push([...,More,Elemental\|Melee,0,.15])` |
| **Active**：melee **50% more** elemental | 0.50 more（同上 tags） | 描述 `Active: ... 50% more elemental damage for 5 seconds.`；runtime `this.i.push([...,.5])` |
| Active：increased melee attack speed | **+50%**（Celerity 5×10%） | skill_bonus `mods['1'].activeMeleeAttackSpeed=0.1`/点；→ `SP.AttackSpeed, Increased, tags Melee` |
| Active：added melee fire | **+25**（Molten Steel 5×5） | `mods['7'].activeAddedMeleeFireDamage=5`/点 |
| Active：increased melee fire | **+100%**（Molten Steel 5×20%） | `mods['7'].activeIncreasedMeleeFireDamage=0.2`/点 |
| Active：increased melee elemental | **+30%**（Efficacy 3×10%） | `mods['16'].activeIncreasedMeleeElementalDamage=0.1`/点 |
| Active：Fire Aura stacks | +2/s（Power Vent 2 点） | `mods['18'].fireAuraStacksPerSecond=1`/点 |
| Active：chill/frostbite | +10% chill、+20% chill→frostbite（Shivering Blade 1 点） | `mods['20']` |
| Kindling Blade | `autoRecast=true`（近战攻击时若不在冷却则自动施放） | `mods['17'].autoRecast=true`；节点文本 |

locator：dataset `source_records entity_kind='ability' source='letools' source_id='sb44eQ'`；`skill_bonus source_id='sb44eQ'`（= `window.skillBonuses['sb44eQ']`）；runtime `class rrljt extends rrldt super('sb44eQ')`（byte 676800-682200 / L1637-1645）；`translations key='Abilities.Ability_EnchantWeapon_Description'`。

> 注意：**Passive 15% more elemental 是"只要专精了就常驻"**（与是否激活无关）；Active 的 50% more 只在激活时。两个都是独立 more 乘区（同 tags Elemental\|Melee）。

### 4.2 Frenzy（ailment 34）— derived

| 参数 | 值 | 依据 |
|---|---|---|
| 类型/时长 | ailment（非 buff），基础 1s；Frenzied Flame 改为 **3 s**（noScaling 单点） | `ailments ailmentList:34`（duration_ms=1000, max_instances=1）；node 6 `Frenzy Duration 3s(property 5093)` |
| 基础效果 | **+20% increased attack speed**、+20% increased cast speed | ailment 34 `buffs=[{property:2,increasedValue:0.2},{property:3,increasedValue:0.2}]` |
| 本 BD 效果缩放 | Σ increased Effect of Frenzy on You = **+128%**（relic +100%、ring1 +28%） | mods `property=120 special_tag=34` |
| 实际攻速 | **+45.6% increased attack speed**（0.2×(1+1.28)） | runtime `rrlpq()`（byte 523371/L1310）：`b.buffs.map(d=>{d.increasedValue*=1+c;...})` |
| Frenzied Flame 额外 | **+8 added melee fire during Frenzy**（1 点，描述为 additional melee fire） | node 6 `property 5037 value=8` |
| 触发 | FR 命中 10% 几率（1 点；max 4 点） | node 6 `Frenzy Chance 10%(property 10034)` |

### 4.3 Blade Weaver / Blade Weaving（被动 50，8 点）— derived

| 参数 | 值 | 依据 |
|---|---|---|
| max stacks | **8**（=节点点数 ×1，节点最大 10 点） | `passive_bonus mg-1` buffs['50'] = `{id:27 BladeWeaving, prop:0(MaxStacks), value:1}`/点；8 点 → 8 |
| 每层效果 | **15% more Elemental damage**（硬编码） | runtime `case BuffId.BladeWeaving`（byte 494055-494314/L1249-1250）：`.15*stacks`, `SP.Damage, More, AT.Elemental` |
| 获得 | 近战攻击 mana cost **< 10** → +1 层 | 节点描述（`nodes node:letools:mg-1:50`） |
| 消耗 | 近战攻击 mana cost **≥ 10** → 消耗全部层数，该次攻击每层 +15% more elemental（满层 = +120% more） | 同上；buff 描述 `Ailments.NonAilment_BladeWeaving_Description` |

消耗后是否保持/清零持续多久无数据（UIBuffs:29 `newStacksRefreshDuration=false`、`noFixedDuration=false`）→ 模型按"消耗即清零、仅该次攻击生效"。

### 4.4 Firebrand（f1b4d，23 点）+ Incineration 消耗链 — derived

| 参数 | 值 | 依据 |
|---|---|---|
| base max stacks | **4** | `translations key='Abilities.Ability_Firebrand_AltText'` |
| 本 BD max stacks | **9**（4 + Wildfire 2×1 + Ardent Branding +3） | node 9/18（f1b4d） |
| buff 持续 | 4 s（altText）；每层 +5 melee fire damage、范围 +25% | 同上 |
| 本体 addedDamageScaling | 1.5（150%） | ability f1b4d `DamageEnemyOnHit.addedDamageScaling=1.5` |
| Incineration（7，1 点） | 下一个非 Firebrand 近战消耗全部层数，**+15% more damage/层** | node 7 `Damage Per Stack +15%(property 0)` + description `(multiplicative)` |
| Illuminating Fire（8，3 点） | 每层被消耗：**+6% crit chance**、**+12% crit multi** | node 8 `+2%`/`+4%` per point/stack ×3 点 |
| Flare（23，2 点） | Firebrand ignite chance **+50%** | node 23 `+25%`/点 ×2 |
| Conflagrate（27，1 点） | 每次攻击 +1 层（即 2 层/击）、Firebrand mana +3 | node 27 |
| Fulmination（19，3 点） | Firebrand 每层 +3% 攻速、上限 +15%（**只作用于 Firebrand**） | node 19 |
| Brand of Arcanus（20，1 点） | 每层 +5 added spell fire damage | node 20 |

**Incineration 的叠加方式**：description 明确 `deals more damage per stack consumed (multiplicative with other modifiers)`，且参考模型对同节点多点/多层的处理是**线性进同一个 more 因子**（`rrl8o` 的 more 池是 Π(1+单个 more 值)，节点多点=值×层数）→ 模型用 `× (1 + 0.15 × stacks_consumed)`；满 9 层 = **×2.35 more**。状态 `derived`（措辞层面）；若严格"每层各自相乘"会是 ×1.15^9=×3.52 → 差距大，建议 ruleset 记为 `assumed` 并给开关。

### 4.5 Rhythm of Fire（node 3，1 点）— derived

| 参数 | 值 | 依据 |
|---|---|---|
| 层数上限 | **12** | node 3 description `If you already have 12 stacks...`；Versatility 节点：其它技能不能超过 12 |
| FR 每次使用获得 | **+4 层** | node 3 `Stacks on use 4(property 54)` |
| 达到 12 层时 | 该次 FR **消耗全部**，+130% more damage、+130% range、+65% width | node 3 stats（property 0/5010/5015，各 +1.3/+1.3/+0.65）；描述 `all multiplicative` |
| 消耗节律 | 消耗层数的那次使用**自身不叠层** → 默认每 **4 次使用**消耗一次 | node 3 altText |
| Versatility（node 4） | 其它技能直接使用也叠层：Surge +6、Fireball +5、Mana Strike +4、Firebrand +3；这些技能不能消耗层数 | node 4 stats |

**+130% more 的叠加方式**：单次乘区，`×2.3`；不是每层相乘（层数只用于触发，没有 per-stack 数值）。12 层是**触发阈值**不是伤害层数。状态 `derived`。

### 4.6 其它命中的条件层

- **Scorching Tide（node 23，3 点）**：目标 ignited 时 melee 伤害 **+60% more**（+20%/点，`Melee Damage vs Ignited` property 5001）。条件口径：只对 melee 伤害、目标处于 ignited。
- **Chaos Flare（node 24，1 点）**：目标 ignited 时 crit chance **×1.2 more**（20% more/点，property 4）。
- **Heat Wave（node 9，5 点）**：fire 伤害 **+50% more**（+10%/点）。
- **Ember Infusion（node 13，3 点）**：melee **+36% more**（+12%/点）。
- **Arcane Severance（node 12，3 点）**：melee **+12% more**；每 4 点 added spell damage → **+3 added melee damage**（+1/点）。本 BD added spell = Infused Weapon +3 + Burden of Knowledge +15 = 18 → **13.5 added melee**（类型未定，open question）。

## 5. R5 点燃条件（结论：本 BD 有多个 ignite 来源；基线 scenario 默认 ignited=false 合理）

| 来源 | 数值 | 状态/依据 |
|---|---|---|
| 被动 Arcane Flames（node 7，5 点） | 火技能命中时点燃邻近敌人（本次命中可点燃主目标，除非该击杀死目标）；5s 冷却；每点 +1 个目标；节点还有 `Fire Hit Damage vs Ignited +5%`（noScaling，≥5 点生效） | derived，`passive_bonus mg-1` stats['7']，描述 `nodes node:letools:mg-1:7` |
| 装备 Sunwreath（ring2） | **+43% Chance to Ignite on Melee Hit** | confirmed（`items uniques:150` mod property 1 specialTag 1 tags 512；文本 `szefo94/uniques.json` key 150） |
| 技能 Firebrand → Flare（2 点） | Firebrand 命中 **+50% ignite chance** | derived（node 23，f1b4d） |
| Flame Reave 本体 | **0**（无 ChanceToApplyAilmentsOnHit 组件） | confirmed（`skill_components fr11mv`） |
| 其它 | 祝福 10 是 fire res shred（非 ignite）；waist/waist 等无 ignite | — |

口径：`Scorching Tide`/`Chaos Flare` 只在**目标当前 ignited** 时生效；FR 首次命中同一目标时若未先被点燃则不享受（除非 Arcane Flames/Sunwreath 在该次命中的异常判定里先于伤害结算）。建议 scenario 提供 `ignited: true/false` 两态，默认 false（保守）。

## 6. R6 资源（mana）：精确值 unresolved；≥10 阈值结论 derived

**参考公式（runtime `rrl5j` byte 423900/L1089）**：

```
mana_cost = (ability.manaCost + Σ added SP.ManaCost) × (1 + Σ increased SP.ManaCost) / (1 + Σ SP.ManaEfficiency)
```

**本 BD 输入**：base 20；Flame Caller **+6**（node 18 property 66）；Sunwreath **+4 Flame Reave Mana Cost**（property 58 specialTag 1，文本 `+(4-10) Flame Reave Mana Cost`，roll=4）；Dancing Flames 4 点 × “-14% Mana Cost”。

**候选结果**：

| 口径 | 结果 |
|---|---|
| (a) flat 后乘 −56%：`(20+6+4)×0.44` | **13.2** |
| (b) 视为 +56% ManaEfficiency：`30/1.56` | **19.23** |
| (c) 字面 ManaEfficiency −56%：`30/0.44` | 68.2（与节点描述 “costs less mana” 矛盾，排除） |

**阈值结论**：两候选都 **≥10 mana** → 触发 Blade Weaving 消耗（15%/层 more elemental）与 Flame Drinker（+18% more elemental）。注意第三方 runtime 的 `meleeAbilityManaCost` 判定是 `>`（严格大于，byte 461458/L1177），而节点文本是 “10 or more”；本 BD 两口径都 >10，不敏感。

`Dancing Flames` 的 property 69 数值符号与描述冲突（数据 −14%/点，而 ManaEfficiency 正数才降成本）→ 精确 mana 值记 `unresolved`，ruleset 用候选 (a) 13.2 或 (b) 19.23 之一并标 `assumed`。

## 7. R7 Enchant Weapon 是否常驻（结论：可常驻；derived）

- **Kindling Blade（EW node 17，1 点）**：`autoRecast=true`；节点文本：`When you directly use a melee attack, Enchant Weapon will automatically activate if it is not on cooldown.` → 持续打 FR 时每个攻击间隙自动重施。
- 参数：active 时长 **9s**（5+4），有效冷却 **8.33s**（15/(1+0.80 ICR)）→ **冷却 < 时长 → 持续近战攻击时 EW 理论 100% 覆盖**。
- ICR 来源：waist +30%、feet +30%、ring1 implicit +10%、idol_altar implicit +10% = +80%（`calc mods property=70`）。
- 状态：`derived`。未验证：`autoRecast` 的实际触发频率、ICR 是否修饰 `chargesGainedPerSecond` 充能回复（参考模型用通用 `(base+flat)/(1+ICR)`）。

## 8. R8 伤害类型 / 转换 / added 标签（结论：无转换；Fire+Melee；added 伤害分池）

**结论**：

| 项 | 值/口径 | 状态 |
|---|---|---|
| 技能 tags | **Fire(8) + Melee(512)** = 520；组件 `damageTags=512` | confirmed（`entity_tags`/`skills.tags_raw`/`skill_components`） |
| base damage | Fire **2**（`damage=[0,2,0,0,0,0,0]`） | confirmed |
| addedDamageScaling | **4.0（400%）** | confirmed |
| 转换 | **无**（Rolling Thunder node 8 未选；`partialSkillBonuses` 也只含 node 8 的 lightning flag） | confirmed（`bd.json` node_map） |
| crit | 5% / ×2.0 | confirmed |
| added damage 分池 | 见下表 | derived |

| 池 | tags | 本 BD 数值 | 进哪个伤害组件（参考模型） |
|---|---|---|---|
| untyped added melee | 512（Melee） | weapon1 implicit 64 + weapon2 implicit 38 + weapon2 sealed 5 + Infused Weapon 6 + idols.1 7 = **120** | 进 "distributed added" C，按 base 类型占比分配；FR 只有 Fire base → 全部并入 Fire（×4 ADE） |
| added melee **fire** | 520（Fire+Melee） | weapon1 affix 61 + weapon2 affix 57 + Sunwreath 14 = **132** | Fire 组件（×4 ADE） |
| added melee **cold** | 516（Cold+Melee） | Frozen Steel 被动 **+4** | 独立 **Cold 组件**（base=0 → 4×4=16 起），吃 Cold/Elemental inc 与冷穿透，不吃 Fire inc |
| added **spell** | 256（Spell） | Infused Weapon 3 + Burden of Knowledge 15 = **18** | 不自动进入 FR（tag 不匹配）；只能经 **Arcane Severance** 换算：18/4×3 = **13.5 added melee**（类型未定，`unresolved`） |

**Frozen Steel 是否作用于 Flame Reave**：**是**（FR 是 melee attack，tags 516 ⊆ Melee|Cold 的请求池），但作为**独立 Cold 伤害组件**，不是加到火伤上。locator：`passive_bonus mg-1` stats['36']（`added property 0 value 1 tags 516`/点 ×4）；`planner-runtime.js` byte 574120-574580（`rrlzr` 按 damage type 循环）。

**其它伤害标签口径**：`increased damage`（tags 0）与 fire/melee/elemental 都进 inc 池；`increased fire damage` 只对 Fire 组件；`increased melee damage` 对 Fire+Cold 两组件都算（tags 512 ⊆ 各自请求池）；`more` 按各自 tags 过滤。**注意**：reference runtime 对 `property=Damage type=0` 且**没有元素 tag** 的 stat 要求 tag 精确匹配（`rrljp` byte 439692/L1126）→ untyped melee added 只在 distributed 池（请求 tags=Melee）被计入，然后按 base 占比分配到 Fire。

## 9. A 属性派生（engine 需要 attributes 值）

**口径**：`属性 = 职业基础 + 任务 +2（全局常量） + 装备/神像/祝福 flat + 被动 flat`。`All Attributes` 展开到五项。

| 属性 | 物品/idol flat | 被动 flat | 职业基础 | 任务+2 | **合计（含 class+quest）** | 不含 class/quest |
|---|---:|---:|---:|---:|---:|---:|
| Str | 17（hands 9 + ring1 8） | 0 | 0 | 2 | **56** | 54 |
| Vit | 8（ring1） | 0 | 0 | 2 | **47** | 45 |
| Int | 60（head 15+13、ring1 16、ring2 16） | 24（Arcanist 8×1、Arcane Warden 5×1、Mental Fortitude 5×1、Illusory Combatant 6×1） | 3 | 2 | **126** | 121 |
| Dex | 29（head 16+13） | 11（Defender of Welryn 5×1、Illusory Combatant 6×1） | 0 | 2 | **79** | 77 |
| Att | 0 | 0 | 0 | 2 | **39** | 37 |
| **total** | | | | | **347** | 334 |

另：`All Attributes` flat = 9（chest Core of the Mountain）+16（weapon1 Whetstone Gavel）+2（ring1 implicit）+10（relic Shattered Worlds）= **37**。
**FR 的 attributeScaling**：Int 与 Dex 各 **+4% increased Damage/点**（tags 0）→ 含任务版 `(126+79)×4% = +820% increased Damage`（不含 class/quest 版 +792%）。
locator：dataset `class source_id='1'`（baseIntelligence=3 等）、`attributePointsFromQuests=2`、`passive_bonus mg-1`；`skills fr11mv extensions.attributeScaling={2:0.04,3:0.04}`；runtime `rrlxo()`（byte 448901-449700/L1148-1150）与 `rrlfp()`（byte 458672/L1170）。

**open question**：任务 +2 是否计入（save 没有 attributes 字段；参考 runtime 用全局常量 2，第三方参考非游戏真值）。建议 ruleset 给 `include_quest_attributes: true` 并标 `assumed`。

## 10. B 暴击（crit multi 加在 200% 上；Chaos Flare/PD 口径）

**模型（runtime，derived）**：

```
crit_chance = (base_crit_chance + Σadded) × (1+Σincreased) × Π(1+more)   cap 1.0
crit_multi  = (base_crit_multi  + Σadded) × (1+Σincreased) × Π(1+more)   （不加 cap）
avgCrit     = (1−crit_chance) + crit_chance × crit_multi
```

locator：`rrlzr` byte 574120-574580（`R=rrlO(...,CriticalChance,{baseValue:l,cap:1})`、`N=rrlO(...,CriticalMultiplier,{baseValue:n})`、`Q=Q*(1-W)+Q*W*N.B`）；`rrlO` byte 434700-435800（baseValue 计入 added 池）。

**结论**：
- **crit multi 是加算到 base 200% 上**（`2.0 + Σadded`），不是独立乘区；本 BD 装备/祝福的 **+275%**（weapon1 affix 50、weapon2 implicit 50、weapon2 affix 48、weapon2 corrupted 18、waist 19、relic 50、blessing3 40）→ **4.75×**（idols.1 corrupted +10% 带 tags 8192=Minion 不作用于玩家）。
- **Precise Destruction +8%**（2 点×4%）是 **added** 进 crit chance 池（base 5%+8%+relic 5% = **18%** 起点）。
- **Chaos Flare “20% more crit chance vs ignited”** 是 **more**，乘在 **crit chance 总量**上（×1.2）；参考 runtime 的 crit chance 计算用默认池（非 onlyAdded/ignoreAdded）→ more 会作用于总量。状态 `derived`（该节点不在 partialSkillBonuses，属文本推导 + 池规则）。
- increased crit chance：waist +17%；被动 **Defender of Welryn**（5 点）：`increased crit chance 0.08/点 with per={stat:Dex,value:1}` —— 精确公式（0.08×points×Dex?）未定，`unresolved`（影响是否满暴）。若按 `0.08 × 5 × 79 = 31.6` 会是 3160% inc（大概率满暴）；若按 `0.08×5=0.4` 则 +40% inc。engine 需二选一标 assumed。

## 11. C 攻速（同 R3；Lead 要的独立小节）

- 装备：hands affix **+30%** melee AS、hands unique **+24%**、feet unique **+13%**（全部 tags 512 → 作用于 melee 攻击）。
- 被动：Mage Flurry **+15%**（3%/点×5，tags 0 全攻速）、Volka's Razor **+12%**（2%/点×6）、Essence Duel **+15%**（3%/点×5，melee）、Gemini **+20%**（dualWield 条件：主手 mace + 副手 sword，满足）。
- Σinc = **1.29**；**无 Buff 基线 APS**：参考口径 **3.149/s**（1.375×2.29）；武器平均口径 **2.450/s**（1.07×2.29，assumed）。
- 附加：EW active +50%（→3.836/s）；Frenzy +45.6%（→3.776/s）；两者 → 4.463/s。
- more attack speed：本 BD 无来源（EW/被动的攻速都是 increased 池；Firebrand 的 Fulmination 只作用于 Firebrand）。

## 12. D 命中次数复核（同 R1）

见 §1。**默认 `hits_per_cast=3`**，`damage_weights=[1.0,0.5,0.5]`；若 engine 想更保守可用 `[0.5,0.5,0.5]`（目标在最远距离）或 `2` 次（假设 Flame Caller 返回被 Reflash 覆盖且 tooltip 的 "two additional" 含一次返回）。**不要**用 4 作为默认（无证据）。

## 13. E 资源/条件词条（max mana、神像 300 条件、feet unique 更正）

**max mana（derived）**：

```
max_mana = (baseMana 50 + manaPerLevel 0.50506×100 + flat + 2×Attunement)
           × (1+ΣincMana) × Π(1+moreMana)
         = (50 + 50.506 + 60 + 2×39) × (1+0) = 238.51
```

- flat mana 60 来自 Blade Conduit 被动（node 52：added Mana 12/点 ×5）。
- 无 increased mana 词条；idol_altar `+2 Mana per Idol in a Refracted Slot` 因无折射不生效。
- **238.51 < 300 → 4 条神像的 “doubled if 300+ max mana” 条件不满足**；按单倍计入 inc fire damage：idols.0 +36%、idols.1 +37%、idols.3 +25%、idols.4 +28%（合计 +126% inc fire）。状态 `derived`（公式来自第三方参考；即使公式有小偏差，238.5 距 300 差 61.5，且无 %mana 来源，结论稳健）。

**feet unique 更正（重要）**：`equipment.feet#unique_mod#3` = Army of Skin 的 `(26-52)% more Melee Damage to High Health Enemies`（本 BD roll **49%**），**不是** “只作用于 Flay/Harvest”。证据：
- `szefo94/uniques.json` key `443`：mods 第 4 条原文 `(26-52)% more Melee Damage to High Health Enemies`；
- dataset `items uniqueList.uniques:443` mods[3] = `{property:117 GlobalConditionalDamage, specialTag:2, tags:512 Melee, type:2 More, value:0.26, maxValue:0.52}`；
- runtime `rrlPo()`（byte 444900-447300/L1140-1142）：specialTag 2 → condition `{enemyHealth:"high"}`；`rrlRo` 用 `rrlMp(enemyHealth,'high')`，`high|full` 为真。
→ **该 49% more 应进入 Flame Reave 的伤害乘区**（目标 high/full 血量时）。这是本 BD 一个显著乘区，不能丢弃；是否默认开启建议由 scenario 控制（默认开启、标 assumed）。

**其它条件词条**：Whetstone Gavel 的 “offhand Exalted Weapon” 加成**不满足**（副手为 base Katana）；Sunwreath “Flame Reave is a circle” = 半径 −35%（几何变化，DPS 数值不变）；Shattered Worlds “+1 to All Skills” 对 FR 无伤害路径（`levelScaling=[]`）；Shattered Worlds 的 Haste 与 EW/Frenzy 无关。

## 14. 明确未解决 / 需实测项（与 facts `open_questions` 对应）

| ID | 项 | 影响 |
|---|---|---|
| OQ1 | Reflash+Flame Caller 实际命中数（3 or 4） | DPS ±33% |
| OQ2 | 本体距离衰减曲线/典型距离系数 | 单次命中 ×2 差异 |
| OQ3/OQ4 | 双持合成、武器 attackRate 是否作为基率 | APS 口径差 28% |
| OQ5 | Dancing Flames mana 落地口径（13.2 vs 19.23） | 只影响 mana 数值，不影响 ≥10 判定 |
| OQ6 | 任务 +2 属性是否计入；Defender of Welryn per-Dex 公式 | inc Damage ~1.5%、crit chance 可能满暴 |
| OQ7 | Firebrand 层数 +5 melee fire 是否作用 FR；层数持续/刷新 | 每层最多 ~20 基础伤害 |
| OQ8 | Rhythm 消耗节律（12 层、每 4 次使用）与其它技能叠层干扰 | 平均 more ~1.325× |
| OQ9 | Frozen Steel +6 “vs chilled/frozen” 的池与适用范围；Arcane Severance 换算后类型 | 次要 |
| OQ10 | Army of Skin 49% more 的 “high health” 阈值/场景开关 | 条件满足时 +49% more |

## 15. 复现

只读脚本在 `<author-archive>/flame-reave-research/`（不进交付物）：
`q.py`（sqlite 只读查询）、`dump_tree.py`（技能树节点/效果/值）、`node_payload.py`（节点原始 payload）、`abilities.py`（技能 ability 字段）、`passive_bonus.py`（`window.passiveBonuses['mg-1']`）、`dump_buff.py`（UIBuffs）、`tr.py`（translations）、`rt.py`（runtime byte window/关键词定位）、`uniques.py`（szefo94 暗金文本）、`mods_analysis.py`、`compute.py`、`gen_facts.py`（生成 facts JSON）。

## 用户裁决（2026-11-07）

以下三条由用户直接确认，规则包 `archive://ruleset-fr-hit-stage1-v1.json` 的 `user_confirmations` + R2/R3/R5 已记录：

1. **双持**：两把武器都生效，added damage 直接相加（不取平均）。
2. **攻速基础公式**：`base_aps = as × speedMultiplier`（= 1.1 × speedMultiplier / useDuration）正确；
   武器 attackRate 不参与；increased/more 池不变。
3. **无类型 added damage**：是自适应伤害，可以当作任意属性的 added damage；本案例单 Fire 组件下按 Fire 结算。

仍开放：技能等级缩放（dataset `levelScaling=[]`，见 OQ/R16）；crit 缩放被动公式（OQ6）；
Firebrand/Blade Weaving/Rhythm 的补层与消耗节律（固定条件估算，未做时间模拟）；
Army of Skin 的 high-health 阈值判定（R20 assumed）。
