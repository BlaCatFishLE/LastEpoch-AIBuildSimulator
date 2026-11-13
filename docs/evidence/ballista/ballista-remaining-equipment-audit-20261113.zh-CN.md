# Ballista 剩余暗金/特殊词条与 coverage 缺口核实（2026-11-13）

- 范围：当前构筑 `archive://examples/ballista-beta5-native-20261112`（calculation-input.corrected.json），只读核实；**未改** template / tools / knowledge / examples / reports，未生成新 run。
- 基线：`archive://reports/ballista-beta5-native-20261112/dps-ballista-armed-construction-5`，单发爆炸 390,345.23，horizon DPS 312,276.18（96 次事件，窗口 5/30/120s）。
- 关键基线数值：crit_chance = (0.05+0.16)×(1+2.86) = **0.8106**；crit_multiplier = 6.98；Dex = **228**（显式属性行 226 + quest all_attributes 2；由 attribute_scaling 值 9.12 = 228×0.04 反证）。
- 逐项结构化证据：`docs/evidence/ballista/ballista-remaining-equipment-audit-20261113.json`；模板 effect 草案：`.scratch/unique-audit/proposed-template-effects.json`。

## 分类计数

| 分类 | 数量 | 条目 |
| --- | --- | --- |
| **可补入（add）** | 2 | Phantom Grip corrupted per-Dex crit；Urzil's Pride corrupted +8 All Attributes（条件已满足） |
| **不适用（not_applicable）** | 14 | Hydra Arc 4% more Global Conditional Damage；Zeurial 两条 conditional Lightning Pen + Haste；Blood Roost Minion Physical Pen with Bleed；Urzil 回蓝描述；Hydra Arc 重复攻击描述（3 条）；Phantom Grip forged weapons 描述；Mourningfrost 抗性描述；Blood Roost/Hydra 的持续时间词条；Hydra 重复攻击隐藏 mod；idol Frenzy；Melvern Crossbow Mercenaries |
| **未解决（unresolved）** | 4 | idol_refraction（Impervious Altar 8 行）；Julra's Stardial 三倍窗口；被动 Agility per-movespeed 增伤；被动 Finesse Them per-crit-avoidance minion crit multi |
| **out_of_scope 复核** | 20 | 19 项确认不适用；1 项误标（Urzil +8 All Attributes）改判为可补入 |

## 可补入（2 项）

### A1. Phantom Grip：每点 Dexterity +1% increased Critical Strike Chance（corrupted affix）

- 物品/词条：`equipment.ring2` Phantom Grip（`item:letools:uniqueList.uniques:348`），`equipment.ring2#affix#corrupted#0.1`，property 98 / tags **427** / per = 每点 Dex，值 **0.01**（1%），corrupted prefix（affix `multiAffixes:1075`）。
- 证据链：
  - dataset：`#stats stat:letools:coreDB.playerPropertyList:427` = “Increased Critical Strike Chance per Dexterity”；`#affixes multiAffixes:1075` 第二行为 `{property:98, tags:427}`。
  - IL：`CharacterMutator.playerPropertyCritChancePerDex = 427`（CharacterMutator.cs:533）、`critChancePerDex` 字段 0x1934（:2241）；ISIL 中 `dex(SP=22) × critChancePerDex` 后以 SP=4 CriticalChance 创建 Stat（CharacterMutator.txt ISIL seq 11648–11692）；Perfect Aim 100% 复制玩家 SP=4 行（BallistaMutator.GetStatsFromPlayer ISIL seq 085–132）。
  - 注意：property 98 marker 的 `modifier_type=0` 不表示 added；dataset stat 名称与 `displayAddedAsPercentage=1` 表明 increased 语义。创建调用的目标 `0x180186BB0` 在 ISIL 无符号名，分桶没有被直接读出。
- 作用：**yes**（经 Perfect Aim 100% 复制给 Ballista）。
- 公式：`crit = clamp((0.05+0.16) × (1 + 2.86 + 0.01×Dex), 0, 1)`；Dex=228 → `0.21 × 6.14 = 1.2894` → **cap 1.0**。即使误读为 added（`(0.21+2.28) × 3.86 = 9.61`）也会被 cap 到 1.0，故 DPS 结论不敏感。
- 预期影响：crit 0.8106 → 1.0；avg_crit 5.847388 → 6.98；单发 390,345.23 → **465,953.30**（×1.19370）；horizon DPS → **372,762.64（+60,486，+19.37%）**。
- 模板草案：现有结构即可落盘（equipment_mod selector + `per_attribute` + `value_source: source_value` + `operation: increased`），见 proposed-template-effects.json 的 `phantom_grip_crit_chance_per_dexterity`。

### A2. Urzil's Pride：≥7 件 Corrupted non-Idol 装备时 +8 All Attributes（corrupted affix）

- 物品/词条：`equipment.chest` Urzil's Pride（`uniques:10`），`equipment.chest#affix#corrupted#0.0`，property 98 / tags **649**，值 **8**（flat），corrupted prefix。
- 条件已满足：当前输入带 corrupted affix 的非神像装备 **11 件**（head/chest/weapon1/weapon2/hands/waist/feet/ring2/ring1/amulet/relic；若把 `equipment.idol_altar` 计入则 12 件），均 ≥ 7。
- 证据链：
  - dataset：`#stats stat:letools:coreDB.playerPropertyList:649`；`#affixes singleAffixes:1011`。
  - IL：`CharacterMutator.addedAllAttributesWith7Corrupted`（0x1FD8）/`currentAddedAllAttributesWith7Corrupted`（0x1FDC）；ISIL 中 `count(corrupted && !isIdol) >= 7` 才保留该值，随后以 **SP=46 AllAttributes** 创建 Stat（CharacterMutator.txt ISIL seq 4361–4478）。当前流水线已支持 `all_attributes`（quest +2 就是通过它进入 Dex=228 的）。
- 作用：**yes**（AllAttributes → 每个基础属性 +8 → Dex 228 → **236**）。
- 公式：条件满足时 `Dex' = Dex + 8`；伤害链变化：
  - `attribute_scaling` increased：0.04×Dex → +0.32；
  - Armed Construction per-Dex more 组：9.12 → 9.44；
  - Mourningfrost：owner added +8（×Shared Enhancements 0.75 = +6 actor added，×2.5 effectiveness → Cold subtotal +15）。
- 预期影响：单发 390,345.23 → **418,794.42**（×1.07288）；horizon DPS → **335,035.53（+22,759，+7.29%）**。
- 模板草案：`stat: all_attributes / operation: added / value: 8` 已有流水线支持；但**条件类型需要最小新能力**（`equipped_item_count` 谓词），条件能力实现前不能直接粘入生效。见 proposed-template-effects.json 的 `urzil_all_attributes_with_7_corrupted_items`。
- **两项同时**：单发 499,912.96（×1.28069），horizon DPS **399,930.37（+87,654，+28.07%）**。

## 不适用（14 项，逐条一句话）

1. **Hydra Arc `4% more Global Conditional Damage`**（property 117，modifierType=2，specialTag **19** = `ConditionalDamageProperty.ToBleedingEnemies`）：玩家侧“对流血敌人”的条件 more 伤害；SP=117 不在 Shared Enhancements/Perfect Aim 转移白名单（白名单只有 SP 0/4/5/1/42/43），explosion 自身也不施加流血 → 不适用（若错误地按常驻加入会是 ×1.04）。
2. **Zeurial's Hunt：Lightning Bow Attack → 下一次 Throwing Attack +71% Lightning Pen**（playerPropertyList:229）：绑定玩家下一发投掷攻击，不是 minion explosion → 不适用。
3. **Zeurial's Hunt：Lightning Throwing Attack → 下一次 Bow Attack +71% Lightning Pen**（playerPropertyList:230）：同上，作用对象是玩家 bow attack → 不适用。
4. **Zeurial's Hunt：两个效果 2s 内都触发 → Haste 2s**（playerPropertyList:231）：玩家攻速/施法速度 buff；本口径事件率由场景控件驱动，不建模玩家攻速耦合 → 不适用。
5. **Blood Roost `26% Minion Physical Penetration with Bleed`**（property 43 / specialTag 2 / tags 8192）：Minion 标签成立，但条件“with Bleed”在当前口径没有流血来源（explosion 不施加流血；玩家 55% bleed 行 tags=0 不作用于 minion，且目标为不流血的 Boss Dummy）→ 不适用。上界：即使常驻满足且只作用于 Physical 分量（961.70/390,345.23 = 0.246%），26% pen 也只带来 **+0.064%** 总伤害。
6. **Urzil's Pride 描述：每 2% 未上限闪电抗 +1% 回蓝**（`Urzils_Pride.cs` manaRegenPercent=0.5）：资源/防御属性，不进入 DPS 口径 → 不适用。
7. **Hydra Arc 三条描述：Evade 后重复最近 bow attack（并随 Dex 75/135 增加到 2/3 个目标）**：玩家侧 Evade 行为；不改变 Ballista 数量/爆炸频率（Siege Engineering 来自 Explosive Trap 引爆，不是 bow attack）→ 不适用。
8. **Phantom Grip 描述：手套词条以 78% 作用于 Forged Weapons**（hidden mod tag 220 specialTag 6）：目标 actor 是 Forged Weapons，不是 Ballista → 不适用。
9. **Mourningfrost 描述：每点 Dex -1% physical/cold resistance**：纯防御属性；已在 `ballista-mourningfrost-20261113.json` 登记为未建模防御项 → 不适用。
10. **Blood Roost implicit：15% increased Bleed / Poison Duration**：异常持续时间；当前口径无 DoT 分量 → 不适用。
11. **Hydra Arc implicit：20% increased Poison Duration**：同上 → 不适用。
12. **Hydra Arc hidden mod：Evade 后重复 bow attack（property 98 tag 591）**：玩家侧行为标记 → 不适用。
13. **Large Umbral Omen Idol corrupted：Evade 后 3.3s Frenzy**（playerPropertyList:459）：玩家攻速 buff，条件触发 → 不适用。
14. **Melvern's Writ：+1 Crossbow Mercenaries**（playerPropertyList:75）：不同 minion 类型 → 不适用。

## 未解决（4 项）

### U1. idol_refraction：Impervious Altar 8 行（`mod:equipment.idol_altar#*`）

- 8 行（implicit 2 + normal affix 5 + corrupted 1，property 130）中只有 2 行是倍率：`48% increased Effect of Prefixes for Idols in Refracted Slots`、`29% increased Effect of Suffixes for Idols in Refracted Slots`；其余是 Ward / Health / Mana / Max Omen Idols / Weaver Idols 等非伤害量。
- 输入已显式登记假设：`inputs/idol-refraction-selection.json` → `refracted_idols: []`、`altar_prefix_effect: 0.0`、`altar_suffix_effect: 0.0`、`status: assumed`（`corrections.json#refraction` 同）。**按该假设当前所有神像词条 ×1.0，refraction 影响 = 0。**
- 为何仍 unresolved：真实存档是否使用 refracted slot、哪几个神像在其中，没有权威解析证据；若存在，伤害相关神像词条（如 crit multi / crit chance / cold pen / minion crit）会被 ×1.48（prefix）或 ×1.29（suffix），量级取决于具体词条。需要：存档中 idol slot 的 refracted 标志解析，或用户明确指定 selection_file。
- 实现建议：不要把 48%/29% 默认乘上；等输入层给出 refracted 神像集合后，按词条 slot 乘倍率再进入常规映射。

### U2. Julra's Stardial：使用移动/时间技能后该物品所有词条 ×3（4s，12s CD）

- 描述 + hidden mod（playerPropertyList:197）。该物品伤害相关词条只有 **Dex 22 点**（16 normal + 6 corrupted；Spell Damage 72% 是 Spell 标签，不适用 Ballista 爆炸；其余为 Mana/Ward/Dodge）。
- 当前模板不支持“条件时间窗口 + 召唤快照”，若加入需要 action/time 建模（4s/12s CD）。
- 若强行建模的**上界估计**（假设三倍包含 corrupted Dex、按 CD 触发、按 1/3 uptime 平均）：Dex 228→272（窗口内），窗口内单发 ×1.4435；1/3 uptime 平均 ×1.1362（≈ DPS 354,802.77）。**这是未确认假设，禁止当作 canonical。**
- 建议：保持 unresolved/limitation，不按 100% uptime 常驻化。

### U3. 被动 Agility（rg-1 node 8）的 per-movespeed 增伤（`passive:8:1`）

- dataset node 8 stats[1] = `{value:0.002, type:1(increased), property:0(Damage), per:[{stat:9, value:0.01, perPoint:true}]}`，5 点归一化值 0.01；翻译 `Skills.Skill_rg-1_8_1_Stat = "Increased Damage per 1% Increased Movement Speed"`。
- 当前失败原因：`per 缩放属性 9 未声明` —— 通用 `resolve_per_attribute` 仅支持 5 个基础属性，不支持任意 stat（stat 9 = Movespeed）。
- 通道：这是玩家 SP=0 increased damage 行，理论上可经 Shared Enhancements 0.75 转移给 minion；一旦实现可能有可见 DPS 影响，但**具体数值取决于 per 语义，不做猜测**。
- 一句话实现建议：把 per 缩放从“基础属性”泛化为“已声明 stat 池（movespeed / crit avoidance）”，并登记 planner per.stat 9/89 → stat_key。

### U4. 被动 Finesse Them（rg-1 node 106）的 minion crit multi per 3% crit avoidance（`passive:106:2`）

- dataset node 106 stats[2] = `{value:0.01, type:0(added), property:5(CritMultiplier), tags:8192(Minion), per:[{stat:89, value:0.03}], noScaling:true, threshold:5}`；翻译 `Minion Critical Multiplier per 3% Crit Avoidance`。
- 当前失败原因：`per 缩放属性 89 未声明`（stat 89 = Crit Avoidance，防御属性但被用作伤害缩放源）。
- 影响方向：minion crit_multiplier 提升 → avg_crit 直接乘；具体数值同样取决于 per 语义，**不做猜测**。
- 实现建议同 U3（同一通用能力）。

## out_of_scope 20 条复核

| # | id | 读取到的东西 | 结论 |
| --- | --- | --- | --- |
| 1 | `passive:8:0` | Agility｜property **50 = Haste On Hit Chance** | 维持不适用（同节点 `passive:8:1` 的 per-movespeed 增伤是 U3） |
| 2 | `mod:equipment.hands#implicit#1` | Bleed Duration | 不适用 |
| 3 | `mod:equipment.hands#implicit#2` | Poison Duration | 不适用 |
| 4 | `passive:50:1` | Glancing Blow Chance | 不适用（防御） |
| 5 | `passive:92:2` | Dodge Rating | 不适用（防御） |
| 6 | `passive:7:1` | Damage Taken While Moving | 不适用（防御） |
| 7 | `passive:7:0` | Dodge Rating | 不适用 |
| 8 | `passive:103:0` | property 43 specialTag 33 = Increased Haste Effect | 不适用（Haste 效果，玩家侧） |
| 9 | `passive:103:2` | Potion Slots | 不适用 |
| 10 | `passive:106:1` | Crit Avoidance | 维持不适用（同节点 `passive:106:2` 是 U4） |
| 11 | `passive:1:0` | Dodge Rating | 不适用 |
| 12 | `passive:1:1` | Poison Resistance | 不适用 |
| 13 | `mod:equipment.weapon1#implicit#2` | Poison Duration | 不适用 |
| 14 | `mod:equipment.weapon1#unique_mod#5` | Evade 后重复 bow attack（tag 591） | 不适用（玩家侧；不改变爆炸） |
| 15 | `mod:idols.4#affix#corrupted#0.0` | Frenzy after Evade | 不适用（玩家攻速） |
| 16 | `passive:28:0` | Dodge Rating | 不适用 |
| 17 | `mod:equipment.relic#unique_mod#0` | Crossbow Mercenaries | 不适用（不同 minion） |
| 18 | `passive:105:2` | Damage Taken While Moving | 不适用 |
| 19 | `passive:105:1` | Dodge Rating | 不适用 |
| 20 | `mod:equipment.chest#affix#corrupted#0.0` | **+8 All Attributes（≥7 corrupted non-idol）** | **误标 → 改判可补入（A2）** |

## 草案验证（只读，未写模板）

用模板副本调用 `le_calc.definitions_v3.validate_template` 与 `effect_binding.verify_mod`：

- A1 Phantom Grip：`validate_template` OK；`equipment_mod` selector 唯一定位到 `equipment.ring2#affix#corrupted#0.1`；`verify_mod` = verified（identity+semantics 通过）；`source_value=0.01`。**可直接落盘。**
- A2 Urzil：`validate_template` 也 OK，但 `condition` 键会被**静默忽略**（当前 effect 结构不读未知键）→ 直接粘贴会把条件效果变成常驻 +8，数值错误；因此在 condition 能力实现前保持 `blocked_on_capability`。selector/verify_mod 本身通过（`source_value=8`）。
- 验证脚本：`.scratch/unique-audit/validate_proposed_effects.py`、`validate_selectors.py`、`validate_drafts_full.py`。

## 建议实现顺序（给 Lead）

1. **先落 A1 Phantom Grip**：不需要新能力，`equipment_mod` selector + `per_attribute` + `source_value` 即可；落盘后跑一次正式 run，确认 crit_chance 到 1.0（cap）后 DPS ≈ 372,763。
2. **再落 A2 Urzil**：需要最小新能力“装备数量条件”（corrupted 非神像 ≥7）。实现后与 A1 一起复核：crit 1.0 + Dex 236 → DPS ≈ 399,930。
3. **idol_refraction**：保持 unresolved；等输入层能给出 refracted 神像集合再实现倍率，不要默认乘。
4. **per-stat 通用化（U3/U4）**：把 per 缩放从属性扩展到 stat 池（movespeed / crit avoidance）；这两项可能还有数个百分点 DPS，但在确认 per 语义前不要写数值。
5. **Julra 条件窗口**：保持 unresolved/limitation；除非做 action/time + 召唤快照能力，否则不要常驻化。

## 证据 locator 索引

- 输入：`archive://examples/ballista-beta5-native-20261112/inputs/calculation-input.corrected.json#mods equipment.ring2#affix#corrupted#0.1 / equipment.chest#affix#corrupted#0.0 / equipment.weapon1#affix#corrupted#0.1 / equipment.weapon2#unique_description#1 / equipment.hands#unique_mod#1`
- 折射假设：`archive://examples/ballista-beta5-native-20261112/inputs/idol-refraction-selection.json`、`inputs/corrections.json#refraction`
- dataset：`data/releases/phase1-dataset-003/data.sqlite#stats stat:letools:coreDB.playerPropertyList:427`、`...:649`、`...coreDB.propertyList:117`、`#affixes multiAffixes:1075`、`#affixes singleAffixes:1011`、`#affixes multiAffixes:983`、`#translations key=Properties.Property_Player_427_Name / _649_Name / Properties.Property_Master_Global Conditional Damage_AltText / Skills.Skill_rg-1_8_1_Stat / Skills.Skill_rg-1_106_2_Stat`
- IL：`.scratch/cpp2il_cs/DiffableCs/LE/CharacterMutator.cs#L533,L2241,L2621-L2622`；`.scratch/cpp2il_isil/IsilDump/LE/CharacterMutator.txt#ISIL seq 4361-4478, 11648-11692`；`.scratch/cpp2il_isil/IsilDump/LE/BallistaMutator.txt#ISIL seq 085-132`；`.scratch/cpp2il_cs/DiffableCs/LE/ConditionalDamageProperty.cs#L24`；`.scratch/cpp2il_cs/DiffableCs/LE/GlobalDamageConditionals.cs#L112-L131`；`.scratch/cpp2il_cs/DiffableCs/LE/SP.cs#L51,L103`；`.scratch/cpp2il_cs/DiffableCs/LE/Urzils_Pride.cs#L6-L10`
- 基线 run：`archive://reports/ballista-beta5-native-20261112/dps-ballista-armed-construction-5/result.json#report_details.components / effect_ledger.attribute_scaling / coverage.gaps`
- 重算脚本：`.scratch/unique-audit/estimate_dps.py`、`.scratch/unique-audit/estimate_dps2.py`、`.scratch/unique-audit/count_corrupted.py`

## 限制

- 所有 DPS 数值是 run-5 同一组件公式的重算估计，不是新 run 的 canonical 结果；未做游戏内实测。
- Phantom Grip 的 increased/added 分桶依据 stat 名称与显示元数据判定；两种读法在本构筑都会 cap 到 1.0，因此 DPS 结论不敏感。
- Urzil 条件计数口径（是否含 idol_altar）不影响“≥7 成立”。
- Hydra Arc 的残余 unknown：未实测玩家 global conditional 是否可能传播到 minion；当前按转移白名单/条件求值链判不适用。
- Julra 的 1/3 uptime 与“三倍包含 corrupted Dex”是未确认假设，只作上界估计。
