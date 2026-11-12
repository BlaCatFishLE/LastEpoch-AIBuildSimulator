# Ballista 爆炸 DPS 模型数值参数包（2026-11-13）

本文件是机器可读包 [ballista-build-values-20261113.json](ballista-build-values-20261113.json) 的中文说明，记录当前构筑（beta_5 存档导入）为 Ballista 爆炸 DPS 模型准备的**玩家侧 + minion 侧 + 节点侧**全部数值、来源与未确认项。仅新增本文件与同名 JSON，不改动本目录已有证据。

## 0. 口径与交付约定

- **DPS 输出单元**：`ability_gen327`（`ballistaExplosion`，Explosives），由 Ballista 节点 `Armed Construction`（node 5）在弩炮到期/被销毁时触发的爆炸。**不**计算 bolt 直击、Explosive Delivery 命中爆炸、点燃/持续伤害。
- **数据版本**：dataset `phase1-dataset-003`（目标游戏 1.5.2），build revision `rev-c83726fd2c25625d`，calculation input 已冻结。
- **用户已确认的前提**（写入 JSON `conventions`）：
  1. Minion 默认不继承人物属性、暴击与伤害词条；
  2. 只有 `Shared Enhancements`（node 22）与 `Perfect Aim`（node 27）按节点比例显式转移；
  3. `Elixir of Construction`（node 25）在本模型按 100% 覆盖处理。
- **三类值的标注**（贯穿 JSON `*_kind` / `value_kind` 字段与本文件）：
  - **dataset 原值**：直接从 `data/releases/phase1-dataset-003/data.sqlite` 读出（skills/minions/nodes/effects/effect_values/translations/tags/classes）。
  - **现有产物值**：来自已冻结的 calculation input、BuildSpec、prepare 产物（例如节点当前点数、装备词条实例、`attributePointsFromQuests`）。
  - **本轮计算值**：本轮在 `<author-archive>/mech-data/` 用现有通用映射 API 聚合得到的池/总值（Dex、crit 池、mana 等）。均写明复现脚本。

## 1. 复现方式与数据来源

| 用途 | 位置 / 接口 |
|---|---|
| dataset（只读） | `data/releases/phase1-dataset-003/data.sqlite`（`mode=ro&immutable=1`）；表 `skills`/`skill_components`/`minions`/`nodes`/`effects`/`effect_values`/`translations`/`stats`/`tags`/`classes`/`source_records` |
| letools 原始静态数据 | 外部资产，登记见 `archive://docs/assets-external.json`；本次使用 `lastepochsupertools_v1.3.1/data/version150/planner/js/517be1eca36fc17ea39ea512e30f4499.js`。dataset 内 locator 指向同内容的 version152 URL（`.../version152/planner/js/3efb2055376cc1c0647de8711a4198ae.js#window.LEAbilities...`），抽查的 12 个 ability 两版一致 |
| 节点当前点数/装备/被动 | `archive://examples/ballista-beta5-native-20261112/inputs/calculation-input.corrected.json`（30 items、189 mods、25 个被动节点/113 点、5 个专精） |
| 通用词条→贡献映射 | `tools/phase3/src/le_calc/contribution_mapping.py` 的 `base_contributions` / `mod_contributions(map_mod)` / `passive_contributions` / `attribute_detail` |
| 属性/法力/抗性聚合语义 | `tools/phase3/src/le_calc/stats.py` 的 `compute_attributes` / `compute_mana` / `compute_resistances`（本轮按其公式语义复算池，未改代码） |
| 暴击公式参照 | `tools/phase3/rules/fr-hit-facts-v1.json#critical`：`crit_chance=(base+Σadded)×(1+Σincreased)×Π(1+more)`，cap 1.0；`crit_multiplier=(base+Σadded)×(1+Σincreased)×Π(1+more)`；基础 5% / 200% |
| 本轮临时脚本 | `<author-archive>/mech-data/`：`01_explore_dataset.py`、`03_dump_core.py`、`11_dump_letools.js`、`25_player_rows.py`、`30_final.py`、`40_build_package.py`、`42_patch_package.py` |

**为什么没有直接用 `le_calc.stats.build_snapshot`/`le_calc.inputs.load_bundle`**：这两个入口把 phase-2 Black Hole 案例写死（`EXPECTED_ITEM_COUNT=29`、`EXPECTED_MOD_COUNT=148`、`bh2` 专精/节点映射、`mg-1` 被动表），本案例（30 items/189 mods/`ba1574`/`rg-1`）过不了硬编码校验；按写入范围约束未修改 `tools/`，改为在 `.scratch` 中直接复用同一套 `contribution_mapping` 通用映射与 `stats` 聚合语义。这是**复用**，不是新建技能专用计算器。

## 2. 技能 / prefab 数值（dataset 原值）

### 2.1 主技能 ba1574（Ballista 召唤）

- `internalName=summonBallista`，`manaCost=44`，`as=1.8333375`，`useDelay=0.15`，`useDuration=0.6`（useDuration 仅 letools 原始数据有，dataset extensions 未导出），`cast_time_ms=600`，`speedScaler=54`，`speedMultiplier=1`，`tags_raw=0`（`fakeTags=8192` Minion，`minionTagsDisplay=2049` Bow+Physical）。
- `comboAbilities=["ba1574_bblsc7_1"]`，`comboAbility=true`，`comboTimeLimit=0`。
- `attributeScaling`：`{"3":[{property:0, tags:8192, increasedValue:0.04}, {property:2, tags:8192, increasedValue:0.01}]}`。按项目已确认的属性索引 `0=Str,1=Vit,2=Int,3=Dex,4=Att`，即 **Dexterity → +4% increased damage、+1% increased attack speed（tags=8192 Minion）**。是否作用于召唤出的 minion 属未确认项（见 §7）。
- `levelScaling`：`[{property:7, tags:8192, addedValue:75}]`（minion Health +75）。
- prefab `SummonEntityOnDeath`：`entity=100053`、`numberToSummon=1`、`duration=10`（存续 10s）、`limit=2`、`limitNumber=true`、`limitDuration=true`、`summonIsASummoner=true`、`companion=false`、`statList=[]`。

### 2.2 子技能 / 爆炸 / 命令技能（身份与差异）

| ability | internalName | tags | mana | cast/useDuration | as | useDelay | 关键组件 | 身份/差异 |
|---|---|---|---|---|---|---|---|---|
| `entity_bbl7_57` | ballistaBolt | 2049 = Bow+Physical | 0 | 2300 ms / 2.3 s | 0.47826198 | 1.6 | DamageEnemyOnHit：40 **Physical**、200% added eff、5%/200%；DestroyAfterDuration 0.3 | minion 100053 的 `abilityList` 唯一技能（弩箭直击）；`attributeScaling` Dex→+5% attack speed（property 2, tags=0） |
| `ability_gen327` | ballistaExplosion | 2056 = Bow+Fire | 0 | 1000 ms / 1.0 s | 1.1000025 | 0.5 | DamageEnemyOnHit：`damage=[0,50,0,0,0,0,0]` **50 Fire**、250% added eff、5%/200%、`damageTags=2048`(Bow)；无 DestroyAfterDuration | **本模型 DPS 单元**；node 5（Armed Construction）与 node 12（Explosive Delivery）的 `relatedAbilities` 都指向它 |
| `ability_gen517` | ballistaShot | 2049 = Bow+Physical | 0 | 500 ms / 0.5 s | 2.200005 | 0.25 | 40 Physical、200%、5%/200%；DestroyAfterDuration 0.3 | 与 entity_bbl7_57 同伤害但攻速/延迟不同；不在 100053 abilityList 内，属另一套弩炮射击变体 |
| `ba1574_bblsc7_1` | ballistaShotCommand | 0（fakeTags 2049） | 44 | 600 ms / 0.6 s | 1.8333375 | 0.3 | 仅 DestroyAfterDuration 0.1 | ba1574 的 combo 命令技能（指挥弩炮射击），无自身伤害 |
| `ability_gen237` | smallExplosion | 1032 = Throwing+Fire | 0 | 1000 ms / 1.0 s | 1.1000025 | 0.5 | DamageEnemyOnHit：40 **Fire**、200%、5%/200%、`damageTags=1024`(Throwing) | `altTextKey` 为 “Ability_Decoy Small Explosion”，**不是** Ballista 爆炸；Ballista 两个爆炸节点均未引用它 |

### 2.3 minion 100053（Ballista）

- `minion_type=rogue_ballista`，`inherits=null`（不继承任何 actor 模板），`actor_data={id:433532438, genId:"rogue_ballista", nameKey:"Actors.Actor_Name_Ballista"}`。
- `health.maxHealth=1000`、`effectiveHealthModifier=2`、`damageable=true`、`canDie=true`。
- `base_stats.stats`（property / increased / more）：
  - `property 0 = Damage`，`moreValues=[0.5]` → 候选读法 **+50% more damage**；
  - `property 2 = Attack Speed`，`increasedValue=0.2` → **+20% increased attack speed**。
- `abilityList={abilities:["entity_bbl7_57"]}`；`abilityMutators.fi9.increasedRadius=0.2`（Explosive Trap 半径 +20%，来源为 actor 模板遗留，未确认是否作用）。
- `abilityRangeList`：engageRange 13 / maxRange 20 / persuitRange 40；`threat=0.001`。

## 3. 当前 14 个 Ballista 节点（dataset `nodes` + `effects`；当前点数来自 calculation input）

树 `tree:letools:LESkillTrees:ba1574`，**当前 31 点 / 14 节点**（node_entries=14）。节点 stat 的 `property/value/tags` 全部原样收录在 JSON `nodes.nodes[].stats[]`；`value_kind=percent_modifier` 表示游戏内百分数显示。`noScaling=false` 表示该值**每点**生效（按点数线性）；`noScaling=true` 表示只生效一次。

| node | 名称 | 当前/最大 | 机制类别 | 关键 stat（property → 值/点；tags） | 说明 |
|---:|---|---:|---|---|---|
| 2 | Efficient Construction | 2/2 | mana、duration | 8→-25%（Mana Cost）；5093→-20%（Duration） | 44 蓝的 -50%、存续 -40% |
| 5 | Armed Construction | 4/4 | explosion、area、more damage per Dex | 5078→flag “Ballista Explodes”；0→+1% **More Damage per Dexterity**；5015→+1% Increased Area per Dexterity | **DPS 单元触发节点**；4 点 → +4% more/Dex |
| 10 | Flaming Shots | 4/5 | added fire（bolt）、ignite | 5037→+3 Fire Damage；10001→+10% Ignite Chance | 作用于 bolt，非爆炸；对爆炸 DPS 不进池 |
| 11 | Rapid Fire | 1/4 | attack speed | 2→+6% | increased attack speed /点 |
| 12 | Explosive Delivery | 1/1 | 命中爆炸、attack speed(less) | 5078→flag “Exploding Bolts”；2→-40% Less Attack Speed；config `explodeFire=true` | 命中爆炸与 Armed Construction 同引用 gen327，但触发时机不同；-40% 攻速影响弩炮 |
| 14 | Practical Build | 1/1 | more damage、more attack speed、duration | 0→+40%（描述为 more，multiplicative）；2→+40%（More Attack Speed）；5093→-25% Duration | 描述明确 “multiplicative with other modifiers” |
| 15 | Sturdy Foundation | 2/3 | health、duration | 7→+25% Health；5093→+15% Duration | 存续 +30% |
| 18 | Agile Engineering | 1/1 | attack speed per Dex、placement speed | 54→+1% Placement Speed per Dex；2→+1% Attack Speed per **5** Dex | 当前 Dex 228 → +45.6% increased attack speed 候选 |
| 22 | Shared Enhancements | 3/3 | player modifier ratio | 0→+25% “Damage Stat Ratio” | 3 点 → 转移比例 75% |
| 23 | Heavy Bolts | 3/3 | more damage、attack speed(less) | 0→+12%；2→-5% Less Attack Speed | 3 点 → +36% more 候选、-15% 攻速 |
| 25 | Elixir of Construction | 3/3 | more damage（药剂条件） | 0→+15% | 3 点 → +45% more（100% 覆盖假设） |
| 26 | Sharpened Tips | 1/2 | base crit | 5007→+3% Base Crit Chance | 1 点 → Ballista 基础暴击 +3%（是否作用于 gen327 未确认） |
| 27 | Perfect Aim | 4/4 | player crit ratio | 4→+25% Critical Chance Stat Ratio；5→+25% Critical Strike Multiplier Stat Ratio | 4 点 → 两项比例均 100% |
| 31 | Care Package | 1/1 | potion | 49→10% Potion Drop Chance Per Second | 与 Eternal Font 药水联动 |

**关键语义提醒**（已写入 JSON `unknowns:property-id-context-reuse`）：skill node stat 的 property id 不能直接用 dataset `stats` 表名字解释。例：property `8`（dataset “Mana”）在此节点上下文是 **Mana Cost**；property `49`（dataset “Potion Slots”）在此是 **Potion Drop Chance Per Second**；`5015/5037/5078/5093/5007/10001` 等 5xxx/1xxxx id 在 dataset `stats` 表**没有**对应行，本包按 letools 的 stat 显示名标注语义（JSON 中 `property_semantics_label`），并标注 `property_namespace_known=false`。

**节点更多/增伤语义候选**：dataset 的 node stat 只存 property+value，不存 operation（more/increased）。本包对每个节点额外给出 `description_operation_candidates`（依据节点描述里是否出现 “more damage (multiplicative with other modifiers)” / “increased attack speed” / “shorter duration” 等措辞），最终 operation 由 re-gamecode 的代码结论确定。

## 4. Explosive Trap 召唤链（Ballista 召唤速率来源）

当前构筑还在 `ex4tp`（Explosive Trap）**25 点 / 19 节点**，并通过 node 30 `Siege Engineering` 在陷阱引爆时按概率召唤 Ballista。JSON `explosive_trap_chain` 收录全部组件与事件参数。

### 4.1 技能与实体（dataset 原值）

- `ex4tp`（active，Fire+Throwing tags=1032）：`manaCost=8`、`cast_time_ms=1350`、`useDuration=1.35`（letools）、`as=0.81481665`、`useDelay=0.4`；prefab：`DestroyAfterDuration 0.3` → `CreateAbilityObjectOnDeath` → `ex4tp_fi9_1`。
- `ex4tp_fi9_1`（陷阱本体，`explosiveTrapOnGround`）：`DestroyAfterDuration duration=30`；`MineTrigger{ability:"ex4tp_fi9_1_detex_1", triggerType:0, castDelay:0}`。**arming time、基础最大陷阱数、triggerType 0 的精确语义在 dataset/letools 中都不存在 → unknown**。
- `ex4tp_fi9_1_detex_1`（引爆）：30 **Fire**、150% added eff、5%/200%、`damageTags=1024`(Throwing)。
- `ability_gen408`（`blastRainStarter`，node 26 启用）：`DestroyAfterDuration 0.3`；`CastAfterDuration` ×2：一组 `interval=1, castOnStartAsWell=true, remainingCasts=1`，另一组 `interval=0.05, remainingCasts=2` → 共 **3 个 projectile**；projectile `ability_gen408_ex4tp_1`（`blastRainProjectile`，duration 0.5）死亡时 `CreateAbilityObjectOnDeath → ex4tp_fi9_1` → 每个 projectile 生成 1 个陷阱。
- `fi9`（Explosive Trap 的原版独立技能）仅作对照：25 Fire/125%、duration 0.875、40% ignite——不是本构筑链路实际使用的实体。

### 4.2 当前 ex4tp 节点（19 项，含 root）与事件参数

已分配：`2(1/4) 7(1/1) 8(2/5) 9(1/1) 10(1/1) 11(1/3) 19(2/3) 20(1/1) 21(1/1) 23(1/4) 24(2/2) 25(2/2) 26(1/1) 27(1/1) 28(3/3) 29(1/1) 30(2/2) 31(1/1)` + root `0(0/0)`（点数为当前/最大）。

| 参数 | 数值（dataset） | 用途 |
|---|---|---|
| 直接施放 | cast 1350ms、as 0.8148、useDelay 0.4、mana 8 | 施放频率 |
| 每次施放投放次数 | Blast Rain 3 projectile（1+2） | 每次施放 → 3 次陷阱投放（候选） |
| Clustered Explosives 24（2/2） | +1 陷阱/点 → **+2**；mana +6/点=+12 | 每次直接使用额外陷阱（与 3 projectile 相乘与否 unknown） |
| Trapas 9 + Static Mines 7 + Coldsnap Mines 10 | `throwsTrapOfEachConversion`、`convertToLightning`、`convertToCold` | 直接施放时抛出“未转换 + 各转换”多重陷阱变体（最多 3 种伤害类型） |
| Minefield 23（1/4） | +1 最大陷阱/点 | 最大激活陷阱数 +1 |
| Trap Sprinkler 28（3/3） | +10% drop/点=**30%**、距离 6m、mana +1/点 | 每次引爆按概率原地掉落新陷阱 |
| Automated Bombardment 29（1/1） | **+70%** drop、mana +11；该方式掉落的陷阱**不能再掉落** | 与 28 合计 **100%（加算候选）** |
| Trapuchet 31（1/1） | 掉落改抛向 12m 内敌人；12m 内有敌人即可继续掉落 | 覆盖 6m 判定 |
| Impact Trigger 27（1/1） | 箭落地即引爆（忽略 arming）、stun chance +75% | 消除 arming 延迟 |
| Siege Engineering 30（2/2） | **10%/点 = 20%** 概率在每次陷阱引爆时召唤 Ballista；消耗 Ballista mana 的 **50%** | Ballista 召唤率核心参数 |
| 召唤 mana 候选 | 44×50% = 22，或 node2 减免后 44×0.5×50% = 11 | 取决于 50% 作用对象 |
| Subtle Sabotage 25（2/2） | mana -12%/点 = -24%、damage -8%/点、area -8%/点 | trap mana |
| Jelkhor 19（2/3） | Mana Efficiency +8%/点=+16%、Throwing Attack Speed +4%/点=+8% | trap mana / 投掷攻速 |
| trap mana 候选 | 加算 (8+12+3+11+3)×(1-0.24-0.16)=**22.2**；乘算 ×0.76×0.84=**23.62** | 两种口径均未确认 |
| 每次施放引爆数 | 3 投放 ×（1 基础 + 2 Clustered，是否相乘 unknown）；每次引爆 20% 召唤 Ballista；Trap Sprinkler 链最多再加 1 次（掉落陷阱不能继续） | 供 Lead 推导召唤速率 |

这些数值足以构造“每次施放 → 期望陷阱数 → 期望引爆数 → 期望 Ballista 召唤数”的事件链，但 3 处乘法/加法口径（blast rain × clustered、drop chance 加算、mana 效率）需要 re-gamecode 结论。

## 5. 玩家侧最终数值（本轮计算值；公式与来源逐项可追溯）

> 数据源：`contribution_mapping` 通用映射（189 mods + 25 被动节点 + 职业基础 + 任务全属性）；每条来源的 `contribution_id`/`source_kind`/`source_label`/`source_path` 在 JSON `player_stats.*.rows` 中。**Minion 不继承**：带 Minion tag 的行计入 minion 直接池，其余行属于玩家侧池。

### 5.1 属性与 Dexterity

| 项 | 值 | 来源 |
|---|---:|---|
| Strength / Vitality / Intelligence / Dexterity / Attunement | 16 / 2 / 14 / **228** / 12 | 见 JSON `attributes` 与 `attribute_sources` |
| Dex 职业基础 | 3 | Rogue `characterClass.baseDexterity=3`（dataset classes） |
| Dex 任务全属性 | +2 | `LEAbilities.attributePointsFromQuests=2`（dataset source_records；参考模型确认折算为五项各 +2） |
| Dex 装备 flat | 190 | 14 条装备/神像 Dex 词条（`mod:equipment.*`，JSON 逐条列出来源） |
| Dex 被动 flat | 33 | `passive:0:0`(Steady Hand 8)、`21:0`(Focus Fire 8)、`50:0`(Cloak of Shadows 3)、`81:0`(Agile Hunt 8)、`92:0`(Evasion Tactics 6) |
| 条件性未计入 | +8 All Attributes | `equipment.chest#affix#corrupted#0.0`「+8 to All Attributes with at least 7 Corrupted non-Idol Items equipped」(property 98 未映射)；冻结导出里非神像腐化装备 12 件（≥7，条件应满足）→ **Dex=236 候选** |

### 5.2 暴击池

玩家（bow attack 口径，公式见 §1）：`crit_chance=(0.05+Σadded)×(1+Σincreased)`、`crit_multi=2.0+Σadded`。

| 池 | 数值 | 组成 |
|---|---:|---|
| 玩家 increased crit chance（无 tag） | +155% | ring2 implicit 50%、idol2 9%、idol3 6%、idol7 14%、passive20 40%、passive100 36% |
| 玩家 increased crit chance（Bow tag） | +95% | idol0 45%、idol4 50% |
| 玩家 added crit chance | +2%（无 tag，ring2）+2%（Bow，passive20） | 合计 +4% |
| 玩家 added crit multi（无 tag） | +312% | 装备/神像/被动共 12 条 |
| 玩家 added crit multi（Bow tag） | +35% | Zeurial's Hunt implicit |
| **玩家 bow attack 暴击几率（候选）** | **31.5%** | (0.05+0.04)×(1+2.50)=0.315 |
| **玩家 crit multiplier（候选）** | **547%** | 2.0+3.47 |

Minion 直接池（不经 node 27）：gen327 基础 5%/200%；node 26 当前 1 点 → +3% base crit；increased minion crit +36%（passive100:1）；added minion crit +9%（ring2 4% + relic 3% + idol3 2%）；added minion crit multi +151%（5 个神像 19–26% + passive100 36%）。
→ 不含 node 27 转移的候选：**crit chance 23.12%、crit multi 351%**。

未映射的伤害相关暴击词条：`equipment.ring2#affix#corrupted#0.1`「1% Increased Critical Strike Chance per Dexterity」(property 98, tags 427)。若按 per-point 理解，Dex 228 → +228% increased crit chance（量级巨大，未确认）。

### 5.3 按 tag 分类的 increased damage 池

玩家侧（Minion tag 已排除；爆炸 tags=Bow+Fire）：

| tag 组 | 合计 | 来源示例 |
|---|---:|---|
| Bow | **+8%** | `equipment.chest#affix#normal#1.1`（Urzil's Pride） |
| DoT | +9% | weapon2 corrupted 7% + idol3 2% |
| Physical | +26% | Jungle Queen's Chaps unique_mod#2 |
| Spell | +72% | Julra's Stardial unique_mod#1 |
| Melee / Throwing | +30% / +30% | passive:49 Pursuit |
| 合计（全部玩家 damage 行） | +175% | — |

Minion 直接池（Ballista 自己作为 minion 可继承的行）：

| tag 组 | 合计 | 适用性 |
|---|---:|---|
| Minion | **+455%** | head 85% + hands 98% + ring1 116% + blessing10 100% + passive48 56% |
| Minion+Bow | **+87%** | Melvern's Writ unique_mod#1 |
| Minion+Physical | +26% | Jungle Queen's Chaps unique_mod#3（对 Fire 不适用） |
| **Fire+Bow 爆炸适用合计** | **+542%** | {Minion}+{Minion,Bow} |

> JSON `player_stats.player_increased_damage_pools` / `minion_direct_increased_damage` 逐条列出来源。

### 5.4 added flat damage 池（玩家侧，bow attack）

| tag 组 | 合计 | 组成 |
|---|---:|---|
| Bow | 47 | weapon1 implicit +42、passive20 +5 |
| Bow+Cold | 58 | weapon1 unique 32 + weapon2 affix 26 |
| Bow+Fire | **14** | Hydra Arc unique_mod#0（唯一 added fire） |
| Bow+Lightning | 43 | weapon1 unique 20 + weapon2 unique 23 |
| Bow+Physical | 1 | passive6 Swift Assassin |
| Lightning+Throwing | 14 | weapon2 unique_mod#1 |
| Melee+Physical / Physical+Throwing | 1 / 1 | passive6 |
| **Bow 合计** | **163** | — |

目前没有任何 Ballista 节点声明 minion 继承玩家 added flat damage，因此该池**仅作玩家侧参考**；node 22 “your damage modifiers” 是否包含 added damage 属未确认项。

### 5.5 more damage 倍率来源

| 来源 | 当前点 | 候选倍率 | 依据 |
|---|---:|---:|---|
| node 5 Armed Construction | 4 | **×(1+0.01×4×228)=×10.12** | property 0「More Damage per Dexterity +1%/点」，同节点多点线性相加（项目既有 more 池约定） |
| node 14 Practical Build | 1 | ×1.40 | 描述 “more damage (multiplicative)” |
| node 23 Heavy Bolts | 3 | ×1.36 | +12%/点，描述 “more damage” |
| node 25 Elixir of Construction | 3 | ×1.45 | +15%/点，药剂后 4s 内建造；100% 覆盖假设 |
| minion 100053 base stat | — | ×1.50（候选） | property 0 `moreValues=[0.5]` 的 actor base stat 读法 |
| weapon1 corrupted affix | — | ×1.04（未映射） | 「4% more Global Conditional Damage」property 117，条件语义 unknown |

node 5 的量级（×10.12）在 Dex=228 下是本构筑最大的单一乘区，也是 re-gamecode 必须确认“每点 more per Dex 是否按点数累加、是否作用于 minion”的关键项（见 §7）。

### 5.6 攻击速度

- 玩家侧：仅 `passive:6:1` Bow +3%（Swift Assassin）；被动 Melee/Throwing +3% 各自不适用。
- 装备未映射但明确伤害相关：`equipment.relic#affix#normal#0.1`「30% increased Ballista Attack Speed」(property 58, tags 379)。
- Ballista（minion）：minion base property 2 +20% increased；node 11 +6%；node 14 ×1.4 more；node 23 -15%（less/increased 待定）；node 12 -40%（less）；node 18 +1%/5 Dex → **+45.6%** 候选；ba1574 attributeScaling Dex→+1%/Dex → **+228%** 候选（是否作用于 minion 未确认）。
- entity_bbl7_57 attributeScaling Dex→+5% attack speed（只作用于 bolt，不进爆炸模型）。

### 5.7 Mana / Mana Regen / 药剂

| 项 | 值 | 来源 |
|---|---:|---|
| 职业 baseMana / manaPerLevel | 50 / 0.50506 | Rogue class（dataset） |
| base mana（engine 口径 `level-1`） | 100.00094 | 50+0.50506×99 |
| flat added | +20 | Julra's Stardial implicit |
| increased | +5% | Phantom Grip unique_mod#3 |
| **max mana（engine 口径，不含 Att）** | **126.000987** | (100.00094+20)×1.05 |
| Attunement +2 mana/点（参考模型） | +24 | Att=12 |
| **max mana（含 Att 候选）** | **151.200987** | engine 口径；(level 口径 151.73) |
| mana regen base | 8/s | class `manaRegen=8` |
| increased mana regen（已映射） | +170% | waist affix 100% + 58%、ring1 implicit 12% → 21.6/s |
| 未映射 regen 描述 | 候选 +137% | chest unique description「1% increased mana regeneration per 2% uncapped Lightning Resistance」；uncapped 闪电抗 274% → 候选 32.56/s |
| 药剂 | 药水每 1s 自动使用（Eternal Font unique_mod#0） | 支持 Elixir 100% 覆盖；另有 +13% 不消耗、Potions slots +4/+12、Mana Gained on Potion Use 35、+88 Ward on Potion Use |
| 抗性（本轮计算，供上述描述评估） | fire 59 / cold 40 / lightning **274** / physical 49 / void 0 / necrotic 98 / poison 33（uncapped %） | `compute_resistances` 语义 |

## 6. Ballista 存续时长（node 2/14/15）候选口径

| 参数 | 数值 |
|---|---|
| prefab 基础 duration | 10.0 s（`SummonEntityOnDeath.duration=10`） |
| node 2 Efficient Construction | -20%/点 × 2 点 = **-40%**（max 2） |
| node 14 Practical Build | -25% × 1 点 = **-25%**（max 1） |
| node 15 Sturdy Foundation | +15%/点 × 2 点 = **+30%**（max 3） |
| 候选 A（加算：同一 duration 池） | 10×(1+0.30-0.40-0.25) = **6.5 s** |
| 候选 B（乘算：逐节点乘法） | 10×0.8²×0.75×1.15² = **6.348 s** |

dataset 只给出 property 5093 的百分比数值，没有 operation（increased/reduced vs less/more）；描述里 node 14 的 “shorter duration” 未带 “(multiplicative with other modifiers)”，因此**候选 A（加算）更可能**，最终由 re-gamecode 结论锁定。

## 7. 未确认项（与 JSON `unknowns` 一一对应，16 条）

1. **ba1574 attributeScaling 是否作用于召唤物**：`{3(Dex): damage +4%/点, attack speed +1%/点, tags=Minion}`。若生效，Dex 228 → +912% increased damage、+228% increased attack speed（量级巨大，且与“minion 默认不继承属性”的用户规则冲突）。**必须由 re-gamecode 定**。
2. **minion 100053 base_stats 读法**：property 0 `moreValues=[0.5]`、property 2 `increasedValue=0.2` 是否为 +50% more damage/+20% increased attack speed（actor base stat 应用路径）。
3. **skill node stat 的 more/increased 语义**：dataset 不存 operation；node 5/14/23 描述含 “more damage (multiplicative)”。
4. **存续时长加算 vs 乘算**：候选 6.5s vs 6.348s。
5. **node 22 转移范围**：哪些“玩家伤害修正”被转移（只 increased？added？more？tag 过滤？），比例 75% 如何复合。候选：tag 过滤 8%×0.75=6%；全部玩家 damage 行 175%×0.75=131.25%。
6. **node 27 复合方式**：玩家 31.5% crit chance / 547% crit multi 如何并入 minion 值（是否含基础值、是否 cap）。
7. **未映射 property 98/117/58**：`+8 All Attributes`（条件已满足候选）、`1% increased crit per Dex`、`4% more Global Conditional Damage`、`30% increased Ballista Attack Speed`。
8. **trap arming time / 基础最大陷阱数 / MineTrigger triggerType 0 语义**：dataset 与 letools 均无。
9. **trap 数量复合**：Blast Rain 3 projectile × Clustered +2 × Trapas 多转换变体如何合成每次施放的陷阱数。
10. **Trap Sprinkler 30% + Automated Bombardment 70% 是否加算**（候选 100%）。
11. **node 30 的 50% Ballista mana 以基础 44 还是 node2 减免后的 22 计算**（22 vs 11）。
12. **mana efficiency/mana cost 减免加算还是乘算**（22.2 vs 23.62）。
13. **玩家 added flat damage 是否被任何节点转移**（当前 14 节点无声明）。
14. **node 26 +3% base crit 是否作用于 gen327 爆炸**（描述为 Ballista 全局 base crit）。
15. **Elixir 覆盖**：模型按 100%；Eternal Font 提供每秒药水支撑，但建造时机由玩家操作决定，属场景假设。
16. **property id 的上下文复用**：dataset `stats` 表不能直接解释 node stat（property 8=Mana vs 节点 Mana Cost；49=Potion Slots vs Potion Drop Chance；5xxx/1xxxx 无 dataset 行）。

## 8. 与既有证据的关系

- 本包**只新增** `docs/evidence/ballista/ballista-build-values-20261113.json` 与本文件；`ballista-catalog-20261112.json`、`ballista-runtime-and-node-values-20261112.json`、`explosion-and-trap-components-20261112.json`、`ballista-mechanism-review-20261112.zh-CN.md` 未改动。
- 既有三份 JSON 是**数据摘录/身份核对**；本包在其基础上补齐：JSON 结构化数值包、玩家侧池（本轮计算）、minion base stats 解释、ex4tp 召唤链事件参数、时长候选、16 条 unknowns。
- 本包不包含 DPS 结果本身：DPS 由 Lead 在定义模板/计算计划中按上述参数与 re-gamecode 的规则结论计算。未确认项不得当作已确认；候选值均标注 `candidate`/`unresolved`。
