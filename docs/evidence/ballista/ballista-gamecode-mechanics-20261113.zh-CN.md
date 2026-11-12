# Ballista 游戏代码机制逆向（beta_5 存档 / 本机安装版 IL）

任务：为统一 DPS 管线提供 Ballista（ba1574）的 IL 级精确机制语义。本文只记录**从本机 IL/数据中能核实的事实**，并显式标注推断与未知；不把推断写成结论。

- 本机游戏版本：`<game-install>`（数据侧 `letools` 冻结副本为 `version150`；本存档 `calculation-input.json` 记为 game_version=1.5.2，dataset=`phase1-dataset-003`）。**数据与 IL 可能来自不同的补丁位（1.5.0 数据 vs 本机安装版 IL），凡是依赖数据表数值的条目都单独标注；机制语义以本机 IL 为准。**
- 逆向资产：
  - `<author-archive>/cpp2il_cs/DiffableCs/LE/`：每类型一个 `.cs`，字段名 + 字段偏移注释 + `[Calls]`。
  - `<author-archive>/cpp2il_isil/IsilDump/LE/`：Cpp2IL ISIL（未命名调用；字段偏移以十进制打印）。
  - `<author-archive>/cpp2il_isil_ca/IsilDump/LE/`：Cpp2IL call-analyzer ISIL（调用已命名，**首选**）。
  - `<author-archive>/cpp2il_il2/LE.dll`：IL-recovered 程序集（方法体为 `throw null`，类型/字段/签名可用）。
  - `<author-archive>/re-gamecode/`：本轮临时脚本与 letools 解析产物（`letools/*.json`、`pe_sections.json`、`read_consts*.py`）。
  - 静态 float 常量：从 `GameAssembly.dll` 的 `.rdata` 直接读取（见“常量表”）。
- 证据定位约定：`[文件:ISIL 行号]` 指 `<author-archive>/cpp2il_isil_ca/IsilDump/LE/<类型>.txt` 的 ISIL 行号（若用未命名版本则注明 `isil`）；字段偏移一律十六进制（ISIL 中打印为十进制）。

---

## 0. 对统一 DPS 管线最重要的结论（先看这里）

1. **DPS 输出单元**：Armed Construction(节点 5) → 每个 Ballista 死亡时生成 1 次 `ability_gen327`（内部名 `ballistaExplosion`，AbilityID 626）爆炸。爆炸基础：50 Fire、added damage effectiveness 250%、基础暴击 5%/200%。[ability_ability_gen327.json]
2. **“死亡”包含到期与上限替换**：Ballista 到期走 `UnsummonAfterDelay` → `Summoned.unsummon` → `Dying.die`；上限替换走 `Summoned.unsummon` + `SelfDestroyer.die`。两条都触发 death 事件，因此 `CreateAbilityObjectOnDeath`（挂在 Ballista 实体上，adapt 时设置 ability=626）都会开一次爆炸。[SummonEntityOnDeath:ISIL 6900–6961][UnsummonAfterDelay:ISIL 034–039][Summoned:ISIL 165]
3. **每次死亡恰好 1 次爆炸**（1 个 `CreateAbilityObjectOnDeath`、`additionalCasts` 默认 0）；不存在“到期爆炸 + 替换爆炸重复触发同一次死亡”。替换发生时旧 Ballista 死亡→爆炸，新 Ballista 正常出生。
4. **爆炸的 per-Dex more**：`ballistaExplosionMoreDamage = Dex × (0.01 × 节点5点数)`（节点5 4/4 → 0.04×Dex）；作为 **more damage** Stat 作用在爆炸那次命中。`Dex` 是**玩家**的 Dexterity（`BallistaMutator.GetStatsFromPlayer` 同一来源的 Stats；见 §4.2 的取属性调用）。[BallistaMutator:ISIL 420–474][BallistaExplosionMutator:ISIL 029–058]
5. **频率链（本构筑）**：Siege Engineering 2/2 = 每次 **trap 引爆**独立 20% 概率召唤 1 个 Ballista（`chanceForBallistaOnDetonation=0.10/点`），召唤出的 Ballista 走**同一套** `SummonEntityOnDeath`（上限 2、duration=6.5s、statList 快照、玩家 mana 扣 50%）。爆炸事件速率 ≈ Ballista 召唤速率（每个召唤最终对应一次死亡）。**详细公式与未知见 §8。**
6. **Elixir of Construction 3/3**：`moreDamageAfterPotion = 0.15×点数 = +45%`，在**召唤那一刻**若 `potionUsedRecently` 为真，则以 `Buff(Stat(more damage))` 形式挂到 Ballista 上、持续 20s；否则该次召唤没有此增益（不是动态判定）。[BallistaMutator:ISIL 588–591][BallistaAdapter:ISIL 434–467]

---

## 1. 目录事实（ability / minion / 组件）

| 实体 | 值 | 证据 |
|---|---|---|
| ba1574 `summonBallista` | AbilityID 379；manaCost 44；useDuration 0.6s；useDelay 0.15s；speedScaler=SP.None(54)；speedMultiplier 1.0；tags 0；fakeTags 8192(Minion)；attributeScaling Dex(`3`)→SP.Damage +4%/点(AT=8192 Minion)、SP.AttackSpeed +1%/点(AT=8192)；levelScaling → SP.Health +75/技能等级(AT=8192) | `ability_ba1574.json`；[BallistaMutator.txt:ISIL 036 `SetInitialAbility(379)`] |
| ba1574 组件 | `SummonEntityOnDeath`：entity=100053、numberToSummon=1、limit=2、limitNumber=true、limitDuration=true、duration=10、statList=[]、summonIsASummoner=true、followsCreator=false | `ballista-runtime-and-node-values-20261112.json#skill_components` |
| entity 100053 `rogue_ballista` | 攻击技能列表 [entity_bbl7_57]；`castSpeedManager.overrides=[{ability:entity_fm8_2, useDelay:1.1, useDuration:2, as:0.55000126}]`（**与 bolt 无关**，bolt 不在 override 列表内）；abilityMutators 有 `increasedRadius:0.2` 等 | `ballista-runtime-and-node-values-20261112.json#minions` |
| entity_bbl7_57 `ballistaBolt` | AbilityID 380；manaCost 0；**useDuration=2.3s**；useDelay=1.6s；speedMultiplier=1.5；speedScaler=SP.AttackSpeed(2)；tags 2049(Bow+Physical)；攻击组件 40 Physical、effectiveness 200%、暴击 5%/200%、damageTags 2048(Bow) | `ability_entity_bbl7_57.json` |
| ability_gen327 `ballistaExplosion` | AbilityID 626；manaCost 0；useDuration 1.0s；useDelay 0.5s；speedMultiplier 1；tags 2056 = Bow(2048)+Fire(8)；命中组件：base damage Fire 50、addedDamageScaling=2.5、crit 5%/200%、damageTags 2048(Bow)、hit=true | `ability_ability_gen327.json` |
| ability_gen237 `smallExplosion` | 另一同名爆炸：40 Fire / 200%，damageTags 1024(Throwing)。**不是** Ballista 爆炸，禁止混用 | `explosion-and-trap-components-20261112.json` |
| ability_gen517 `ballistaShot` / ba1574_bblsc7_1 `ballistaShotCommand` | 928 / 929；Ballista 的指挥/齐射技能（本 DPS 口径不用） | `AbilityID.cs`；`BallistaMutator.Awake` 缓存 929 |
| ex4tp `explosiveTrap` | AbilityID 734；manaCost 8；**useDuration 1.35s、useDelay 0.4s、speedMultiplier 1.8、speedScaler=SP.AttackSpeed**；comboTimeLimit 2.9；prefab: DestroyAfterDuration 0.3 + CreateAbilityObjectOnDeath → `ex4tp_fi9_1`(explosiveTrapOnGround 749) | `ability_ex4tp.json` |
| ex4tp_fi9_1 / 750 | `explosiveTrapOnGround` 749（落地陷阱本体，mana 3）；`explosiveTrapDamage` 750（引爆伤害） | `dump_trap.js` 输出 |

---

## 2. 节点属性 → 字段映射（问题 A / task-1 Q3）

### 2.1 机制总入口

`BallistaTree.updateMutator(LocalTreeData, TreeData)`（生成代码）：遍历 `TreeData.nodes`（`LocalTreeData.NodeData{id@0x10, pointsAllocated@0x11}`），对 `pointsAllocated>0` 的节点取 `GlobalTreeData.getTreeData(treeID).getNode(id)`，**对节点内部名字符串做 switch**（字符串 hash + 逐字比较），按预烘每点常量累加到组件字段/Stat 列表，最后一次性写回 `BallistaMutator`。**节点 stat 的 property/值并不在运行时读取；每点数值硬编码在生成代码里（常量见 §2.4 表）。** [BallistaTree.txt:ISIL 001–1030，字符串 switch 见 ISIL 257–891，最终写回见 ISIL 929–985]

写回目标（`rbx = BallistaMutator`，`rdi = 另一个被 GetComponentInChildren 找到的组件`，本构筑涉及的 14 个节点只写 `rbx`）：

```
[rbx+0x120] unconditionalTempStats = 新建空 List<Stat>（本树从不填充）
[rbx+0x128] statList             = 收集到的 List<Stat>（召唤时复制给 SummonEntityOnDeath.statList）
[rbx+0xC4]  increasedManaCost    = stack0x64
[rbx+0x14C] addedDuration        = 0
[rbx+0x150] increasedDuration    = stack0x60
[rbx+0x158] doubleShotChance     = stack0x94
[rbx+0x160] attackSpeedOnHit     = stack0xAC
[rbx+0x164] maxPotionsDropped    = stack0x74
[rbx+0x168] moreDamageAfterPotion= stack0xA4
[rbx+0x16C] gainsFrenzyNearPlayer= stack0x1E0(flag)
[rbx+0x170] addedFireDamage      = stack0x68
[rbx+0x174] additionalProjectiles = r13
[rbx+0x178] ballistaExplodesOnEnd= stack0x5A(flag)
[rbx+0x17C] ballistaExplosionMoreDamagePerDexterity = stack0x6C
[rbx+0x180] ballistaExplosionIncreasedAreaPerDexterity = stack0x70
[rbx+0x184] arrowsFireExplodeOnEnd= stack0x58(flag)
[rbx+0x185] explodeConvertedToCold= stack0x59(flag)
[rbx+0x186] tripwire             = stack0x5B(flag)
[rbx+0x1A0] hasShield            = stack0x1F8(flag)
[rbx+0x1A4] shieldHealth         = r15(0)
[rbx+0x1A8] increasedShieldWidth = stack0x8C
[rbx+0x1B1] harpoon              = stack0x5C(flag)
[rbx+0x138] playerDamageModifierEffectivenessRatio = xmm15
[rbx+0x13C] playerCritChanceEffectivenessRatio     = stack0x7C
[rbx+0x140] playerCritMultiEffectivenessRatio      = stack0x78
[rbx+0x144] playerAilmentChanceEffectivenessRatio  = stack0x80
[rbx+0x148] targetsToPierce      = r12
[rbx+0x190] placementSpeedPerDexterity  = stack0x84
[rbx+0x194] attackSpeedPer5Dexterity    = stack0x88
```

Stat 构造使用 `Stats.Stat(SP property, AT tags, float addedValue, float increasedValue, float moreValue, byte specialTag, int extraTag)`（7 参构造）；经 `Stats.MoreStat/IncreasedStat/AddedStat` 或直接 `new Stat(...)`。ISIL 参数槽位实测映射：**`stack+0x20` = increasedValue，`stack+0x28` = moreValue，`stack+0x30` = specialTag，`stack+0x38` = extraTag；addedValue 在 xmm3**（`Stats_NestedType_Stat.txt` 构造器反汇编：`[rdi+0x1C]←xmm3`、`[rdi+0x20]←[rsp+0x80]`、`moreValues.Add←[rsp+0x88]`）。该映射已用 `Stats.MoreStat` 与 `BallistaExplosionMutator.getTempStats` 双向校验。

### 2.2 本构筑 14 个 Ballista 节点逐条映射（全部来自 BallistaTree.updateMutator）

> 每点数值 = 从 `GameAssembly.dll` `.rdata` 读出的硬编码 float；“op” 指进入 `Stat` 的哪个槽或直接改字段。数据侧每点数值（letools）与常量完全一致，等于双重确认。

| 节点 | 内部名(switch case) | 作用目标 | 每点值 | op | 点数 → 结果 |
|---|---|---|---|---|---|
| 2 Efficient Construction 2/2 | Ballista Less Cost And Duration | `increasedManaCost`(0xC4)；`increasedDuration`(0x150) | -0.25；-0.20 | 直接字段 = -0.25×pts；-0.20×pts | mana -50%；duration -40% |
| 5 Armed Construction 4/4 | Ballista Explodes On Death | `ballistaExplodesOnEnd`=true；`ballistaExplosionMoreDamagePerDexterity`；`ballistaExplosionIncreasedAreaPerDexterity` | flag；+0.01；+0.01 | 直接字段 | 爆炸开；more=0.04×Dex；area=0.04×Dex |
| 10 Flaming Shots 4/4 | Ballista Fire Damage And Ignite | `addedFireDamage`(0x170)；Stat(SP=1 AilmentChance) | +3.0；+0.10 | 字段 += 3×pts；Stat increased=0.10×pts | +12 fire added（给 bolt/shot，不是爆炸）；+40% ignite chance stat |
| 11 Rapid Fire 1/4 | Ballista Increased Attack Speed | Stat(SP=2 AttackSpeed) | +0.06 | Stat **increased** = 0.06×pts | +6% increased AS（给 minion） |
| 12 Explosive Delivery 1/1 | Ballista Arrows Explode On Expiration | `arrowsFireExplodeOnEnd`=true；Stat(SP=2) | flag；-0.40 | Stat **more** = -0.40×pts | bolt 到期爆炸开；-40% more AS |
| 14 Practical Build 1/1 | Ballista Damage And Attack Speed Less Duration | `increasedDuration`-=0.25×pts；Stat(SP=0 Damage) more=0.40×pts；Stat(SP=2) more=0.40×pts | -0.25；+0.40；+0.40 | 字段/Stat more | duration -25%；+40% **more** damage；+40% **more** AS |
| 15 Sturdy Foundation 2/3 | Ballista More Health And Duration | Stat(SP=7 Health) more=0.25×pts；`increasedDuration`+=0.15×pts | +0.25；+0.15 | Stat more/字段 | +50% more health；duration +30% |
| 18 Agile Engineering 1/1 | Ballista Placement Speed | `placementSpeedPerDexterity`+=0.01×pts；`attackSpeedPer5Dexterity`+=0.01×pts | +0.01；+0.01 | 直接字段 | 放置速度 = Dex×1%；minion AS = (Dex/5)×1% increased（`BallistaMutator.Mutate` 内 `Dex/5 × 字段` 生成 `Stats.IncreasedStat(SP=2)`）[BallistaMutator:ISIL 117–171] |
| 22 Shared Enhancements 3/3 | Ballista Modifiers Apply To Ballista | `playerDamageModifierEffectivenessRatio`(0x138) | +0.25 | 字段 += 0.25×pts | 0.75（复制玩家 Damage 池的 75%，见 §5） |
| 23 Heavy Bolts 3/3 | Ballista Slower And More Damage | Stat(SP=2) more=-0.05×pts；Stat(SP=0) more=+0.12×pts | -0.05；+0.12 | Stat more | -15% more AS；+36% **more** damage |
| 25 Elixir of Construction 3/3 | Ballista More Damage After Potion | `moreDamageAfterPotion`(0x168) | +0.15 | 字段 += 0.15×pts（召唤时快照为 Buff） | +45% more（详见 §9） |
| 26 Sharpened Tips 1/2 | Ballista Added Crit Chance | Stat(SP=4 CriticalChance) | +0.03 | Stat **added** = 0.03×pts | +3% 基础暴击（added） |
| 27 Perfect Aim 4/4 | Ballista Crit Modifiers Applies To Ballista | `playerCritChanceEffectivenessRatio`(0x13C)；`playerCritMultiEffectivenessRatio`(0x140) | +0.25；+0.25 | 字段 += 0.25×pts | 各 1.00（复制玩家暴击率/暴伤池 100%） |
| 31 Care Package 1/1 | Ballista Potion Chance | `maxPotionsDropped`(0x164) | +1（int） | 字段 += pts | +1 药水掉落上限 |

校验样例（ISIL 常量与数据一致）：
- 节点 2：`0x18457CC00 = 0.20`（duration），`xmm8 = [0x18457CF18] = 0.25`（mana cost）。[BallistaTree:ISIL 749–768]
- 节点 5：`0x18457CDAC = 0.01`（两个 per-Dex 字段）。[BallistaTree:ISIL 858–880]
- 节点 10：`0x18457CC3C = 3.0`、`0x18457CDB8 = 0.10`。[BallistaTree:ISIL 491–518]
- 节点 11：`0x184738630 = 0.06`；节点 14：`0x18457D0D0 = 0.40` + xmm8=0.25；节点 15：`0x1847385A0 = 0.15` + xmm8=0.25；节点 22/27：xmm8=0.25；节点 23：`0x18473872C = 0.12`、`0x184738744 = -0.05`；节点 26：`0x1845832F8 = 0.03`；节点 31：`points` 直接加 int。
- 常量表（`.rdata` 直读）：0x18457CF18=0.25、0x18457CDAC=0.01、0x18457CDB8=0.10、0x18457CC3C=3.0、0x18457CC00=0.20、0x18457D0D0=0.40、0x184738630=0.06、0x18473872C=0.12、0x184738744=-0.05、0x1845832F8=0.03、0x1847385A0=0.15、0x184738F7C=-0.40、0x18457CC18=1.0、0x18457D128=1.25、0x18457CC54=20.0。

### 2.3 「属性池 / 数值转移」字段与节点关系

- `playerDamageModifierEffectivenessRatio`(0x138) ← 节点 22（25%/点）
- `playerCritChanceEffectivenessRatio`(0x13C)、`playerCritMultiEffectivenessRatio`(0x140) ← 节点 27（各 25%/点）
- `playerAilmentChanceEffectivenessRatio`(0x144) ← 本树中没有该 case 落在本构筑（对应节点为“Ballista Ailment Chance Applies To Ballista”，本次未点；每点同样 0.25）
- 其它“ballista 专属”的 manager 字段（`increasedBallistaAttackSpeed` 0x1F78、`addedBowDamageForBallista` 0x1F80、`addedBowPhysicalDamageForBallista` 0x1F7C、`addedMaximumBallistae` 0x1F74、`ballistaMaxActive` 0x1F8C、`ballistaAddedSpawnPerCast` 0x1F88、`ballistaIncreasedDuration` 0x1FA4、`addedCritChanceForFalconAndBallistae` 0x1FCC 等）**不由 Ballista 技能树写入**；来自被动/装备/其它技能树，经 `AbilityStatsMutatorManager` 在 `BallistaMutator.Mutate` 中读取 [BallistaMutator:ISIL 222–540]。本构筑这些值为 0（除非 calculation-input 的 mods 另行提供）。

---

## 3. 召唤生命周期（问题 8 / Q8）

`BallistaMutator.Mutate` 组装召唤参数（[BallistaMutator:ISIL 040–181、420–489]）：

1. `limitNumber=true`；`limit = 2 (BALLISTA_BASE_SUMMON_LIMIT) + AbilityStatsMutatorManager.addedMaximumBallistae`，若 `ballistaMaxActive>0` 再取 min；本构筑 = **2**。
2. `numberToSummon = 1 (BALLISTA_BASE_SUMMON_COUNT)`；若 `ballistaAddedSpawnPerCast>0` 则 `+1`；再被 limit 截断；本构筑 = 1。
3. `statList = BallistaMutator.statList`（节点生成的 Stat，§2；含 `Stats.IncreasedStat` 与 `MoreStat` 等）
   + `GetStatsFromPlayer()` 返回值（§5，在 ISIL 173–181 追加）
   + `attackSpeedPer5Dexterity` 换算出的 `IncreasedStat(SP=AttackSpeed)`（ISIL 117–171）
   + manager 的 ballista 相关 Stat（ISIL 222–419）。
4. `duration = (SummonEntityOnDeath.duration, 目录值10) × (1 + BallistaMutator.increasedDuration)`；
   代码：`xmm8 = 1.0 + increasedDuration(0x150)`；`xmm8 *= (0xFC + addedDuration(0x14C))`；写回 `[SummonEntityOnDeath+0xFC]`；同时写 `BallistaAdapter.ballistaLifetime`。**additive increased 再乘基础 duration，不是多次乘算**。[ISIL 420–426]
   - 本构筑：`increasedDuration = +0.30(节点15) −0.40(节点2) −0.25(节点14) = −0.35` → **duration = 10 × 0.65 = 6.5s**。
5. 其它写入：`ballistaExplodesOnEnd`、爆炸 per-Dex 两个字段、`arrowsFireExplodeOnEnd`、`explodeConvertedToCold`、`addedFireDamage`、`additionalProjectiles`、`maxPotionsDropped`，以及**仅当 `potionUsedRecently(0x198)` 为 true 时** `moreDamageAfterPotion`（§9）。
6. Adapter 字段逐一复制到实体组件（见 §4、§5）。

### 3.1 超上限 / 到期

- 超上限：`SummonEntityOnDeath.Summon` 在达到 `limit` 时调用 `getLowestPriority(existingSummons,…)` 选出最旧/最低优先级，随后 `Summoned.unsummon(...)` + `SelfDestroyer.die(...)`（`SummonEntityOnDeath:ISIL 6900–6961`）。**替换 = 旧单位死亡，不是简单 Destroy。**
- 到期：`SummonEntityOnDeath.Summon` 在 ISIL 8508–8521 处，若 `limitDuration` 为真则 `UnsummonAfterDelay.Add(go, duration, unsummonWithNormalDestructionDelay)`；`UnsummonAfterDelay.OnUpdateTick` 在计时结束时调用 `Summoned.unsummon(normalDestructionDelay, false, false)`，而 `Summoned.unsummon` 内部会调用 `Dying.die`（ISIL 165）。**到期 = 死亡事件。**
- 死亡 → `CreateAbilityObjectOnDeath`（Ballista 实体上的组件，adapt 时 `abilityToInstantiate=626`）创建爆炸（§4）。

---

## 4. 爆炸（ability_gen327 / 626）伤害构成（问题 1 / Q1）

### 4.1 爆炸如何被挂上、何时触发

`BallistaAdapter.adapt`：
- 缓存 `Ability.getAbility(626)` → `adapter.ballistaExplosion(0xD0)`；`GetAbility(513)`；`GetAbility(263)`；`GetAbility(377)`。[BallistaAdapter:ISIL 104–139]
- 当 `adapter.ballistaExplodesOnEnd(0x58) != 0`：取 Ballista 实体上的 `CreateAbilityObjectOnDeath` 组件（type 0x1853955E8），设 `abilityToInstantiateRef`（+0x28）= 626、`+0x60 = 1`（启用）。[BallistaAdapter:ISIL 485–497]
- 同函数把爆炸 mutator 字段写入 `BallistaExplosionMutator`：`explodeConvertedToCold(+0x130)`、`ballistaExplosionMoreDamage(+0x134)`、`ballistaExplosionIncreasedArea(+0x138)`、`creator(+0x148)`。[BallistaAdapter:ISIL 235–251]
- 触发时机 = **death 事件**（`CreateAbilityObjectOnDeath.CreateAbilityObject(Dying)`），到期与替换都会死亡（§3.1）。
- **未在 IL 中找到显式写 `isBallistaExplosion(0x13C)=true` 的代码** → 该 bool 极可能来自 Ballista 实体 prefab 上该组件的序列化默认值（组件与 mutator 都在 ballista 实体 prefab 上）。见 unknown U-1。

### 4.2 伤害数值

`BallistaExplosionMutator.Mutate`（[BallistaExplosionMutator:ISIL 001–275]）：
- 由 `AbilityMutator.GetVisualOverride`、AT 从 `getTags()`、`Stats.GetIncreasedAreaForAreaSkill(AT)` 取玩家/施法者 stats 的面积加成。
- 面积：`areaIncrease = statsArea + ballistaExplosionIncreasedArea`；`radiusMult = GetIncreasedRadiusAfterAdditiveAreaIncrease(0, areaIncrease)`；若 `isBallistaExplosion`：`radiusMult = (radiusMult + 1) × 1.25 − 1`（常量 0x18457D128=1.25，等价“+25% more radius”内在加成，与 `inherentMoreRadiusFromBeingBallistaExplosion=0.25` 一致）；然后 `SphereCollider.radius *= (1+radiusMult)`、`CreateVfxOnDeath.increasedRadius` 同样缩放。[ISIL 041–188]
- 伤害：`BallistaExplosionMutator.getTempStats`：把 `unconditionalTempStats` 并入返回列表；**若 `isBallistaExplosion && ballistaExplosionMoreDamage > 0`**，追加 `new Stat(SP=Damage, more = ballistaExplosionMoreDamage)`。[ISIL 018–058]
  - 即：爆炸那次命中吃 **`1 + (0.04×Dex)` 的 more damage**（4/4 节点 5）。
  - 该 more 是“爆炸专属”统计，不会作用到 bolt/其它攻击。
- 基础命中：Fire 50、`addedDamageScaling=2.5`（250% added damage effectiveness，作用于 added damage 池，而不是“2.5 倍总伤害”）、暴击 5%/200%、`damageTags=2048(Bow)`；ability tags=2056(Bow+Fire)。[ability_ability_gen327.json]
- 冰转换（节点 13，未点）：`DamageStatsHolder.convertAllDamageOfType` + 把某字段设 80；不适用本构筑。

### 4.3 爆炸是否吃“added bow damage”

- 玩家身上的 added bow damage **不会**通过 Shared Enhancements 转移：`GetValueMultiplier` 只对 `SP.Damage(0)/CritChance(4)/CritMulti(5)/AilmentChance(1,42,43)` 有比例；flat added 的 stat 属于 addedValue 池，`GetStatsFromPlayer` 只复制这几类 SP 的 Stat 并把 added/increased/more **整体乘以比例**，但 SP=0 的 addedValue 是否会进入爆炸的 added 池仍取决于标签匹配（爆炸 tags={Bow,Fire}，无 Minion 标签）。
- manager 的 `addedBowDamageForBallista(0x1F80)` / `addedBowPhysicalDamageForBallista(0x1F7C)` 会作为 `Stat(SP=Damage, AT=2049 Bow+Physical / AT=?)` 加入 minion statList [BallistaMutator:ISIL 258–287]；本构筑这些 manager 值为 0，除非装备/被动提供。
- `addedDamageScaling=2.5` 只放大“added damage”池，且必须标签匹配（Bow 相关）。**结论：本构筑爆炸的 added 池来自 minion 自身 stats（节点/manager），不来自玩家武器 flat added；需要数据侧确认玩家装备的 flat bow damage 是否存在 ballista 转移通道（当前 IL 未见）。** → unknown U-2。

### 4.4 暴击构成

- 爆炸基础 `critChance=0.05`，`critMultiplier=2`（=200% 基础暴伤）[ability_gen327]。
- Sharpened Tips(节点26 1/2)：`Stat(SP=CriticalChance, added=+0.03)` 进入 minion stats → **+3% 基础暴击**（additive 到暴击率）。
- Perfect Aim(节点27 4/4)：把玩家暴击率/暴伤 Stat 复制 `×1.00` 加到 minion stats（`new Stat(stat, multiplier)`，保留原 SP/AT/数值槽）→ 与基础 5% 相加（暴击率）／与基础 200% 相加（暴伤，即 +玩家暴伤%×1.0）。[BallistaMutator:ISIL 3957–4284]
- 具体相加顺序：暴击率 = (5% + 3% + 转移的玩家暴击率 Stat 汇总) ；暴伤 = 200% + 转移的玩家暴伤 Stat 汇总。由于转移 Stat 保留 added/increased/more 的原始槽位，最终仍按 Stats 通用聚合规则计算。

---

## 5. Shared Enhancements / Perfect Aim 转移语义（问题 4、5 / Q4、Q5）

1. `GetValueMultiplier(SP)`（[BallistaMutator:ISIL 3897–3956]）：
   - `SP=0 Damage` → `playerDamageModifierEffectivenessRatio`
   - `SP=4 CriticalChance` → `playerCritChanceEffectivenessRatio`
   - `SP=5 CriticalMultiplier` → `playerCritMultiEffectivenessRatio`
   - `SP=1/42/43`（AilmentChance / IncreasedAilmentDuration / IncreasedAilmentEffect）→ `playerAilmentChanceEffectivenessRatio`
   - 其它 → 0；任一比率为 **≤0 时该 SP 返回 0**
2. `GetStatsFromPlayer()`（[BallistaMutator:ISIL 3957–4284]）：
   - 取 `stats.???`（玩家全部 Stat 列表，字段 [stats+0x88]）枚举；
   - 对每条 Stat 读 `property`（`Stat+0x10`），过滤：`ratio(prop)>0` 且 property ∈ {0,4,5,1,42,43}；
   - `multiplier = GetValueMultiplier(prop)`；
   - `new Stat(sourceStat, multiplier)`（`Stat(Stat, float)` → 内部 `CopyFrom` 把 addedValue/increasedValue/moreValues **逐项乘以 multiplier**，保留 AT tags/specialTag/extraTag）；加入返回列表。
3. 该列表在 `BallistaMutator.Mutate` ISIL 173–181 处 **`AddRange` 到 `SummonEntityOnDeath.statList`** → **召唤时快照**。之后玩家属性变化不会回溯已存在 Ballista，下一次召唤才更新。
4. 因此：
   - Shared Enhancements 3/3：玩家 Damage 池的 75% 复制到 minion（operation 与原 Stat 槽一致：原 increased 仍按增加、原 more 仍按 more、原 added 仍按 added；只是数值 ×0.75）。
   - Perfect Aim 4/4：玩家 CriticalChance/CriticalMultiplier Stat 的 100% 复制到 minion。
   - minion 自身属性（节点生成的 Stat + 基础 stats）与转移 Stat 在 `Stats` 内按 SP/AT 正常汇总合并。
5. **不是“百分比乘到 minion 总伤害”**，而是按 Stat 粒度的逐项缩放复制；标签过滤只发生在“属性种类”（SP）层，不做 AT 标签过滤（复制保留原标签）。

---

## 6. 属性缩放（问题 6 / Q6）

- ba1574 的 `attributeScaling`：属性键 `"3"`（Dexterity，见下）→ `{property:0(SP.Damage), tags:8192(Minion), increasedValue:0.04/点}`、`{property:2(SP.AttackSpeed), tags:8192, increasedValue:0.01/点}`。
  - 该缩放属于**召唤技能引导的 minion 属性**：`Ability.AttributeScaling` 在召唤时转换成 minion 的 Stat（`SummonEntityOnDeath.Summon` 内把 `[creationReferences+0x96]` 的 attributeScaling 列表转成 `Ability.AttributeScaling` 对象挂到 `Summoned`，见 `SummonEntityOnDeath:ISIL 2257–2315`）。即：**玩家 Dex 量的 4%/点 作为 minion 的 increased damage（Minion 标签），1%/点作为 minion increased attack speed**。
- 玩家 Dex 的读取点：`BallistaMutator.Mutate` 中通过 `stats` 的虚方法按属性索引 3 取值（同一处也用于节点 5 per-Dex 与节点 18 per-5-Dex）。[BallistaMutator:ISIL 132–149、443–447]
- 与节点 5 per-Dex more、节点 18 per-5-Dex AS 的关系：
  - 节点 5：额外单独的 more 乘区（只作用于爆炸），不是 4% increased 的一部分 → 两者相乘。
  - 节点 18：`(Dex/5) × 0.01` 的 increased attack speed Stat → 与 attributeScaling 的 1%/点 AS 相加（同一 SP=2 increased 池）。
- **快照语义**：attributeScaling 的 minion Stat 在**召唤时**按当时玩家 Dex 计算并加给该 minion；节点 5/18 的 per-Dex 值也在召唤时计算（`BallistaMutator.Mutate` 是召唤流程）→ 已存在 Ballista 不随玩家 Dex 变化。

---

## 7. minion 100053 基础 stats（问题 7 / Q7）

目录 `stats.stats`：`{property:0, increasedValue:0, moreValues:[0.5]}`、`{property:2, increasedValue:0.2}`。
→ minion 自带 **+50% more damage（SP=Damage, more=0.5）** 与 **+20% increased attack speed（SP=AttackSpeed）**，在 `SummonEntityOnDeath.Summon`/`SetStats` 阶段并入 minion Stats，与其他所有 Stat 按 SP/AT 汇总。该 50% more 是爆炸与 bolt 共享的乘区。

---

## 8. Siege Engineering 触发链与频率代数（问题 E、F / Q11）

### 8.1 已核实的精确链路

1. 玩家技能树节点 30 Siege Engineering（2/2）：`ExplosiveTrapTree.updateMutator` case **“Explosive Trap Drops Ballistas”**：`chanceForBallistaOnDetonation += points × 0.10`（xmm15，常量 `0x18457CDB8=0.10`）→ 写入 `ExplosiveTrapMutator.chanceForBallistaOnDetonation(0x19C)`。**2/2 = 20%**。[ExplosiveTrapTree:ISIL 294–304；最终写回 ISIL 1059 `[rbx+0x19C]=xmm15`]
2. `ExplosiveTrapMutator.Mutate` 把玩家层概率复制到地面陷阱组件 `ExplosiveTrapOnGroundMutator`：`[rbx+0x13C]=[rdi+0x198]`、`[rbx+0x140(0x140=chanceForBallistaOnDetonation)]=[rdi+0x19C]`、`[rbx+0x144]=[rdi+0x1A0]`。[ExplosiveTrapMutator:ISIL 7332–7338]（`rdi`=玩家 ExplosiveTrapMutator，`rbx`=陷阱的 ExplosiveTrapOnGroundMutator）
3. 引爆时 `ExplosiveTrapOnGroundMutator.Mutate` 再把 → `ExplosiveTrapDamageMutator`：`[rax+0x134]=[rbx+0x140]` 等。[ExplosiveTrapOnGroundMutator:ISIL 833–836]
4. `ExplosiveTrapDamageMutator.Mutate`（每次陷阱引爆生成伤害 ability object 时执行）在 ISIL 676–740：
   - `if (Rng.Roll(chanceForBallistaOnDetonation(0x134)))`（**每个引爆的陷阱独立判定一次**）；
   - 命中后：`GetComponent<CreateAbilityObjectOnDeath>(trapGO)`（type 0x185361690），设：
     - `abilityToInstantiate = [ExplosiveTrapDamageMutator.ballista(0x1C0)]`（`Awake` 中 `Ability.getAbility(379)` 缓存，即 ba1574 summonBallista）
     - `requireAndChargeMana = true`（+0x92=1）
     - `increasedManaCost = -0.5`（+0xA8，即召唤 Ballista mana 消耗 **×0.5**）
     - `useCreatorAbilityObjectConstruct = true`（+0xB8=1）
     - `mana = 创建者的 BaseMana`（+0xA0，来自 actor）
   - 即：**陷阱死亡时**用 ba1574 的 ability object 构造走一次玩家召唤（同一 `SummonEntityOnDeath`、同一 `BallistaMutator`、同上限/同 duration/同 stat 快照），mana 按 `GetTotalManaCost()×0.5` 扣（`requireAndChargeMana=true`）。
   - `[ExplosiveTrapDamageMutator:ISIL 684–740]`；`Awake` 缓存 379 见 `:ISIL 034–037`。
5. 如果本次判定失败或没有点 Siege，则不会加该组件 → 无召唤。

### 8.2 相关节点（部分确认）

| 节点 | 目录统计 | 代码 case（ExplosiveTrapTree） | 目标字段 | 状态 |
|---|---|---|---|---|
| 30 Siege Engineering 2/2 | +10% summon / 50% mana | Explosive Trap Drops Ballistas | `chanceForBallistaOnDetonation += 0.10/点` | **已确认**（常量 0.10） |
| 28 Trap Sprinkler 3/3 | +10%、6、+1 mana | Explosive Trap Drops Traps | `chanceForNewTrap += 0.10/点` | **已确认**（常量 0.10；node id 按目录 +10% 对应） |
| 29 Automated Bombardment 1/1 | +70%、+11 mana | Explosive Trap Chance To Drop Fresh Traps | `chanceForNewFinalTrap += 0.70/点`；`addedManaCost += 11/点` | 高置信（node 统计与常量 0.70、11 完全一致） |
| 24 Clustered Explosives 2/2 | +1、+6 mana | Explosive Trap Additional Traps Per Throw | `additionalTrapsThrown += points`（int） | 高置信（`Add stack0xAC, stack0xAC, rsi`） |
| 23 Minefield 1/4 | +1 | Explosive Trap More Traps | `additionalMaxTraps += points`（int） | 中置信（分支存在，但 ISIL 里 points 加法未逐指令核对） |
| 26 Blast Rain 1/1 | 4×flag | Explosive Trap Blast Rain | `firesArrows(0x141)=true`（陷阱改为弓箭版） | 中置信 |
| 27 Impact Trigger 1/1 | flag+75%+flag | Explosive Trap Blast Rain Instant Detonation When Hitting Ground | `arrowTrapsInstantlyDetonate(0x140)=true`；`AddedStat(SP=45, 0.75/点)` | 低-中置信（常量 0.75 与 node 统计一致，但 case 名与统计的对应需再核） |
| 7/8/9/10/11/19/20/21/25/31 等 | — | 待逐条对照 | — | **未完成**（见 unknown U-3） |

`ExplosiveTrapMutator` 关键常量（diffable）：`MAX_ACTIVE_TRAPS=6`、`MAX_EXCESS_TRAP_TRIGGERS=3`、`EXCESS_TRAP_TIMEFRAME=2`、`TIME_BETWEEN_DETONATIONS=0.1`、`ADDITIONAL_DELAY=0.3`、`BALLISTA_MANA_MULTIPLIER=0.5`（与 §8.1 一致）。

**超上限陷阱的强制引爆限速器（已核）**：`TriggerExcessTraps`（ca ISIL 055–158）取 `maxTraps = limitedToOneTrap ? 1 : get_maxTraps()`；当 `activeTraps.Count > maxTraps` 时对超出部分逐个 `AttemptTriggerExcessTrap`；后者（ca ISIL 089–133）维护 `Time.time` 历史：**2s 滑动窗口内最多强制引爆 3 颗**，超过则跳过。因此 Trap Sprinkler / Automated Bombardment 产生的溢出陷阱不是“无限瞬时排队引爆”，而是被 `≤3 次/2s` 平滑；活动陷阱上限 6。

**mana 扣除（已核）**：`CreateAbilityObjectOnDeath.createAbilityObject`（ca ISIL 156–214）在 `requireAndChargeMana` 时：
- `baseCost = BaseMana.getManaCost(ability 379)`（默认 = Ability.manaCost 44，未叠加 mutator 的 added/increased/more，除 `increasedManaCost` 项）
- `cost = (1 + increasedManaCost) × baseCost = (1 − 0.5) × 44 = 22`
- 默认 `allowPayingIntoNegativeMana(0x93)=false`：若当前 mana < 22 → **直接跳过整个 ability object 创建**（该次召唤完全不发生、也不扣 mana）；否则 `BaseMana.reduceMana(22)`。
- 即 Siege Engineering 的召唤成本是 **22**（不是 11）；“每点 50% mana”是 `increasedManaCost=-0.5` 的乘性减免，不是把 44 变 11。

### 8.3 事件频率代数（本构筑，Boss Dummy 场景）

已核实的节点系数（本构筑）：

- **Clustered Explosives 2/2** → `additionalTrapsThrown += 点数 = +2` → 每次施放基础陷阱数 `n_throw = 1 + 2 = 3`（ca：`ExplosiveTrapTree` case “Explosive Trap Additional Traps Per Throw”，`Add stack0xAC,stack0xAC,rsi`，最终写 `[rbx+0x1B0]`）
- **Trap Sprinkler 3/3** → `chanceForNewTrap = 0.30`（0.10/点，常量 0x18457CDB8）
- **Automated Bombardment 1/1** → `chanceForNewFinalTrap = 0.70`（0.70/点）＋ `addedManaCost = 11`
- **Siege Engineering 2/2** → `p_siege = 0.20`
- 超上限限速：≤3 次强制引爆/2s，活动陷阱 ≤6

设：

- `λ_throw` = Explosive Trap 每秒施放次数（玩家 AS/操作，**外部输入**；公式见 §10）
- `λ_spawn ≈ λ_throw × n_throw × (1 + p_sprinkler)`：每次施放 3 颗、每颗再 30% 概率额外抛 1 颗（“Drops Traps”语义）→ 若逐颗判定成立
- `λ_detonate ≈ λ_spawn × 1/(1 − p_final)`：每次引爆 70% 再丢一颗最终陷阱 → 几何链期望倍率 `1/(1-0.7)=3.33`（**上界**；受 ≤6 上限与 ≤3/2s 强制引爆限速约束）
- `λ_summon = λ_detonate × p_siege`
- `λ_explosion ≈ λ_summon`（稳态；每个召唤最终对应一次死亡爆炸；第 3 次召唤替换最旧 Ballista 会提前引爆，但总数守恒）
- 存活约束：`min(2, …)`；若 `λ_summon > 2/6.5 = 0.3077/s` 则始终满员并频繁替换（爆炸速率仍 = λ_summon）
- mana 约束：每施放 8 + 每次成功召唤 22（mana 不足则跳过该次召唤）

代入本构筑（上界）：

```
λ_explosion ≈ λ_throw × 3 × 1.30 × 3.333 × 0.20 ≈ 2.60 × λ_throw
```

**注意**：`λ_spawn` 的“逐颗 30%”与 `λ_detonate` 的“几何链”目前只验证了字段值/概率来源与分支存在，尚未逐指令证明判定次数与嵌套调用顺序（U-3），因此 `2.60 × λ_throw` 只能作为上界/待验证值，不能作为最终 DPS 频率。若保守只计入已确认的“3 颗/施放 + 20% 召唤”，则下界为 `0.60 × λ_throw`。

稳态关系与约束（方法说明）：

- `λ_summon = λ_detonate × p_siege`；`λ_explosion ≈ λ_summon`（每个召唤最终对应一次死亡爆炸；第 3 次召唤替换最旧 Ballista 会提前引爆，但总数守恒）。
- 空场瞬态例外：首次填充只有 2 个位置，前两个召唤的爆炸要等到期/替换才发生（只影响启动瞬态，不影响长期平均）。
- 存活上限：任何时刻最多 2 个 Ballista；duration=6.5s。若 `λ_summon > 2/6.5 ≈ 0.3077 /s`，则始终满 2 个且频繁替换——此时爆炸速率仍 = `λ_summon`，只是单次爆炸对应的存活时间 < 6.5s（不影响单次爆炸伤害）。
- `λ_throw` 属于玩家操作/攻速的外部输入（见 §10）；trap 的 arm time、触发半径、Trapas 等未纳入（U-3/U-9）。
- **mana 约束**：每次施放 8 mana；每次成功 Siege 召唤额外扣 **22** mana（44×0.5），mana 不足则该次召唤跳过。mana 池/回复属于场景假设（外部输入）。

---

## 9. Elixir of Construction / 药剂窗口（问题 12 / Q12）

- 节点 25 → `moreDamageAfterPotion = 0.15 × 点数`（3/3 = 0.45）。
- `BallistaMutator.onPotionUse()`：若 `moreDamageAfterPotion > 0`：`StopAllCoroutines` 后启动 `PotionUsedRecentlyTimer` 协程。[BallistaMutator:ISIL 1165–1201]
- 协程：`potionUsedRecently(0x198)=1` → `WaitForSeconds(potionUsedRecentlyTime(0x19C))` → `potionUsedRecently=0`。[BallistaMutator_NestedType__PotionUsedRecentlyTimer_d__62:ISIL MoveNext]
  - `potionUsedRecentlyTime` 是序列化默认值，IL 中未见常量赋值；节点文案/任务口径为 **4s**，记为**数据配置值（未从 IL 复算）**。
- `BallistaAdapter.adapt`：若 `moreDamageAfterPotion(0x48) > 0` → 生成 `Stat(SP=Damage, more = 值)` 并包 `Buff(stat, 20.0s)`（常量 `0x18457CC54=20.0`），加到 Ballista 的 `StatBuffs`。[BallistaAdapter:ISIL 434–467]
- **快照语义**：`moreDamageAfterPotion` 只在 `BallistaMutator.Mutate`（召唤那一刻）判断 `potionUsedRecently` 后写入 adapter（ISIL 588–591）；因此召唤之后使用药水不会给已存在 Ballista 加；已挂上的 Buff 持续 20s（> lifetime 6.5s，等效全程）。
- Care Package(节点31)：`maxPotionsDropped += 1`，adapt 里用于 `ChanceToDropPotionsOverTime` 式组件（+0x54 读值，interval = lifetime/上限）→ **药水来源**，不直接改 DPS。

---

## 10. 攻击/施放频率公式（问题 9 / Q9，含未知）

`UsingAbility`（ca ISIL 13560–13740）给出可核实的骨架：
- `useSpeedMultiplier(0x11C)` = `baseUseSpeedMultiplier(=1.0f 默认, UsingAbility 0x154)` × `Ability.speedMultiplier(0x60)` × `getSpeedMultiplierStat(AbilityInfo, tags, increaseFromMutator)`，并 clamp 下限 0.1（常量 0x18457CDB8）：[UsingAbility:ISIL 204–283]
- `CastSpeedManager.getUseDuration/​getUseDelay`：先查 `overrides` 列表（entity 100053 的 override 是 `entity_fm8_2`，与 bolt 无关），无 override 返回 0（表示走默认）→ `UsingAbility` 用 `Ability.useDuration/useDelay` 除以 useSpeedMultiplier：
  - `effUseDuration = Ability.useDuration / useSpeedMultiplier`
  - `effUseDelay = Ability.useDelay / useSpeedMultiplier`
  - 若 `effUseDuration < effUseDelay`，则 `effUseDuration = effUseDelay + 0.01`（0x18457CDAC=0.01）。[UsingAbility:ISIL 264–319]
- `getSpeedMultiplierStat` 精确组合（attack speed vs cast speed、more/additive 的细则、以及 `Ability.as` 这个 letools 字段在 IL 中对应哪个字段）**未完全解码** → U-4。已确认：`speedScaler=SP.AttackSpeed(2)` 时使用 AttackSpeed 池；`speedMultiplier` 为乘性。

按此骨架：

- **minion ballista 攻击间隔** ≈ `max(useDuration 2.3, useDelay 1.6) / (1.5 × (1 + Σincreased AS) × Π(1+more AS))`。
  - 本构筑 minion AS 加成（都在 minion stats 上）：节点 11 +6% increased、节点 14 +40% more、节点 23 -15% more、节点 12 -40% more、节点 18 (Dex/5)×1% increased、minion 基础 +20% increased、attributeScaling Dex×1% increased、manager `increasedBallistaAttackSpeed` 等。
  - 纯数值示例（不含 more 组合细则）：若 increased=+26%（6+20）、more 乘区=0.4×0.6×0.85（=0.204）→ `U=1.5×1.26×1.204≈2.275` → 间隔 ≈ 2.3/2.275 ≈ 1.01s。**这只是骨架代入，不是最终值**（getSpeedMultiplierStat 的精确组合未解码）。
- **玩家召唤 ba1574**：`speedScaler=SP.None(54)`、speedMultiplier=1 → 施放时间 0.6s 不随 AS/CS 缩放（只受通用施放速度机制中未确认部分影响）。[ability_ba1574]
- **Explosive Trap 施放（投掷）**：`useDuration 1.35s`、`useDelay 0.4s`、`speedMultiplier 1.8`、`speedScaler=SP.AttackSpeed` → `effUseDuration ≈ 1.35 / (1.8 × (1+ΣAS))`、`effUseDelay ≈ 0.4 / 同`；实际“每秒施放”还要看 combo/动画与玩家操作（外部输入）。
  - Blast Rain(26) 使陷阱走弓箭版（`firesArrows=true`），是否改变施放节奏未确认（U-3）。
- 召唤上限 2、duration 6.5s → 最长稳定在场时间节奏见 §8.3。

---

## 11. 其它条件（问题 13 / Q13）

- `moreDamageAfterPotion`：见 §9。
- `gainsFrenzyNearPlayer`（节点未点）、`tripwire`、`hasShield`、`fireMode`、`harpoon`：本构筑未点/未启用，不影响本次 DPS 口径。
- 本构筑有 **Explosive Delivery（节点12）**：它只改 bolt/shot（`arrowsFireExplodeOnEnd=true` + `Stat(SP=2, more=-0.40)`），**不改变 Armed Construction 爆炸的伤害或触发**；因此按 lead 口径不计 bolt 命中/爆炸，但它带来的 -40% more AS 会进入 minion 的 AS 池（不影响爆炸事件数）。
- 爆炸的伤害乘区顺序（按 Stats 通用规则）：`base × (1 + Σincreased) × Π(1+more) × 暴击期望`；本构筑专属乘区：minion 基础 more 0.5、节点 23 more 0.36、节点 14 more 0.40、爆炸 per-Dex more `0.04×Dex`、Elixir more 0.45（有条件）、新增的 AS 不参与伤害乘区。

---

## 12. 未解决 / 需要数据侧或实测的项（unknown）

| ID | 问题 | 证据现状 | 处理建议 |
|---|---|---|---|
| U-1 | `isBallistaExplosion(0x13C)` 由谁置 true | IL 中无写点；组件/mutator 在 Ballista 实体 prefab 上，极可能序列化默认 true | 若建模必须确认，可在游戏内观察“无节点5时爆炸是否有内在 25% radius”；或从资产包反序列化 prefab |
| U-2 | 玩家 flat added bow damage 是否可能进入爆炸 added 池 | IL 未见 ballista 转移通道；`addedDamageScaling=2.5` 只放大 added 池 | 数据侧核对玩家装备/被动提供的 minion-added 通道；当前按“不转移”建模 |
| U-3 | Explosive Trap 33 个节点的完整映射（尤其 Trap Sprinkler/Automated Bombardment 连锁、Impact Trigger、Blast Rain、Trapas、Ash Compartment、Lightning Bomb、Sky Signal、Free Lofting Bird、Jelkhor's Blueprint、Flash Frozen、Static Mines） | 已提取全部 switch case 字符串与最终字段写回表，只逐条核对了 6 个 | 后续脚本：对每个 case 分支提取“常量→stack 槽→最终字段”三元组（本轮 `<author-archive>/re-gamecode/dump_ex4tp_cases2.py` 已能列出分支，需补全常量解析） |
| U-4 | `getSpeedMultiplierStat` 精确公式（additive/more、`as` 字段、动画因子） | `UsingAbility` 里 3 层虚调用未完全解码；已确认 speedMultiplier 乘性、下限 0.1、duration/delay 均除同一 U | 继续读 `UsingAbility.getSpeedMultiplierStat`（ca 行 15820）与 `UsingAbilityPlayer.getBaseUseSpeedMultiplier`；或游戏内测攻速曲线 |
| U-5 | 角色等级 minion 伤害缩放是否真实生效 | `CharacterClass.getMoreMinionDamageFromCharacterLevel(level)` 公式 = `(level − firstLevelForMinionScaling + 1) × moreMinionDamagePerLevel`（level≥first 时），但**全工程只有 CharacterSheet（面板显示）调用**，未找到应用点 | 若 lead 确认 26/0.008：100 级 → (100−26+1)×0.008 = **0.60（+60% more）**；但**“是否作用到 minion 伤害”未确认**，暂不写入伤害公式 |
| U-6 | 爆炸 damage tag 的最终 AT 集合 | ability tags=2056={Bow,Fire}；组件 damageTags=2048={Bow}；是否再并 `Hit`(8388608) 由 `DamageEnemyOnHit.setStandardVariables(isHit,tags)` 决定，未找到该调用点 | 需读 `DamageEnemyOnHit` 初始化；当前按 {Bow, Fire} 处理，`Hit`/`Minion`/`Area` 标签不假定 |
| U-7 | `minimumAge`/`delay` 等 `CreateAbilityObjectOnDeath` 序列化参数（爆炸是否延迟/位置偏移） | adapt 只设 ability/enabled；其余来自 prefab 序列化 | 默认按“死亡即刻、Ballista 位置、1 次”建模 |
| U-8 | potion 窗口 `potionUsedRecentlyTime` 具体值 | 字段来自序列化，IL 未赋常量 | 采用 4s（任务/文案口径），标注为配置值 |
| U-9 | ex4tp 每次施放陷阱数/arm time/触发半径/上限 6 的运行时验证 | `MAX_ACTIVE_TRAPS=6` 常量、`armTime` 字段存在，但本构筑节点对它们的修正未全解 | 影响 `n_trap` 与 `λ_detonate`，需 U-3 完成后才能给终值 |

---

## 13. 证据索引（文件 / 方法 / 关键指令行）

> 路径均为仓库根相对；行号指该文件内行号（ISIL 行号在正文另注）。

| 证据 | 位置 |
|---|---|
| Ballista 节点映射 | `<author-archive>/cpp2il_isil/IsilDump/LE/BallistaTree.txt`（updateMutator：文件行 54–2107，ISIL 1–1030） |
| GetValueMultiplier / GetStatsFromPlayer | `<author-archive>/cpp2il_isil/IsilDump/LE/BallistaMutator.txt`（ISIL 1–60 / 862–1124 区段） |
| Mutate（召唤组装） | 同上，文件行 2430–3896（ISIL 1–742） |
| mutateUseSpeed / getTempStats | 同上（mutateUseSpeed 文件行 1246–1363；getTempStats 1988–2052） |
| 爆炸 mutator | `<author-archive>/cpp2il_isil/IsilDump/LE/BallistaExplosionMutator.txt`（Awake/OnFirstTick/getTempStats/addsTempStats：ISIL 1–273；Mutate：294–849） |
| Adapter（死亡爆炸挂载、药水 Buff、字段复制） | `<author-archive>/cpp2il_isil_ca/IsilDump/LE/BallistaAdapter.txt`（adapt：ISIL 1–810） |
| 召唤生命周期 | `<author-archive>/cpp2il_isil_ca/IsilDump/LE/SummonEntityOnDeath.txt`（Summon ISIL 1423–3149 区段；limit 替换 1450–1565；duration 3083–3123） |
| 到期 | `<author-archive>/cpp2il_isil_ca/IsilDump/LE/UnsummonAfterDelay.txt`（OnUpdateTick ISIL 014–048）；`<author-archive>/cpp2il_isil_ca/IsilDump/LE/Summoned.txt`（unsummon ISIL 1–166，die 在 165） |
| CreateAbilityObjectOnDeath（死亡爆炸组件、mana 校验） | `<author-archive>/cpp2il_isil_ca/IsilDump/LE/CreateAbilityObjectOnDeath.txt`（createAbilityObject：ISIL 105–214 含 getManaCost/reduceMana/不足跳过） |
| Siege Engineering | `<author-archive>/cpp2il_isil_ca/IsilDump/LE/ExplosiveTrapDamageMutator.txt`（Mutate ISIL 676–740；Awake ISIL 34–37 缓存 379）；`<author-archive>/cpp2il_isil_ca/IsilDump/LE/ExplosiveTrapMutator.txt`（Mutate ISIL 7332–7338）；`<author-archive>/cpp2il_isil_ca/IsilDump/LE/ExplosiveTrapOnGroundMutator.txt`（ISIL 833–836） |
| 陷阱树映射 | `<author-archive>/cpp2il_isil/IsilDump/LE/ExplosiveTrapTree.txt`（updateMutator；最终写回文件行 2092–2245） |
| 攻速公式骨架 | `<author-archive>/cpp2il_isil_ca/IsilDump/LE/UsingAbility.txt`（ISIL 204–319）；`<author-archive>/cpp2il_isil_ca/IsilDump/LE/CastSpeedManager.txt`（getUseDuration/getUseDelay） |
| Stat 构造/复制 | `<author-archive>/cpp2il_isil_ca/IsilDump/LE/Stats_NestedType_Stat.txt`；`<author-archive>/cpp2il_isil_ca/IsilDump/LE/Stats.txt`（MoreStat/IncreasedStat/AddedStat） |
| 常量 | `<author-archive>/re-gamecode/read_consts.py` / `read_consts2.py`；源 `GameAssembly.dll` `.rdata`（VA→file offset 见 `<author-archive>/re-gamecode/pe_sections.json`） |
| 目录数据 | `docs/evidence/ballista/ballista-runtime-and-node-values-20261112.json`；`<author-archive>/re-gamecode/letools/*.json`（本轮从本机 letools version150 重新导出） |

## 14. 复现命令（临时脚本，均在 `<author-archive>/re-gamecode/`）

```powershell
# letools 数据导出（node vm，不需要网络）
node <author-archive>/re-gamecode/dump_letools.js          # 导出 ability / entity / tree / bonuses JSON
# tree 节点结构
.\.venv\Scripts\python.exe <author-archive>\re-gamecode\dump_tree.py
# ex4tp 节点 switch 分支与常量提取
.\.venv\Scripts\python.exe <author-archive>\re-gamecode\dump_ex4tp_cases2.py
# 静态 float 常量（VA -> file offset -> float）
.\.venv\Scripts\python.exe <author-archive>\re-gamecode\pe_parse.py
.\.venv\Scripts\python.exe <author-archive>\re-gamecode\read_consts.py
.\.venv\Scripts\python.exe <author-archive>\re-gamecode\read_consts2.py
```

（本文件为新增证据文件，不修改任何既有证据/数据/代码。）
