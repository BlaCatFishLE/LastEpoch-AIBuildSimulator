# 本地游戏文件只读逆向：进展记录 #1（2026-11-04）

> **公开版说明（2026-11-13）**：本文件是作者本地研究记录，保留当时记录的路径与数值作为历史依据；文中 `archive://examples/…`、`archive://reports/…`、`archive://docs/history/…` 与已从公开版移除的历史规则包均为作者本地归档（统一以 `archive://` 前缀标记），不随公开仓库分发。可复用结论与其来源定位见 [docs/evidence/extracted/](../extracted/README.zh-CN.md)。

## 0. 授权与边界

用户 2026-11-04 回复：**（1）只读逆向本地 1.5.3 游戏文件：许可；（2）联网下载开源 IL2CPP 分析工具：许可；（3）游戏内受控实测：等前面工作完成后再开工。**

本记录只覆盖**只读**分析；未修改任何游戏文件，未执行游戏代码；工具下载到 `<author-archive>/rev/`（不进交付物，记录哈希/版本）；证据以小型字段摘录存于本目录 `evidence/`。

## 1. 版本与环境（实测）

| 项 | 值 |
|---|---|
| 安装路径 | `<game-install>` |
| 游戏版本 | **1.5.3**（`Last Epoch_Data/globalgamemanagers` PlayerSettings bundleVersion；Steam appmanifest buildid **26087601**） |
| Unity | **6000.4.8f1**（Unity 6.4） |
| `GameAssembly.dll` | 97,256,960 B，sha256 `323e9183f3bee600b6c9e1c469218b9de926e9044f2ca3d44fae8a0ec9a91464` |
| `global-metadata.dat` | 36,457,932 B，sha256 `2a4e8440606c420345934158b6784d8f5d8b0184f32fd91af0e9e0903ba94f83` |
| IL2CPP metadata 版本 | **39**（Il2CppDumper 原版报 `not a supported version[39]`） |

## 2. 工具链（版本与结果；全部仅本机使用）

| 工具 | 版本/资产 | sha256 | 结果 |
|---|---|---|---|
| Il2CppDumper（Perfare） | v6.7.46 net6-win | zip `d1117d66…` | **失败**：metadata v39 不支持（保留为对照记录） |
| Cpp2IL（SamboyCoding） | 2022.1.0-pre-release.21 Windows | exe `663fb432433b4371fd1ee0ebc321a8fff2a9aac5ac4230c843f9e03ddee4e04c` | metadata v39 **解析成功**；DummyDll 输出成功；IL recovery 的方法体为空（未完全恢复） |
| Cpp2IL StrippedCodeRegSupport 插件 | pre-release.21 | `2cc4f8c66541f18b4daed5410149ee7859c3190454d30bda5ccd0895d2989ee6` | 加载成功，但仍未恢复方法体 |
| Il2CppDumper（c01ns fork） | v108.0 net8（含 v39 适配） | zip `4c47399ab14ca7260418bd272b9b94af36dbaf4fee34a60bdf05bbd4fe6e7364` | **成功**：`dump.cs` 56,303,550 B、`script.json` 120,667,904 B、`il2cpp.h` 93,509,189 B、`stringliteral.json` 4,550,576 B、DummyDll |
| ILSpy | 9.1.0.7988 binaries（net8，`ICSharpCode.Decompiler` 9.1） | zip `1e925a4ce4c79d68139a486da66c682e2191e51215188816f613866c7276fc55` | 可用（PowerShell 7 加载引擎）；因 IL recovery 为空，暂未产生有效 C# 方法体 |
| Capstone | 5.0.7（pip） | — | 原生 x64 反汇编（用 dump.cs 的 RVA/Offset） |
| UnityPy | 1.25.4（项目内既有） | — | 游戏 bundle 内嵌 type tree，MonoBehaviour 字段级读取可用 |

Metadata 规模：**40,789 个 type definition、272,489 个 method pointer**。`dump.cs` 提供每个方法 `RVA/Offset/VA`，`script.json` 提供方法名 ↔ 地址索引；因此即使 IL 恢复失败，仍可对**指定方法做原生反汇编**。

## 3. 已确认的数据事实（1.5.3 本地证据）

### 3.1 本体重复伤害组件 = `RepeatedlyDamageEnemiesWithinRadius`

- 类字段（dump.cs）：`radius, minRadius, modifyRadiusOverTime, radiusChangePerSecond, damageInterval, damageIncreasePerInterval, maxDamageIncreaseFromInterval, limitMaxDamageIncreaseFromInterval, damageAtStart, undodgeable, offset, maxTargets, alsoDamageParent, delay`，另有方法 `OnFirstTick / OnUpdateTick / OnDestroy / DealAllRemainingDamage(float remainingDuration) / getApplicationsPerSecond`。
- **Black Hole 实际实例**：`PermaLoad.bundle` 中 GameObject `BlackHoleSpell` 上的组件实测：
  - `damageInterval = 0.3`
  - `damageAtStart = true`
  - `radius = 3.0`
  - `delay = 0`，`damageIncreasePerInterval = 0`，`maxTargets = 0`
- `OnFirstTick` 反汇编（VA 0x181015310）：读 `damageAtStart`（偏移 0x135）；为 true 时把 `damageInterval`（0x128）写入累计器 `index`（0x158）→ **damageAtStart 表示生成时立即造成第一跳**（与 v1「首跳在生成时刻」一致）。
- `DealAllRemainingDamage` 反汇编开头（VA 0x1810149A0）：`remainingDuration / damageInterval` → 取整（floor）→ 若 ≤0 直接返回；随后对范围内目标结算。**初步结论**：销毁/提前结束时只结算「剩余完整 interval」的跳数，不足一个 interval 的余量在数学上可能被丢弃——但**函数尾部还包含一次额外结算路径**，需完成尾部分析后才能下最终结论（v1 用 rate×duration 会把余量算进去，参考模型则丢弃；这是当前 v1 与游戏真值最可能的差异点之一）。
- 残余不确定：`OnUpdateTick` 的伤害数值是否按「每 interval 一次、每次 = 每秒速率 × interval」结算（v1 假设），以及 `DealAllRemainingDamage` 尾部的具体次数。

### 3.2 Ignite 异常（1.5.3）= 火焰穿透型，不是伤害乘区

`PermaLoad.bundle` 中 `Ailment` 资产 `Ignite`（path_id 见证据 JSON）实测：

| 字段 | 值 |
|---|---|
| `id` | 1（AilmentID.Ignite） |
| `duration` | 2.5 s |
| `dealsDamage` | true |
| `dealsAllDamageAtEnd` | false |
| `baseDamage.damage[]` | [0, **40.0**, 0, 0, 0, 0, 0]（索引 1 = FIRE） |
| `effectOfIncreasedEffectiveness` | **1** |
| `additionalPenetrationDamageType` | **1** |

枚举定义（dump.cs，metadata v39）：
- `Ailment.EffectOfIncreasedEffectiveness`：`MoreDamageAndBuffEffect = 0`，**`AdditionalPenetration = 1`**
- `DamageType`：`PHYSICAL=0, FIRE=1, COLD=2, LIGHTNING=3, NECROTIC=4, VOID=5, POISON=6`

**结论（数据层）**：对 Ignite 而言，"increased effect of ailment（点燃效果）"的机制是 **额外穿透**（`AdditionalPenetration`），穿透伤害类型为 **Fire**。这与用户指出的装备词条 "Fire Penetration with Ignite" 以及 phase-1 dataset 003 的修正完全一致；用户 2026-11-04 进一步澄清该词条来自 BD 导入错误，"Fire Penetration with Ignite" 才是正确词条，**不需要继续做 effect→穿透缩放的逆向检查**（v1 已按点燃专属 35% 火穿透处理，数据修正已发布）。

### 3.3 Black Hole 技能本体

`PermaLoad.bundle` 中 `Ability` 资产 `BlackHole`（`m_Name="BlackHole"`，`abilityName="Black Hole"`，`playerAbilityID="bh2"`）：

| 字段 | 值 |
|---|---|
| `useDuration` | **0.6** s（基础使用时长；与 v1 cast 公式 base_use_duration=0.6 一致） |
| `tags` | 4356 |
| `manaCost` | 35 |
| `maxCharges` | 1.0（基础；额外充能来自技能树/装备） |

### 3.4 技能树节点的实现字段（正在补齐数据）

dump.cs 中 Black Hole 的两个 mutator 类字段直接对应节点效果：
- `BlackHoleMutator`：`convertToFireDamage`、`chanceToIgnitePerUncappedFireRes`（**Heat Wave**）、`forceSingleBlackHole`（Singularity）、`extraBlackHoles`、`binaryStar`、`addedCooldownLength`、`chanceToRecastOnEnd`、`moreDamageAtCentre`、`prioritisesRaresAndBosses`、`getCharges/getChargeRegen/getAddedCharges/GetAddedCooldownLength/GetMinimumCooldown` 等；
- `BlackHoleEndMutator`：`convertToFireDamage`、`binaryStar`、`moreCriticalMultiplier` 等。
- 可据此定位：Red Giant = `convertToFireDamage`；Binary System = `binaryStar`；Heat Wave = `chanceToIgnitePerUncappedFireRes`；Singularity = `forceSingleBlackHole`；额外充能/冷却改动见对应 getter override。

节点数值（1.5.3 的 SkillTreeNode 资产）尚未定位到：phase-1 已知的两个 `SkillTreeNode` bundle 不含这些节点名；下一步用 mage skill tree bundle 定位（或其 GUID 引用链）后补全。

## 4. 待完成清单（P0，按顺序）

1. **本体跳数口径**：完整反汇编 `RepeatedlyDamageEnemiesWithinRadius` 的 `OnUpdateTick` + `DealAllRemainingDamage` 尾部 + `OnDestroy`，回答：每次结算的伤害量、不足 interval 的剩余是被丢弃还是并入最后一跳、damageAtStart 与 delay 的组合；与 v1 rate×duration 口径对比量化。
2. **Binary System / Red Giant**：反汇编 `BlackHoleMutator.getTempStats / Mutate / Awake` 与 `BlackHoleEndMutator.Mutate / getTempStats`，确认 `binaryStar` 是否让总伤害翻倍、是否双份施加 Ignite、`convertToFireDamage` 转换哪部分；结合技能树节点数值。
3. **Heat Wave（bh2 node 6）**：确认 `chanceToIgnitePerUncappedFireRes` 的运算（× uncapped fire res、每秒一次、生成时刻首判）以及是否独立于 attack/cast；结合节点 property 10001 数值。
4. **Ignite effect → 穿透缩放**：定位 `ActiveAilment`/`DamageStatsApplier` 中 `additionalPenetration` 的使用（e.g. 穿透增加量 = increasedEffect × 1? 或另有系数），确认 35% → +35% 火穿是否精确。
5. **冷却/充能/施法**：验证 `getCharges/getChargeRegen/GetAddedCooldownLength/GetMinimumCooldown` 与通用 Stat 应用顺序（ICR/CDR）；施法时间 = AbilityVisuals 动画/速度字段。
6. 节点数据补全并交叉核对 1.5.2 vs 1.5.3 差异；产出 ruleset 更新 + 新 run（不覆盖 v1）。

## 5. 复现

```powershell
# 版本/哈希
Get-FileHash '<game-install>\GameAssembly.dll' -Algorithm SHA256
Get-FileHash '<game-install>\Last Epoch_Data\il2cpp_data\Metadata\global-metadata.dat' -Algorithm SHA256

# 元数据 dump（c01ns fork v108；.NET 8 runtime）
& dotnet '<author-archive>\rev\tools\Il2CppDumper-c01ns\Il2CppDumper.dll' `
  '<game-install>\GameAssembly.dll' `
  '<game-install>\Last Epoch_Data\il2cpp_data\Metadata\global-metadata.dat' `
  '<author-archive>\rev\il2cppdump-v39'

# 游戏数据（UnityPy，只读）
& .venv\Scripts\python.exe -X utf8 <author-archive>\rev\extract_game_data.py

# 原生反汇编（capstone，dump.cs 的 Offset）
& .venv\Scripts\python.exe -X utf8 <author-archive>\rev\disasm.py 0x1013D10 0x1013E30 0x10133A0
```

## 6. 状态

- 本轮未改动 ruleset / phase-3 发布 run / 数据集；v1 交付物保持冻结。
- 已产出的证据 JSON（随本目录交付）：`evidence/toolchain-and-version-1.5.3.json`、`evidence/game-data-blackhole-ignite-1.5.3.json`、`evidence/code-findings-1.5.3-round1.json`。
- 下一轮继续完成 §4 的 P0 1–3（跳数口径、Binary/Red Giant、Heat Wave），然后处理 Ignite effect 穿透缩放与冷却公式，最后更新 ruleset 并发布新 run。

## 7. 第二轮：代码级结论（原生反汇编，2026-11-04）

工具链补齐：`dump.cs` 的 RVA/Offset 已解析为 244,416 个方法索引 + 33,316 个类字段偏移（`<author-archive>/rev/symbols.json`），可用 capstone 对指定方法做带字段/调用注解的原生反汇编（`<author-archive>/rev/asm/*.txt`，脚本 `analyze.py`）。IL 体恢复仍为空，因此结论均来自**原生指令 + 游戏数据**。

### 7.1 本体跳数（高置信度）

- 组件 `RepeatedlyDamageEnemiesWithinRadius` 的 Black Hole 实例：`damageInterval=0.3`、`damageAtStart=true`（`BlackHoleSpell`，path 2064363195564901928）。
- `OnFirstTick`：damageAtStart 为 true 时把累计器置为 interval → **生成时刻立即第一跳**。
- `OnUpdateTick`：累计器 += deltaTime，超过 interval 就减去 interval 并 `applyDamage`；没有按比例补余量的分支。
- `OnDestroy`：只清理引用，**不调用** `DealAllRemainingDamage`。
- `DealAllRemainingDamage(remaining)`：`N = floor(remaining/interval)`，N<=0 直接返回；仅用于显式/提前结束路径。
- `getApplicationsPerSecond()`：返回 `1.0 / damageInterval`，说明 prefab 的 baseDamage 是**每跳伤害**（不是每秒伤害）。

**结论**：正常存续期内跳数 = `ceil(duration / interval)`（首跳在生成时刻 + 每 interval 一跳；7.15s → 24 跳）。也就是说，最后一个不足 interval 的尾段在游戏里被按**一个完整跳**结算（不是按比例并入，也不是丢弃）。v1 的 rate×duration 等于 23.833 跳当量，因此 v1 本体伤害**低估约 0.7%**（与参考模型整数跳结论一致，说明用户规则 1 的“余量并入最后一跳”在游戏实现里是“取整到完整跳”）。该结论推翻 v1 的本体伤害口径，需在新 run 中改为整数跳。

### 7.2 Heat Wave（高置信度，与 v1 期望相同但时序不同）

`BlackHoleMutator.Mutate` 中 `chanceToIgnitePerUncappedFireRes`（0x140）的代码路径：

- 读取玩家 `PrecalculatedStatsHolder.uncappedFireResistance`（0x24）；
- `chance = uncappedFireResistance * chanceToIgnitePerUncappedFireRes * 0.5`；
- `Ailment.getAilment(Ignite=1)`，调用 `RepeatedlyApplyAilmentsInRadius.addChance(ailment, chance, 0, 0, false, 0)`；
- 随后把该 aura 组件配置为 `applicationInterval = 0.5f`、`applyOnStart = true`（0x98 / 0xb4 字段）。

**结论**：游戏 Heat Wave 实际是 **每 0.5 秒一次、生成时刻首判**（不是 1 秒一次），每次几率 = `0.5 × 未封顶火抗 × 节点字段`。因为 0.5 秒 × 两次 = 每秒，**每秒期望层数 = 未封顶火抗 × 节点字段**：5 点（+10%）× 856% = 85.6%/s，与 v1 的 85.6 期望完全一致；差异只在判定时序/方差（v1 按 1 秒一次、85.6% 建模）。用户此前对“每秒一次”的确认是工具提示层表述；新 run 可改为 0.5s 判定（期望不变，首判都在生成时刻）。

### 7.3 Binary System / Red Giant（高置信度）

`BlackHoleMutator.Mutate`（0x182e0e0）中 `convertToFireDamage`（0x13c）与 `binaryStar`（0x184）分支：

- 若二者任一为 true：`getBaseDamage(COLD)` 后 `addBaseDamage(FIRE, coldAmount)` → 把冷伤复制/加到火伤（Red Giant 转换）；
- 若 `binaryStar == false`：再把 `-coldAmount` 加回 COLD → 冷伤被抵消，等于**纯转换**；
- 若 `binaryStar == true`：跳过抵消，**保留原冷伤** → 同时拥有火分量（复制）与冷分量（原值）= **结构翻倍（一火一冷）**。

**代码层结论（结构部分）**：Red Giant 的实现是 `cold → fire` 转换（加火分量、抵消原冷分量）；Binary System 在代码里会跳过“抵消冷分量”的一步，从而得到**一火一冷两个分量**（结构上的双星）。

**但 1.5.3 技能树节点数据补充了关键信息（见 §8）**：Binary System 节点除了 flag 之外还有一条独立词条 **"Damage +100%"**（描述写的是 multiplicative with other modifiers），Singularity 节点同样有 "Damage +100%" + 每施放 1 个黑洞的 flag。因此 v1 在 §7.3 里“+100% 只是结构翻倍、不是独立乘区”的推断很可能是**错的**：需要下一轮确定技能树节点的 "Damage +100%" 如何应用（increased/more）、是否与结构翻倍叠加、以及 Ignite 是否继承该 more。结论在未解决前标记为 open，不应写进正式 ruleset。

### 7.4 仍待完成

1. 1.5.3 的 Black Hole 技能树节点数值（Heat Wave/双星/Red Giant/Singularity）——已在 §8 找到并提取（节点 bundle 为 `assets_5557fd063dd9c5ac.bundle`）。
2. `forceSingleBlackHole`、`extraBlackHoles`、`activeHoles` 的运行时语义（是否在多黑洞并存/重施时销毁旧黑洞）——用户规则 6 的代码级复核。
3. ~~Ignite 的 effect→穿透缩放系数~~ **（用户 2026-11-04 澄清：Ignite effect 来自之前的 BD 导入错误，正确词条是 "Fire Penetration with Ignite"，已按数据修正处理，不需要逆向检查）**。
4. 冷却/充能/施法时间公式（`getCharges/getChargeRegen/GetAddedCooldownLength/GetMinimumCooldown` 与本构筑数值）；节点数据已给 base recharge=14s、Recurrence=property 70。
5. 更新 ruleset（本体改整数跳、Heat Wave 0.5s 判定；Binary/Recurrence 待定）并发布新 run（不覆盖 v1）。

## 8. 第三轮：1.5.3 技能树节点与能力权威字段（2026-11-04）

### 8.1 bh2 Ability 权威字段（`PermaLoad.bundle`，`playerAbilityID=bh2`）

| 字段 | 值 | 含义 |
|---|---|---|
| `useDuration` | 0.6 s | 基础使用时长（与 v1 cast 公式的 base_use_duration 一致） |
| `speedMultiplier` | 0.8 | 基础速度乘数 |
| `speedScaler` | 3 | SP 枚举 3 = CastSpeed（施法速度缩放） |
| `speedScalerEffectiveness` | 1.0 | 施法速度效果系数 |
| `speedScalerAppliedAsIncrease` | false | 不是“increased”直接加到速度 |
| `maxCharges` | 1.0 | 基础充能 1（+1 来自 Pocket Dimension 节点 30） |
| `chargesGainedPerSecond` | 0.07142999768257141 = **1/14** | 基础充能恢复率 → **基础充能时间 14.0s** |
| `manaCost` / `tags` | 35 / 4356 | — |

这把 v1 的 base cooldown 14s 与 cast 公式的 0.6s/0.8/施法速度缩放从“参考模型推导”升级为**游戏数据实测**；但 cast 时间公式的最终形式（含 animation/speed 字段怎么组合）与 ICR/CDR 顺序仍需代码核对。

### 8.2 bh2 技能树节点 1.5.3（`assets_5557fd063dd9c5ac.bundle`，共 32 个节点）

已提取到 v1 规则涉及的节点（完整 JSON 见 `evidence/bh2-skilltree-nodes-1.5.3.json`）：

| id | 节点 | 关键数据 |
|---|---|---|
| 6 | Heat Wave | 词条 "Ignite Chance Per Second Per 1% Uncapped Fire Res" property 10001，+2%/点，max 5 → 5 点=+10%；**alt text：100% 火抗时 1 点=200% 点燃几率/秒**（→ 每秒期望 = 未封顶火抗% × 2% × 点数的整数百分比形式，与 v1 85.6%/s 一致，见 §7.2） |
| 9 | Binary System | flag "Binary System"（property 54）+ **"Damage +100%"（property 0，描述 multiplicative with other modifiers）**；一星火一星冷 |
| 4 | Red Giant | "Base Damage Converted to Fire" flag（property 5080）+ Duration +80%（property 5093）+ 不再拉怪 |
| 28 | Singularity | "Damage +100%"（property 0，multiplicative）+ "Black Holes Per Cast = 1" flag（property 54，downside） |
| 30 | Pocket Dimension | "Additional Charges +1"（property 5066） |
| 17 | Recurrence | "Cooldown Reduction +15%"（property 70，4 点=+60%）+ mana/pull 削弱 |
| 25 | Endless Maw | Duration +30%（property 5013）+ "Cooldown Duration (seconds) +2"（property 70） |
| 5 | Scorching | "Fire Penetration +15%"（property 10042）/点，max 5 |
| 23 | Supermassive | "Damage To Rares And Bosses +25%"（property 0）/点，max 4 |
| 26 | Scattered | 额外第二个黑洞（flag） |
| 29 | Armageddon / 13 Pulsar / 12 Collapse | 每秒/结束爆炸等行为（v1 未纳入） |

**重要影响**：
- v1 把 Binary System 的 +100% 当作“结构翻倍本身”的假设很可能不成立；节点数据同时存在 structural flag 与独立 **Damage +100%** 词条，需要下一轮用技能树 stat 应用代码（`Tree.updateMutator` / SkillTreeMutatorManager）确认它是 more 乘区还是仅 UI 说明。
- 若 +100% 为 more：v1 本体/循环可能被低估最多 2×（且按用户规则 5，Ignite 可能继承 BH 树 more 节点 → Ignite 也可能翻倍）；必须先解决再更新 ruleset。
- Recurrence 的 property 70 显示为 "Cooldown Reduction"（不是 ICR 属性），而 v1 把它并入 ICR 池（`(base+flat)/(1+ICR)`）；这一映射需要在充能/冷却代码中确认，可能改变有效充能时间与循环 DPS。
- Heat Wave 的 alt text 与代码（§7.2）一致：每秒期望 = 未封顶火抗% × 2% × 点数。

### 8.3 本轮新增证据

- `evidence/bh2-skilltree-nodes-1.5.3.json`：节点 id/描述/词条（property、tags、value、noScaling）。
- `evidence/bh2-ability-and-charge-1.5.3.json`：Ability 字段 + 1/14 充能率。
- 节点定位方法：扫描 25,916 个 `assets_*.bundle` 的原始 MonoBehaviour 字节，搜 "Heat Wave"/"Binary System"/"Red Giant"/"Singularity" → 命中 `assets_5557fd063dd9c5ac.bundle`（完整 bh2 树），再按 `SkillTreeNode` 解析。此扫描可复现（脚本 `<author-archive>/rev/scan_node_bundles.py`）。

（第三轮证据 JSON 见上；下一轮 P0：Binary +100% 与 Recurrence/冷却的代码级判定，forceSingle/extra holes，Ignite effect 缩放。）

## 9. 第四轮：`BlackHoleTree.updateMutator` 节点效果解码（2026-11-04）

用 `script.json` 的 ScriptString 表解析出 `BlackHoleTree.updateMutator`（RVA `0x24bd660`）的 switch 标签，并按 case 反汇编出每个 bh2 节点实际添加的 `Stats.Stat`。关键结果：

| 内部标签 | 实际代码效果 |
|---|---|
| `Black Hole Ignite Chance Per Second`（Heat Wave） | 累加器 `+= points × 2.0`（百分比单位，2%/点）→ 与节点词条/alt text 一致；结合 `BlackHoleMutator.Mutate`（×未封顶火抗、0.5s 光环、生成即首判）完成闭环 |
| `Black Hole Convert To Fire`（Red Giant） | 设转换 flag；duration 累加 `+0.8`（+80%）；关闭拉怪 |
| `Black Hole Damage and Less Pull`（Tidal Force） | `Stats.Stat(property=Damage(0), tags=0, increased=0, more=points×0.15)`；拉怪累加 `-points×0.15` |
| `Black Hole Increased Fire Damage`（Flaming） | `Stats.Stat(property=Damage(0), tags=8(Fire), more=points×0.10)` |
| `Black Hole Added Duration and Cooldown`（Endless Maw） | duration `+= points×0.30`；cooldown `+= points×2` 秒 |
| `Black Hole Additional Charge`（Pocket Dimension） | charge `+= points`（+1） |
| **`Black Hole Binary Star`（Binary System）** | **置 binary flag + `Stats.Stat(property=Damage(0), tags=0, increased=0, more=points×1.0)` → 额外的 +100% more** |
| **`Black Hole One Hole Only`（Singularity）** | 置 one-hole-per-cast flag + `Stats.Stat(property=Damage(0), more=points×1.0)` → +100% more（v1 已应用 node 28 more=1.0） |
| `Black Hole Extra Black Hole`（Scattered 等） | extra-black-hole 累加器；运行时语义待定 |

`DamageStatsHolder.getBaseDamage/addBaseDamage` 已反汇编确认：`baseDamage[type]` 是普通 float 数组，`addBaseDamage` 是简单的 `+=`——因此 Binary 的结构性 `cold→fire` 复制是真实的第二份伤害分量，**和** +100% more 统计不是同一件事。

**高风险 open**：Binary System 在代码里同时有（a）结构性双分量（cold 复制为 fire，保留 cold）与（b）独立 +100% more 统计。若二者叠加，本体相对无 Binary 最多是 4×（2 分量 ×2 more），且若按用户规则 5 该 more 被 Ignite 继承，Ignite 也会 ×2；这与 v1 当前的“结构翻倍、extra_more=0”不同（v1 可能总体低估约 2×）。下一轮必须先确认：更多统计是否作用于两个分量、是否被 Ignite 继承、以及是否与游戏内实际表现一致（必要时经用户同意做一次受控实测）。

> **2026-11-05 已解决**：用户游戏内实测 Binary 开/关总 DPS 400k→800k（DPS 主要为点燃），确认 **(a) 结构双分量 + (b) 独立 +100% more 叠加**，且 **Ignite 继承**该 more（总伤害 ×2、Ignite ×2）。v2 规则包已按此定稿，见 §16。

**本轮不影响 v1 交付物**：ruleset/run 未改。

（第四轮证据 JSON：`evidence/bh-tree-mutator-effects-1.5.3.json`）

## 10. 第五轮：冷却/充能数据链（2026-11-04）

**能力数据**：bh2 `chargesGainedPerSecond = 1/14`（基础充能 14.0s），`maxCharges=1`（+1 来自 Pocket Dimension），`useDuration=0.6`、`speedMultiplier=0.8`、速度缩放属性 = CastSpeed、效果系数 1.0。

**节点效果（来自 BlackHoleTree.updateMutator）**：
- Recurrence：property 70 "Cooldown Reduction" +15%/点（4 点 = 60%）；在 `AbilityMutatorManager.blackHoleReducedCooldown`（offset 0xad8）。
- Endless Maw：duration +30%/点、cooldown **+2s/点**（累加路径）。
- Pocket Dimension：`blackHoleAddedCharges`（0xae4）+1。

**代码路径**：`BlackHoleMutator.getChargeRegen` override（RVA 0x1831320）取的 base 是 `AbilityMutator.getChargeRegen()`（ability 的 1/14 + addedChargeRegen），随后若 `blackHoleReducedCooldown != 0`：`rate = base / (1 - reducedCooldown)`。即 **Recurrence 60% 是 (1-CDR) 形式的充能速率提升**：14s → **5.6s**（在未计 ICR/added cooldown 之前）。

**结论/影响**：v1 把 Recurrence 60% 并入 ICR 加算池，公式 `(14+2)/(1+ICR 1.3575)=6.787s`，与代码路径不符。2026-11-05 用户游戏内实测充能回满 **3.6s**，与 `(14+2)×(1−0.6)/(1+0.7575)=3.641s` 一致 → **Recurrence 是 (1−CDR) 乘算冷却缩减，ICR 是独立分母**；v2 已按此实现并定稿（见 §16）。

**下一步**：`ChargeManager.RefreshChargeInfo` 的 ICR 精确应用位置仍可继续核对（不影响当前结论）；剩余 assumed 项（cast time、ignite scaling pool 等）见 §16。

（第五轮证据 JSON：`evidence/cooldown-charge-1.5.3.json`）

## 11. v2 规则包准备：两项已确认修正的候选影响（2026-11-05）

在等待用户游戏内实测（Binary +100% 与冷却/充能，这两项代码分析已按用户要求暂停）期间，先把**已确认的两项 1.5.3 机制**做成可配置项并跑候选结果（不发布、v1 冻结）：

1. **本体整数跳**：`black_hole.tick_model.remainder_policy` 新增 `ceil_full_ticks`（v1 legacy = `merge_into_last_tick`）。7.15s / 0.3s → 24 跳**满额**（不是 23 + 0.833 余量）。候选对本体影响 **+0.6993%**。
2. **Heat Wave 0.5s 判定**：`ignite.heat_wave.rolls_per_second` 支持 2；每次判定期望层数按 `expected_stacks_per_second × 0.5` 拆分，保证每秒期望不变。代码实现：0.5s 光环、生成即首判 → 7.15s 内 15 次判定 × 42.8 层 = **642 期望层**；而 v1 的 1s 模型是 8 次 × 85.6 = 684.8（对 7.15s 黑洞多算了约 6.25%）。

**候选结果**（已被 §16 的正式 v2 run 取代；仅保留作为"只有两项确认改动"的中间对照）：

| 指标 | v1 | 候选 v2 | 变化 |
|---|---:|---:|---:|
| 单次总伤害 | 31,219,074.24 | 29,512,303.30 | **−5.467%** |
| 本体 | 3,517,205.77 | 3,541,801.61 | +0.699% |
| Ignite | 27,701,868.47 | 25,970,501.69 | −6.250% |
| 期望层数 | 684.8 | 642.0 | −6.250% |
| eager 120s DPS | 4,717,076.66 | 4,460,025.38 | −5.438% |
| patient 120s DPS | 3,967,354.80 | 3,750,927.71 | −5.456% |

**说明**：v1 的 Heat Wave 1s 模型确实高估了期望层数（8 次 1s 判定 = 8s 覆盖，而黑洞只有 7.15s）；0.5s 模型 15 次判定覆盖 7.5s（首判在生成瞬间），是更贴近游戏代码的离散口径。该候选只包含已确认项，**不含 Binary +100% 与冷却公式修正**（等用户实测结果）；不作为最终 run 发布。

（候选影响 JSON：`evidence/v2-candidate-impact-1.5.3.json`）

## 12. 多黑洞生命周期（2026-11-05，本轮补充）

反汇编 `BlackHoleMutator.Mutate` / `onStartedAbility` 后确认：

- `activeHoles`（offset 0x1f0，`List<GameObject>`）是**跨施放的存活黑洞登记表**：`Mutate` 开头会清理已销毁项（`RemoveAll` 风格调用 0x18085b850），结尾把本次生成的洞 `List.Add`（0x1803db5 区块）；`onStartedAbility` 会遍历这个列表给每个洞单独掷 `blackHoleFireballChanceOnDirect`，证明旧施放的黑洞仍在存活并被跟踪。
- 没有任何“新施放销毁旧黑洞”或全局数量上限的代码。
- `forceSingleBlackHole`（offset 0x17c，Singularity）**只在 `Mutate` 中用于跳过“额外黑洞（extraBlackHoles）生成分支”**（0x1830827–0x1830841）：即 **每次施放最多 1 个洞**，并抑制 Scattered 类额外洞；不影响其他施放已存在的洞。
- `extraBlackHoles`（offset 0x180）>0 且未被 Singularity 抑制时，本次施放会额外生成黑洞（`Ability::getAbility` + 延迟施放路径）。
- caveat：`chanceToRecastOnEnd`（结束时有几率再生成一个洞）分支在 Singularity gate 之前独立判断；本构筑未选该节点，暂不影响。

**结论**：多施放独立共存（各自计时/结算），Singularity 只是“单次施放上限 1 个洞”，不是全局限制——与用户规则 6 一致。v1 的 eager/patient 并发模型（无全局硬上限、允许重叠）**不需要数值修改**，只需把 ruleset 中的 `concurrent_instances` / `black_holes_per_cast` 状态从假设升级为 confirmed。

（证据 JSON：`evidence/multi-black-hole-lifecycle-1.5.3.json`）

## 13. v2 规则包草案与候选 run 验证（2026-11-05）

- 草案文件：`archive://tools/phase3/rules/ruleset-bh-stage1-v2.json`（v1 文件保持冻结不动）。
- 已包含的确认项：`ceil_full_ticks` 本体整数跳、Heat Wave 0.5s（每秒期望不变）、Red Giant 转火/持续 +80% confirmed、多黑洞生命周期 confirmed。
- 尚未定稿（保留 v1 假设/open，等游戏内实测）：`rule-bh-binary-more-interpretation`、`rule-ignite-binary-excluded`、`rule-cooldown-icr`、`rule-cast-time`。
- 用 v1 冻结输入快照跑完整候选 run（含 HTML）：一致性 `all_pass`、170 phase-3 测试通过；单次总伤害 29,512,303.30（v1 31,219,074.24，−5.467%）、eager 120s DPS 4,460,025.38（v1 4,717,076.66）。
- `report.py` 的 `status_reason` 改为读取当前 `ruleset_id`（不再硬编码 v1）。
- 复现说明：`evidence/reproduction-1.5.3.zh-CN.md`（命令 + 文件哈希 + RVA/offset 索引）。

得到用户实测数据后：补全 Binary/冷却 → 更新 v2 → 生成正式新 run（新 id，不覆盖 v1）→ verifier/一致性 → 汇报。

**候选 run 的离线 HTML 验证（2026-11-05）**：Chrome headless 加载候选 v2 HTML，进程 exit=0、DOM 2,518,550 B、console/stderr 错误匹配 0；截图确认顶部状态横幅/KPI/时间线正常（单次 29,512,303.30；本体 3,541,801.61；Ignite 25,970,501.69；层数 642.00；body_tick×48；ignite_roll×15）。已把 HTML 顶部横幅与 `status_reason` 中硬编码的 v1 文案改为读取当前 `ruleset_id`，候选 v2 页面显示 `bh-stage1-v2 限定模型`。v2 ruleset 另加 `reverse_engineering` 元数据块（1.5.3 RE 版本边界说明：机制按 1.5.3 逆向，构筑数据仍为 phase1-dataset-003/1.5.2）。

## 14. Elemental 标签过滤（2026-11-05，本轮补充）

1.5.3 的 `AT` 枚举中 **Elemental = 128 是一个独立 flag**，不是 Fire/Cold/Lightning 的位并集。标签匹配函数：

- `Tags::Applicable(AT situationTags, AT statTags)`（RVA `0x16abe60` / offset `0x16aa860`）逻辑为：
  1. 若 `(situation & stat) == stat` → 匹配；
  2. 否则要求 stat 带 Elemental 位（0x80），且 situation 至少含 Fire(8)/Cold(4)/Lightning(2) 之一（0x0e）；
  3. 然后清掉 stat 的 Elemental 位并在 situation 补上 Elemental 位再比较 → 即 **Elemental 统计应用到任何元素伤害情况**。
- `Stats::GetTotalIncreased`（RVA `0x16a9ba0` / offset `0x16a85a0`）内联了同样的 Elemental 展开逻辑，再累加 `stat.increasedValue`。

**结论**：Ignite 的伤害 situation tags 是 Fire（+DoT/Ailment），因此：
- **Elemental 增伤进入 Ignite**（用户规则 3 得到代码级确认）；
- **Spell 增伤不进入 Ignite**（Spell=256 要求 situation 有 Spell 位，而 ailment 伤害没有）；
- Fire-only 增伤只作用于 Fire 情况，不匹配 Cold。

证据 JSON：`evidence/elemental-tag-filtering-1.5.3.json`。

## 15. Heat Wave 光环是"每黑洞一次"而非"每颗星一次"（2026-11-05，本轮补充）

`BlackHoleMutator.Mutate` 区域 0x18302c1–0x1830405：

1. 只调用一次 `Ailment::getAilment(Ignite)` + `RepeatedlyApplyAilmentsInRadius::addChance`（0x1830333），紧接着在同一光环对象上 addChance Chill / Blind —— **这段代码不在任何遍历伤害分量/双星的循环里**。
2. 每次判定几率 = 未封顶火抗 × `chanceToIgnitePerUncappedFireRes`（0x140）× 0.5。
3. 同一光环组件随后写 `applicationInterval=0.5f`（field 152/0x98）与 `applyOnStart=true`（field 180/0xb4）——字段映射：radius=0x90、applicationInterval=0x98、applyOnStart=0xb4。

**结论**：Heat Wave 是**每个 Black Hole 施放一个光环**；Binary System 的两颗星共用这一个光环，没有第二份 addChance/第二个光环 → 每秒判定次数不因双星翻倍。这与 v2 的"one roll per Black Hole（not per star）"假设一致，无需额外数值修正。

证据 JSON：`evidence/heat-wave-aura-1.5.3.json`。

## 16. v2 正式结果（2026-11-05）

### 16.1 用户游戏内实测（两项 open 的裁决）

- **冷却**：当前构筑充能回满 **3.6s** → `(14 + 2) × (1 − 0.6) / (1 + 0.7575) = 3.641s`。Recurrence 60% 是 `(1−CDR)` 乘算冷却缩减；ICR（装备/被动 75.75%）是独立分母。v1 的 ICR 加算池公式（6.787s）被否定。
- **Binary**：训练靶开/关 Binary System，总 DPS **40w → 80w**，且 DPS 主要为点燃 → Binary 的 +100% 是**独立 more、总伤害 ×2、Ignite 继承**；与代码里 `Stats.Stat(Damage, more=1.0)`（除结构双分量外）一致。

证据 JSON：`evidence/user-in-game-measurements-2026-11-05.json`。

### 16.2 正式 run（不覆盖 v1）

- 路径：`archive://reports/phase-3/black-hole-stage1/20261105T002500-bh-stage1-v2/`
- 后续 v2b（2026-11-05 机制 confirm 后发布，数值完全相同）见 §17；v2 保留为历史可复现版本。
- 规则包：`bh-stage1-v2` sha256 `90fc0de4e008343ec82e17169ba574844931a7a0510853ee48248dbb61ff8f8b`
- 输入：v1 冻结 snapshot（build `rev-c746411ccebee9c0` seq8、dataset `phase1-dataset-003`、calc sha `531f7a50…`）
- 文件 sha256：
  - `result.json` `6942dbdee70437e3111544f1e41fe7bc5318bb0f8ebf419c683594e12b0d82fb`（1,917,726 B）
  - `black-hole-damage.html` `9e42ecf812e89b29e5f4692a6709c9dc3676cbfe908c6aa434bd053a32009626`（2,558,097 B）
  - `input-snapshot.json` `ba3178a741eee88c7fc71d1ab0c59e437e8ac4372232667094112fd01f7dcc42`
  - `run-manifest.json` 内包含全部文件 sha256 与 consistency 结果

### 16.3 关键结果（v1 → v2）

| 指标 | v1 | v2 | 变化 |
|---|---:|---:|---:|
| 单次完整伤害 | 31,219,074.24 | **59,024,606.60** | +89.06% |
| 本体 | 3,517,205.77 | **7,083,603.23** | +101.40% |
| Ignite | 27,701,868.47 | **51,941,003.38** | +87.50% |
| Ignite 尾部 | 13,370,341.32 | **25,015,477.31** | +87.10% |
| 期望施加层数 | 684.8 | **642.0** | −6.25%（Heat Wave 0.5s 判定） |
| eager 120s DPS | 4,717,076.66 | **15,989,606.98** | +239.0%（34 次施放，cycle 3.641s，最多 3 个并存） |
| patient 120s DPS | 3,967,354.80 | **7,501,855.41** | +89.09%（16 次施放，cycle 7.654s，始终 ≤1 个并存） |

一致性：`all_pass=true`（8 项全 True）。离线 HTML：Chrome headless exit 0、DOM 2,548,948 B、console/stderr 错误 0；顶部横幅为 `bh-stage1-v2`。

### 16.4 仍不确定 / 假设项（v2 run 保持 assumption 状态）

- `rule-cast-time`（施法时间公式，未游戏内实测）
- `rule-ignite-scaling-pool`（Ignite 玩家侧 inc 池口径）
- `rule-pyromancer-ignite-chance`（Pyromancer +40% ignite chance 是否参与 Heat Wave，用户判断为 hit 相关，未计）
- `rule-ignite-damage-slices`（Ignite 连续流记账/切片口径）
- `rule-preparation`（Preparation 下次元素法术增益，未默认生效）
- `rule-crescendoing-rage`（本阶段不纳入）
- 另有 1 条 warning：`idol_altar` 条件 CDR 词条无法判定（输入侧，已记录）
- 已知命名遗留（非阻塞）：`consistency.window_plus_tail_equals_ignite_total` 实际比较的是 `window + tail = single_total`（window 含本体），verifier 已用 `(window−body)+tail=ignite_total` 单独验证通过；后续可改名，不影响数值与验收。

### 16.4b 独立验收（verifier）

`archive://reports/phase-3/black-hole-stage1/20261105T002500-bh-stage1-v2/v2-review.zh-CN.md`：**PASS（92/92 检查，0 fail）**。包括：A 自洽 20/20；B 独立复算 45/45 + 直接算术 6/6；C manifest/指纹 10/10；D 规则状态 14/14；E HTML 离线 PASS（exit 0、console 错误 0、DOM JSON 严格等于 result、KPI 文本命中、横幅 bh-stage1-v2）；F v1 三个冻结哈希未改；G phase-3 178 tests OK（skip 28）。

### 16.5 复现

```powershell
PYTHONPATH=tools/phase3/src .venv\Scripts\python.exe -X utf8 -m le_calc.cli run `
  --snapshot reports\phase-3\black-hole-stage1\20261104T130800-bh-stage1-v1\input-snapshot.json `
  --ruleset archive://tools\phase3\rules\ruleset-bh-stage1-v2.json `
  --out reports\phase-3\black-hole-stage1\20261105T002500-bh-stage1-v2 `
  --run-id 20261105T002500-bh-stage1-v2
```

独立验收报告：`archive://reports/phase-3/black-hole-stage1/20261105T002500-bh-stage1-v2/v2-review.zh-CN.md`（verifier）；HTML 证据：`archive://reports/phase-3/black-hole-stage1/evidence/html-verification-v2.zh-CN.md`。

## 17. Heat Wave 单次 710 层验证与机制 confirm（2026-11-05）

用户补充测试（**949% 未封顶火抗是临时测试配置，不是构筑静态快照的 856%**；节点与构筑一致；单次 Black Hole；710 为近似值）：

| 口径 | 949% 下最高点燃层数 | 结果 |
|---|---:|---|
| v2：0.5s×15 次、单光环、7.025s | **711.75** | 与实测 ~710 吻合（差 ~0.3%） |
| v1：1s×8 次 | 759.2 | 高约 7%，否定 |
| 双星各一个光环 | ~1423 | 否定 |
| 点燃持续 6.025s | ~617 | 否定 |

结论：**Heat Wave 的 0.5s 判定、生成即首判、每黑洞一个光环（双星共用）、每次判定期望层数公式、7.025s 点燃持续时间**均被这组单次观测定量验证（949% 仅作验证场景，v2/v2b 主 run 仍按构筑快照 856% 计算，642 层）。

另两条机制确认：
- **Pyromancer +40% Ignite Chance 不参与 Heat Wave**：取消加点后 710 层不变；若参与应约 +40%。`rule-pyromancer-ignite-chance` → confirmed。
- **释放速度公式**：用户确认"释放速度 = 基础释放速度 × (1 + 施法速度 inc)"，与 v2 公式形状一致；但基础释放速度未能精确实测（仍来自游戏能力字段 as×speedMultiplier），因此 `rule-cast-time` 标 confirmed 并注明"公式形状 confirmed、基础常数未独立测量"。
- `rule-ignite-heat-wave` / `rule-ignite-heat-wave-timing` 的证据中补充该单次验证。

### 17.1 新规则包与 run（不覆盖 v1/v2）

- 规则包：`archive://tools/phase3/rules/ruleset-bh-stage1-v2b.json`，sha256 `c0da8b19c3d0097f78e0241fe0d503b93b349f218dd19405cb4b98a3ce5d2f07`；相对 v2 仅机制状态/证据变化，数值不变。
- 新 run：`archive://reports/phase-3/black-hole-stage1/20261105T014500-bh-stage1-v2b/`
  - `result.json` sha256 `3b97c9ce750657bab143870061362954b1a4db8a3ea66ae36901b2a8708d0aab`（1,919,107 B）
  - `black-hole-damage.html` sha256 `e8770ce336f7ea0fa949513d72174450b20773ab8660ed249e12b20f285c5d99`（2,560,361 B）
  - 规则状态计数：confirmed 27 / derived 9 / assumed 2 / unresolved 2；仍为 assumption 状态（剩余 assumed：Ignite 玩家侧 inc 池、Ignite 连续流切片；unresolved：Preparation、Crescendoing Rage）。
- v2 run（`20261105T002500-bh-stage1-v2`）保留为历史可复现版本；v1 仍冻结未改。
- 机制验证证据：`evidence/user-heat-wave-validation-2026-11-05.json`；v2b HTML 证据：`archive://reports/phase-3/black-hole-stage1/evidence/html-verification-v2b.zh-CN.md`。
- **独立验收**：`archive://reports/phase-3/black-hole-stage1/20261105T014500-bh-stage1-v2b/v2b-review.zh-CN.md` — **PASS（117/117 检查，0 fail）**；v2b 与 v2 数值完全一致（仅规则状态/证据/元数据变化），HTML 离线 PASS、186 tests OK（skip 28）、v1/v2 冻结哈希未改。

## 18. 怪物等级减伤（Monster DR）接入（2026-11-05）

按用户要求加入游戏内怪物随等级变化的减伤，用于选择怪物等级并显示实际 DPS 以便后续验证。

- 数据来源：tunklab `/mobdr`（页面 1.5.2，等级 1–100；等级 1–6=0%，100=87%），从页面 JS chunk 提取并保存为 `tools/phase3/data/monster_dr.json`（sha256 `699ecc39…`）；详细说明见作者本地归档 `archive://docs/history/phase-3-monster-dr-integration.zh-CN.md`。
- 引擎：新增 `--monster-level 1..100`（0=关闭）；目标侧独立乘区 `× (1 - DR)`；`result.json` 新增 `monster_dr`（等级表 + `dps_by_level` 0/1–100）；离线 HTML 新增「怪物等级与游戏内实际 DPS」面板（下拉 + 101 行换算表）。
- 规则包 v3：`archive://tools/phase3/rules/ruleset-bh-stage1-v3.json`（v2b + monster DR；默认等级 100；`rule-target-monster-dr-table` confirmed、应用方式 assumed）。
- 正式 run：`archive://reports/phase-3/black-hole-stage1/20261105T021000-bh-stage1-v3/`（等级 100，DR 87%）
  - 单次 **7,673,198.86** / 本体 920,868.42 / Ignite 6,752,330.44 / 尾部 3,252,012.05；eager 120s DPS **2,078,648.91**；patient **975,241.20**（= v2b ×0.13）；
  - 一致性 all_pass；HTML 离线 PASS（exit 0 / 错误 0 / 面板渲染）；phase-3 **193 tests OK**（skip 28）。
- 边界：怪物 DR 的"独立乘法"应用方式尚未单独游戏内验证（assumed）；表值来自 tunklab 1.5.2，1.5.3 逐级一致性未验证；仍不是完整实战 DPS。
