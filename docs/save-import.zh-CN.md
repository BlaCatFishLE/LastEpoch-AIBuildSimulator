# 离线存档导入（save → BD）格式与语义说明

本文记录 Last Epoch 离线存档（`1CHARACTERSLOT_BETA_*`、`STASH_*`）的结构、物品字节布局、
容器枚举、以及"从存档读出 BD"的判定规则与已知偏差，供 `tools/save_import` 维护与后续阶段使用。

验证对象：一份用于验证解码器的离线存档样本（24 字节头部 `EPOCH` + JSON，文件 sha256 见下），
放在本机游戏存档目录的副本上验证，典型占位路径 `<game-save-dir>\1CHARACTERSLOT_BETA_2`
（53,768 B，sha256 `0448f11302f955a2f96e3eca491c4718e0c434c3efbc57aadb64f4c860180813`）。
原始存档**不随仓库分发**；使用者请用自己的存档副本复现。

## 1. 容器格式

```
bytes[0..5)  = ASCII "EPOCH"
bytes[5..)   = UTF-8 JSON 文档（Cosmos/Mongo 风格：id/partitionKey/seqNo/_etag/version）
```

- 角色：`savedItems`（43 项；`{containerID, data:[bytes], formatVersion:2, inventoryPosition, quantity}`）、
  `savedCharacterTree`、`savedSkillTrees`、`savedWeaverTree`、`abilityBar`、`savedQuests`、
  `monolithRuns`、`blessingsDiscovered`、`factions` 等。
- 仓库：`STASH_*`（`savedItems`、`materialsList`、`wovenEchoesList`、`keysList` 等）。

## 2. 物品 `data` 字节布局（ItemData 序列化 v6）

`data[0] == 6`（`ItemData.currentDataSerialisationVersion`）。布局由游戏二进制（Unity IL2CPP 构建，**不随仓库分发**）的
`ItemData.setValuesFromSerialisation`（加载）与 `RebuildID`（写出）交叉恢复，并由
两个 MIT 解析器实现独立印证。绝对偏移：

```
[0]       版本 6
[1..4]    instance id（大端 4 字节；bit7 of [1] = duplicated，其余 31 位 = individualID）
[5]       baseTypeID（<101 为可装备类型；101/102/103/104/105/106/107/108/109 为
          碎片/符文/雕文/钥匙/记忆/共鸣/编织回声/袋子/透镜）
[6]       subTypeID
[7]       rarity（低 6 位）| giftingTargetCount << 6
[8]       flags：bit4 corrupted，bit5 ruined，bit6 requiredFaction，bits0-3 阵营等级，bit7 永不交易
[9..11]   implicit rolls（3 字节）
```

之后按稀有度分两种：

- **普通/魔法/稀有/尊崇**（rarity ∉ {7,8,9}）
  ```
  [12]    forgingPotential（低 6 位） | forgingPotentialType << 6
  [13]    affixCount（低 6 位） | sealedRegular<<7 | sealedFromCorruption<<6
  [14..]  词缀（每个 3 字节）
  ```
- **暗金/套装**（rarity 7/8）：`[12..13]` uniqueID 大端；`[14..21]` 8 字节 unique rolls；
  `[22]` affixCount<<5 | (legendaryPotential 或 weaversWill 低 5 位)；`[23..]` 词缀。
- **传说**（rarity 9，暗金+词缀）：`[12..13]` uniqueID；`[14..21]` unique rolls；
  `[22]` = affixCount（低 3 位）；若 corrupted 则 bit7 表示"最后一个词缀为腐化词缀"，
  否则 bits3.. 为 weaversWill；`[23..]` 词缀。

词缀 3 字节：

```
b0 = (tier0 << 4) | (affixId >> 8)    # affixId 为 12 位游戏词缀 id
b1 = affixId & 0xFF
b2 = roll（0..255；**255 = 满 roll**）
显示 T 级 = tier0 + 1；tier0 == 7（显示 T8）= primordial（原初）词缀
```

> **roll 取值规则（项目约定，重要）**：`roll == 255` 表示**满 roll**，计算/展示时取该 T 级词条的
> **范围上界（`roll_max`）**；`roll ∈ [0, 254]` 在 `[roll_min, roll_max]` 上线性映射
> （`min + (max-min)*roll/255`）。不要因为 255 就把它当“未解析 / 哨兵值”丢弃——本存档由编辑器
> 写成满 roll，phase-2 导出与 phase-3 计算都按上界使用（例：head implicit raw 255 → 77 = 范围最大值）。

非装备类型（101..109）使用各自的短 payload（碎片/符文/雕文/钥匙 7–8 字节等）；本存档中的
`Rune of Shattering` 为类型 102。类型 107（编织回声）在编辑器写出的存档里可能带通用装备布局，
解码器会同时报告"游戏读法"与"编辑器读法"（`editor_layout_guess`）。

## 3. 容器 ID（`ContainerID` 枚举，LE.dll，129 个成员）

| containerID | 名称 | 用途 / planner 槽位 |
|---:|---|---|
| 1 | INVENTORY | 背包 |
| 2–12 | EQ_HELMET, EQ_ARMOR, EQ_WEAPON, EQ_OFFHAND, EQ_GLOVES, EQ_BELT, EQ_BOOTS, EQ_RIGHTRING, EQ_LEFTRING, EQ_AMULET, EQ_RELIC | 装备；生成器工具映射：2 head, 3 chest, 4 weapon1, 5 weapon2, 6 hands, 7 waist, 8 feet, 9 ring2, 10 ring1, 11 amulet, 12 relic |
| 29 | IDOLS | 神像（8 格；位置 `inventoryPosition`，planner 坐标 = `x+1, 6-y-size.y`） |
| 33–39, 43–45 | BLESSING_0..6, BLESSING_7..9 | 10 个祝福槽 = 时间线 1..10 |
| 82–85 | NEMESIS_0..3 | 宿敌物品（不属于当前配装） |
| 91–96 | WEAVER_TREE_ITEM_NODE_* | 编织者树物品容器 |
| 123 | IDOLS_INVENTORY | 对应 planner 装备键 `idol_altar`（金字塔神像台） |
| 124–128 | RAGEOFMORDITAS_0..4 | 莫迪塔斯物品（不属于当前配装） |

## 4. 角色数据语义

- `savedCharacterTree`：`nodeIDs`/`nodePoints` 即法师被动树（Mage `mg-1`）节点与点数。
  `version=12` 是生成器工具写死的默认值，planner 侧树数据版本为 7（节点/点数一致，仅元数据不同）。
- `savedSkillTrees`：每棵专精树 `{treeID, slotNumber, version, xp, nodeIDs, nodePoints}`。
  **存档不保存技能等级**，只保存经验值；`xp=5,700,000` 是 20 级上限对应的 xp
  （游戏 `SpecialisedAbilityManager.getAbilityLevel(xp)` 用阈值表计算，基础等级上限 20）。
  planner 中的 24/26/28/38/29 为"有效等级 = 基础 20 + 装备/被动提供的 +Level of Skills"，
  该归因在存档侧不直接存在（仅能通过物品词缀近似重建）。
- `savedWeaverTree`：编织者天赋树（phase-2 导入范围外；本工具仍完整读出）。
- `abilityBar`：技能栏 5 项 id（与专精列表独立，可含未专精技能，例如 `te44` Teleport）。
- `savedQuests`：`SavedQuestHelper.StatefulQuestListToSavedQuests`（恢复的 IL）创建并写入
  QuestID/QuestStepID/分支/目标完成情况/trackStatus，但**从不写 `State`**，因此所有存档的
  `state` 恒为 0 = NotStarted。判定任务是否完成必须比较 `questStepID` 与任务清单的"完成步"，
  不能依赖 `state`。

## 5. 特殊词缀通道：编辑器写法 vs 游戏读法

本存档由生成器工具（`lastepochsupertools v1.3.1`，本地 Web 工具）写出。其写出的物品字节与
游戏加载器的通道语义在两处不同（决定游戏内实际显示，属于"编辑器 bug"而非解析错误）：

| 场景 | 编辑器/planner 意图 | 游戏加载器实际读法 |
|---|---|---|
| 普通稀有度物品，count byte bit7（0x80） | corrupted（腐化附加词缀，存于首位） | sealed regular（Glyph of Insight 封印词缀） |
| 普通稀有度物品，count byte bit6（0x40） | sealed（封印词缀，存于首位） | sealed from corruption |
| 传说（rarity 9）物品 `[22]` bit6（0x40，如 `0x42`） | sealed（planner `sealedAffix` 存于首位） | bits3.. = weaversWill（本存档读作 8），无 sealed 通道 |
| tier0 == 7（显示 T8） | primordial | 同：`hasSealedPrimordialAffix` |

因此 `bd.json` 每个词缀同时给出 `channel_editor`（生成器/planner 意图）与 `channel_game`
（游戏实际读法），并在 `channel_note` 中说明差异。对游戏内实际效果，应以 `channel_game` 为准；
与 planner 对照时用 `channel_editor`。

同时，生成器工具对"编辑器写入"的物品使用：
`instance id = 0x00007FFF`（32767）、implicit rolls `[255,255,255]`、词缀 roll `255`（=满 roll，取词条范围上界）。
所以**不能用 roll 原值**验证“随机 roll”解码正确性（但 255→上界的换算规则必须正确）；身份（base/sub/unique/affix id+tier）、unique rolls（ur）、
神像 roll（8/8 精确）才是有效验证面。

## 6. 已验证的一致性（本存档 vs planner `o3ZRm9my`）

| 项 | 结果 |
|---|---|
| 角色：等级/职业/专精 | 3/3 一致（100 / Mage / Sorcerer） |
| 法师被动树 | 19/19 节点、19/19 点数、113/113 点一致 |
| 5 棵专精树 | 5/5 树、61/61 节点、128/128 点一致 |
| 编织者树 | 22/22 节点、32/32 点一致（planner scope 外） |
| 技能栏 HUD | 5/5 一致 |
| 物品身份 | 装备 11/11（含 idol_altar）、神像 8/8、祝福 10/10、编织者树物品 5/5 |
| 神像词缀 id/T级/roll | 8/8 精确（含 corrupted 通道） |
| 装备词缀 id+T级 | 9/11 一致；差异来自编辑器未写 hands 的原初词缀 1144 与 relic 的 72 词缀 |
| unique rolls（ur） | 4/4 可比对暗金一致；weapon1 为满 roll 255（按范围上界） |
| 任务 | planner completedQuests 20 条中 19 条在存档出现（planner-only=158）；战役任务 33/36 达到完成步 |

## 7. 已知限制

1. 编辑器生成物品的 roll 字节是满 roll（255）：按约定取词条范围上界；不能当作“随机到的原值”。
2. 特殊词缀通道按上表区分；以游戏内显示为准时使用 `channel_game`。
3. `idol_altar` / 编织者树物品的容器语义来自枚举 + 生成器工具对照（`123`、`91–96`）。
4. 编织回声（类型 107）通用 payload、`state=0`、技能等级归因等已知偏差在解码报告中逐项标注
   （作者本地记录，不随公开仓库分发）。
5. 未覆盖：在线云存档、非 v6 历史存档、商店/交易等运行时容器。

## 8. 交叉验证与与 phase-2 importer 的衔接

### 8.1 生成器工具原生代码交叉验证（离线执行）

把生成器工具（`lastepochsupertools v1.3.1`，本地 Web 工具，**不随仓库分发**）的读档/解码函数在 Node.js `vm`
沙箱中离线执行（工具与存档均只读、不联网），得到权威对照：

| 检查 | 结果 |
|---|---|
| `pvmDecodeItem` 对 43 件物品 | 42/43（唯一 null 为 cid=128 的 7 字节钥匙/材料，与 clean-room 解码器的非装备例外一致） |
| 生成器 id encoder 自检 | 4/4 精确（`UAwJgzGCMBsQ` 等 planner id 重新编码一致） |
| tool wire vs planner `o3ZRm9my` | 装备 id 11/11、神像全签名 8/8 + x/y 8/8、祝福 10/10、编织者 5/5、charTree 19/19、skillTrees selected 5/5 全等、weaverTree 22/22、HUD/bio 全等 |
| 读→写回环 | `encode(decode(data))` 24/42 字节一致；18 处差异仅为 encoder 硬编码字段（instance-id 与 forging potential），无其他字节差异 |
| 特殊词缀位 | 工具代码确认 `+64 = sealed`、`+128 = corrupted`；primordial 无独立通道 |

交叉验证记录（作者本地）：tool-native 对照与 wire 导出样例，不随公开仓库分发。

### 8.2 与 BuildSpec 导入链的衔接（可选集成路径）

工具原生 wire 即 lastepochtools 的 `data` payload；把 `blessings[].ir` 从标量 `0` 规范化为
3 字节数组后，BuildSpec 导入链可在临时数据根发布 revision 并通过结构校验（identity 计数与解码结果一致）。

该路径依赖本地生成器工具，未纳入 `tools/save_import` 的 clean-room 主链路；仅作为研究/对照路径保留，
不属于公开版的必需流程。
