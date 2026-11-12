# 来源清单（source inventory）

日期：2026-11-01。本文记录第一阶段实际调查并使用的数据来源、载体、版本证据、可取得的数据与已知限制。所有计数来自当次运行产物（作者本地运行记录与数据库；运行产物不随公开仓库分发），不是站点首页宣传数。

> 再分发边界：本文列出的第三方站点原始抓取快照、`data/reference/` 参考库、游戏本体与游戏资源均**不随公开仓库分发**；
> 随仓库分发的只有规范化结果（dataset 通过 Release 附件，affix-db 规范化库在仓库内）。详见
> [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

## 0. 调查环境

| 项目 | 结论 |
|---|---|
| 项目根 | `<repo>`（仓库根；本文路径均相对仓库根） |
| 解释器 | 项目 `.venv` 的 Python **3.12**（创建步骤见 [README.md](../README.md) 环境一节）；统一入口按 `config/project.json` 候选 → `LE_PYTHON` → PATH 查找 |
| SQLite | Python 3.12 标准库内置 |
| 网络 | 两站 2026-11-01 均直接 HTTP 200 可访问；LE Tools 静态数据未被 Cloudflare 拦截，Tunklab 无验证页 |
| 工具依赖 | 普通计算不依赖 node/git/rg。本地游戏资源研究可选 UnityPy（研究依赖，不随仓库捆绑） |
| 版本边界 | 两站都声称游戏 1.5.2（Tunklab 首页 / LE Tools `window.dataVersion='version152'`）；本地安装为 **1.5.3**（Steam buildid 26087601）。本地资源不并入 1.5.2 dataset，只作缺口研究证据 |

## 1. Last Epoch Tools（主来源，A/B 域结构化数据）

- 基址：`https://www.lastepochtools.com`
- 载体：Next.js 页面 + 版本化静态数据脚本 `/data/version{N}/<area>/js/<hash>.js`，内容是 `window.<X>={...}` 形式的纯字面量；另有两个 JSON 翻译文件。
- 发现方式：抓 `skills/`、`db/`、`planner/` 入口页 → 读 `window['dataVersion']='version152'` → 从页面 `<script src>` 提取当次真实文件名（文件名每次部署会变，禁止硬编码）。
- 版本证据：`window['dataVersion']='version152'`（页面内联脚本），与站点 1.5.2 对应；每个资源记录 URL 与 SHA-256。

### 实际数据包与内容

| 资源（role） | 内容 | 计数 |
|---|---|---|
| `planner/js/<hash>.js` | `window.coreDB`（属性/核心属性/技能属性）、`window.LEAbilities`（技能、异常、召唤物、触发关系）、`classInfoForSources`、各类 *Sources 映射 | ability 1,228；ailment 148；minion 96；property 1,040；mastery 20；class 5 |
| `skills/js/<hash>.js` | `window.LESkillTrees`、`LESkillTreesUI`、`LECharTrees`、`LEWeaverTree`、`LECharTreesUI`、`LETooltipIcons` | 技能树 136（节点 3,956）；职业被动树 5（节点 541）；编织树 1（节点 80）；UI 布局 142 |
| `skills/js/<hash2>.js` | `window.skillTreeDiffs / passiveTreeDiffs / abilityDiffs / ailmentDiffs` 历史 diff | 仅作版本证据，未展开入库（raw 保留） |
| `db/js/<hash>.js` | `window.itemDB`（itemList 基底 1,558、affixList 1,156、uniqueList 486、setBonusList 25、rel* 关系） | item_base 1,558；affix 1,156；unique 486 |
| `endgame/js/...`、`factions/js/...`、`checklist/js/...` | 终局/阵营/清单配置 | generic/配置，部分入库 |
| `data/version152/i18n/full/en.json` | 游戏内文本（名称/描述/属性名）39,400+ 键 | 用于把 nameKey 解析成英文名/描述 |
| `static_data/i18n/en.json` | 站点 UI 文本 | 辅助 |
| `page_skills/db/planner` | 入口 HTML | 版本与脚本发现证据 |

关键：LE Tools 的实体数据里大多是 `nameKey`（如 `Abilities.Ability_HungeringSouls_Name`），必须配合 i18n 文件才能得到显示名；`name` 字段仅部分新数据包已内联。解析器为此新增 `translation` 来源记录（39,400 条），规范化时统一解析。

### 解析结果（run `w1-letools`，snapshot `letools-20261101`）

- 记录 53,855 条（含 39,400 条翻译）：ability 1,228、skill_tree_node 3,956、skill_tree 136、affix 1,156、item_base 1,558、unique 486、buff 264、ailment 148、property 1,040、minion_entity 96、passive_node 541、weaver_node 80、mastery 20、class 5、relation_candidate 3,266 等。
- `UNSUPPORTED` JS 构造：0（jsdata 覆盖本快照全部数据形态）。
- 离线重解析两次字节一致（见适配器报告）。
- 已解析但未展开的：历史 diff（`version_history`）、若干 config 窗口 → 记 info issue，raw 保留。

### 已知限制

- 技能冷却时间在数据包中没有直接字段（只有 `maxCharges/chargesGainedPerSecond`），规范值为 NULL + absent evidence。
- 技能树节点坐标只在 `*UI` 模块，需二次关联；节点 `stats` 的 `value` 是显示文本（如 `-10%`、`+1`），规范化为比率/数值并保留原文与单位。
- LE Tools 的 player ability 列表（182）与 abilityList（1,228，含子技能/怪物技能）不同；`skill_type` 只能按标志位推断，推断状态已记录。

## 2. Tunklab（主来源，关系与描述补充）

- 基址：`https://lastepoch.tunklab.com`
- 载体：Next.js(Turbopack) 客户端 JS chunk。数据以 `e.s(["f",0,{version:"1.5.2",data:...}])` 模块或页面级 `let c={"mg-1":[...]}` 嵌入；无 JSON API。
- 发现方式：抓 5 个入口页（`/`、`/skillsearch`、`/buffs`、`/ailments`、`/class/mage`），解析 HTML 与 TURBOPACK 注册表得到当次 chunk 列表，再按 payload 形状分类（chunk 文件名每次部署变化）。
- 版本证据：首页 `Game Version: 1.5.2`；各数据模块自报 version（1.5.2 / 1.5-preview1 / 1.3.6 / 无包装），逐模块保留，不作为冲突。

### 页面 ↔ 数据 chunk（当次）

| 页面 | 关键 chunk | 内容 |
|---|---|---|
| `/` | `0430-8cl4viex.js`（7.9MB） | ability 4,350、affix 1,156、unique 487、class 5、mastery 20、ailment、buff、property 113、item_type 47、loot/monster/地牢 |
| `/` | `3qurytohg5ynr.js` | cache/quest/monolith_mod 等 |
| `/skillsearch` | `1xbwm4iifo2k2.js` | 137 技能树、3,956 技能节点 |
| `/buffs` | `0svsdish-g6fo.js` | 264 buff |
| `/ailments` | 复用 `0430` 模块 | 147 ailment |
| `/class/mage` | `0_gctk-sux77f.js` / `0zaj6myudbu94.js` | 5 棵职业被动树 + 541 节点；25 套装 |

### 解析结果（run `w1-tunklab`，snapshot `tunklab-20261101`）

- 记录 28,478 条：relation 14,897、ability 4,350、skill_tree_node 3,956、affix 1,156、monster 884、property_text 713、passive_tree_node 541、unique_item 487、monolith_mod 290、buff 264、ailment 147、skill_tree 137、property 113、item_set 25、item_type 47、class 5、mastery 20、quest 139、timeline 10、dungeon 3 等。
- 关系类型（原始）：requires_node 4,777、has_tree 3,456、modifies_stat 2,932、triggers_skill 1,913、modifies_skill 806、applies_ailment 572、summons_minion 277、belongs_to_class 56、belongs_to_mastery 80 等。
- 离线重跑两次（含与在线 run）JSONL 字节一致；Fireball/树/节点/Buff/Ignite/词缀/职业/被动节点逐字段与原始 chunk 一致；`parse_failures=0`。
- 站点交叉核对：`/buffs` 264 行、`/ailments` 147 行（row key = 状态 id）、Skill Nodes 3,956、Affixes 1,156、Skills 136 与模块计数一致。

### 已知限制

- Buff 数据集没有 duration 字段（`noFixedDuration` 只表示无固定时长）；aliment/buff 均可能缺生命周期细节 → 规范值 NULL + absent evidence。
- 节点无坐标/布局。
- 技能标签位掩码语义、`damage.damage` 七元组顺序、`speed/charges/applyPrefix` 语义未完全确认，规范层保留 raw 与推断标注。
- property id >133 的技能专属属性无字典（389 条 modifies_stat 未解析）。
- Unique Items 站点计数 463 与模块 487、Base Items 698 与 1,570 为口径差异，已记 info issue。

### 2.1 本地异常状态参考库（phase-3 补充，2026-11-06）

条件 Buff 研究需要 ailment 的**说明文本/每层效果/Boss 玩家效果差异**，而冻结的 `phase1-dataset-003` 只保留
内部字段（内部名、duration、max_instances、buff 数值、一行 description），没有站点上的完整 Effects 区块。
因此 phase-3 额外抓取一份**独立参考库**（不修改 dataset，不进入 DPS 输入）：

- 路径：`data/reference/tunklab/ailments.sqlite`（+ `ailments.json`、`README.md`；原始抓取工作区仅为作者本地归档，
  **`data/reference/` 不随公开仓库分发**）；另有 `ability-glyph-of-dominion.json`（Glyph 能力参考，来自本地 catalog +
  归档翻译 + Tunklab 能力页链接）。
- 来源：`https://lastepoch.tunklab.com/ailments`（147 个 `/ailment/<slug>` 页面，147/147 抓取解析成功）；
  `https://www.lastepochtools.com/ailments/fire_res_shred` 作为备选参考链接（Cloudflare 对非浏览器请求 403，
  未取正文；数值与本地 catalog/Tunklab 交叉核对）。
- 内容：147 ailments（name/kind/description/duration/max stacks/Boss 玩家效果/internal name/in-game name/
  ailment id/source URL/抓取时间/HTML sha256），186 条 effect lines，552 条 attributes（含 Unlimited/Permanent
  原文），并按 `ailmentList` id + internalName 与 `phase1-dataset-003` 的 `ailments` 表 147/147 匹配。
- 用途：条件效果研究证据（例如 Apex of Thought 的 Fire Resistance Shred：4s、10 层、每层 -5% 火抗、
  对 Boss/玩家 60% 效果）；DPS 引擎不直接读取该参考库。
- 边界：第三方粉丝站转写，未绑定游戏版本；load-bearing 前需回到游戏/catalog 核对。见该目录 README。

## 3. 跨源合并（crosswalk）

- 文件：`data/mappings/crosswalk.json`（由 `tools/ingest/scripts/build_crosswalk.py` 生成，可审计、可重建）。
- 生成规则：必须至少两个独立信号（共享游戏 ID + 名称匹配 / 数字索引匹配 / 词缀 tier 结构一致 / 暗金基底与词缀数一致 / 职业作用域一致）；ID 相同本身不作为证据。
- 结果（2026-11-01）：6,890 个 canonical 实体、7,081 个 Tunklab 成员被合并：
  - 技能树 141、技能节点 4,497（共享 treeId/nodeId）
  - ability 365（Tunklab `playerAbilityID` = LE Tools ability id，且名称或数字 index 匹配）
  - buff 264（id + 名称）、ailment 147（id + 名称）、affix 1,156（id + 8 tier 结构一致）、unique 486（id + 基底/子类型/词缀数/等级一致）、class 5、mastery 20
- 未合并：Tunklab 无对应 ID 的 898 个 ability、id 匹配但无第二信号的 1,221 个 ability、1,866 个 playerAbilityID 不在 LE Tools abilityList 的记录、1 个空树 key；均保留为 Tunklab 侧独立实体并写入报告，不删除。
- 两站重复展示同一底层数据的情况在 `field_evidence` 中分来源记录，不视为两份独立实测证据。

## 4. 本地游戏资源（缺口研究来源，非 1.5.2 dataset 输入）

- 安装：`<game-install>`（Last Epoch 安装目录；只读，未修改。**游戏本体与资源不随仓库分发**）
- 版本证据：Steam `appmanifest_899770.acf` buildid **26087601**，LastUpdated 2026-10-22T11:59:44Z；PlayerSettings.bundleVersion **1.5.3**；Unity **6000.4.8f1**
- 关键文件：`GameAssembly.dll` 97,256,960 B（sha256 `323E9183…A91464`）、`global-metadata.dat` 36,457,932 B（`2A4E8440…4F83`）、`resources.assets` 101,962,560 B（`F959D005…CC47`）
- 工具：UnityPy 1.25.4（项目 `.venv`）。`StreamingAssets/LEAssetBundles/*.bundle` 内嵌序列化 type tree，MonoBehaviour 字段可直接读；player build 的 `*.assets`/`globalgamemanagers` 无 type tree，MonoBehaviour 字段不可读；`PlayerSettings` 严格读取在 Unity 6 上差 4 字节（`check_read=False` 可读版本）。
- 实测可取得：`PermaLoad.bundle` 77,095 个 MonoBehaviour / 1,182 类，其中 Ability 4,350、CharacterClass 5、Ailment 149、BuffParent 173、EffectDataSO 17、AffixList（616 single + 540 multi）；扫描 17,151/26,638 bundle 定位到 `SkillTreeData`（140 棵树，含 "Fireball Skill Tree"）与 `SkillTreeNode`（样本 "Poison Tipped" id=13/maxPoints=8/stats 完整）。
- 版本结论：**本地 1.5.3 ≠ 网站 1.5.2**，不并入 1.5.2 dataset；只作字段形状/缺口研究证据。详见作者本地 discovery 记录（不随公开仓库分发）。
- 未完成：`LEAssetBundles/Catalog.bin`（LABC v4）未解析，bundle→资产名映射缺失，目前靠类名扫描；扫描覆盖 64.4%。

## 5. 开源参考项目（仅代码参考，未复用数据）

- `Musholic/LastEpochPlanner`、`uta666XYZ/LastEpochBuilding`：仅查阅公开说明以校准字段与覆盖范围；本阶段未拉取、未复用其代码或数据；其技能/Buff 支持范围不作为本项目的覆盖上限。
- 两站数据均为站点公开静态资源；本项目的提取与规范化仅用于本地构建与验证，第三方原始快照不随仓库分发。

## 6. 来源状态汇总

| 来源 | 自动发现 | 自动抓取 | 离线解析 | 规范化 | 状态 |
|---|---|---|---|---|---|
| letools | 是（页面→dataVersion→脚本清单） | 是（缓存/304/重试） | 是 | 是 | OK |
| tunklab | 是（页面→chunk 清单→payload 分类） | 是 | 是 | 是 | OK |
| game（1.5.3） | 只读扫描 | 手动/脚本一次性 | 部分 | 否（版本不并入） | PARTIAL（缺口研究） |
| 开源项目 | 仅参考 | 否 | — | — | 未使用 |

## 7. 当前发布状态（公开版）

- 当前 dataset：`phase1-dataset-003`（目标游戏 1.5.2 / Season 5，`dataset_status=valid`、`partial=false`），指针为 `data/current.json`。
- 分发方式：`data/releases/phase1-dataset-003/data.sqlite`（约 251 MB，file sha256 `afa79e9f…`）作为 GitHub Release 附件分发，**不入 git 正文**；同目录的 `manifest.json` 随仓库分发，规模/校验结果以 manifest 与数据库本身为准。
- 词缀库：`data/affix-db/v1/normalized/affix-db.sqlite` 随仓库分发（规范化结果）；其 `source-snapshot/` 与 `data/reference/` 不分发。
- 历史 dataset（`-001` / `-002`）与历史发布报告仅保留在作者本地，不随公开仓库分发；文档不再以它们作为当前基线。
- 离线重建 dataset 需要原始来源快照（不随仓库分发）；普通使用者只需下载 Release 附件即可运行。
- 数据来源、第三方文本与商标边界见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。
