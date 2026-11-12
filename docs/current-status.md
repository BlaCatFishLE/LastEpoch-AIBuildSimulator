# 当前状态（current-status）

> 本页是公开版的能力摘要，不是导入前检查清单，也不复制日常操作规则（见 [workflow.md](workflow.md)、[AGENTS.md](../AGENTS.md)）。
> 版本：0.4.0（候选发布状态，2026-11-13）。数值与结论以当前案例输入、规则包和结果中的 `issues` / `coverage` 为准，不要从本文抄写数值。

## 1. 摘要

- 项目已能把用户的**离线存档**导入为独立构筑输入，匹配沉淀的机制知识，走统一的“计划 → 执行 → 结果 → 报告”链，生成离线 HTML 报告，并可在本机回环服务中做条件重算。
- **输入路径有两条，能力边界不同**：① 存档路径：clean-room 解析存档 → BD；若要自动生成 BuildSpec/calculation-input/snapshot 并 run 出 DPS/HTML（“完整首接”），需要用户自备可选的 Node + `lastepochsupertools v1.3.1`（不随仓库分发，缺失时明确 `needs_input`）；干净 clone 不能直接对任意存档出 DPS。② planner 路径：用户提供 Last Epoch Tools planner payload 的本地 JSON → BuildSpec（离线、不依赖 Node），再按手工流程补齐案例字段后 prepare/run。
- 现行计划/结果格式是 **plan-v4 / result-v6**；VC（Void Cleave）、FR（Flame Reave）、BH（Black Hole）三个 execution-v1 计算切片在作者本地完成过限定范围的签收验证。
- Ballista（Armed Construction 到期/销毁爆炸口径）为 **partial**：已接入统一 plan-v4 actor/派生事件能力，但玩家侧仍有未建模来源，`coverage` 保留缺口。
- 这是**有限技能范围**的构筑模拟工具：不是任意技能可用，也不是完整战斗模拟或实战 DPS；结果保留 `assumed` / `unresolved` / `coverage` 缺口。
- 项目由 AI 协作开发，结果按 **AS IS** 提供。

## 2. 支持矩阵

| 能力 | 状态 | 说明 |
|---|---|---|
| 离线存档解析为 BD（clean-room、只读） | 支持 | 只读解析；不支持云存档与非 v6 历史格式；需要用户自己的存档副本 |
| 存档 → 可计算报告（完整首接：BuildSpec/calculation-input/snapshot → run） | **需要可选外部工具** | 需要 Node + `lastepochsupertools v1.3.1`（不随仓库分发；`--tool-home` / `LE_TOOL_NATIVE_HOME`、`LE_NODE`）；缺失时 `needs_input`，不静默换路线 |
| planner payload（本地 JSON）→ BuildSpec → 计算 | 支持（进阶手工流程） | 不依赖 Node；`le_build import --file ... --share-id ... --offline` → export → 补齐案例字段 → prepare/run |
| 词缀库（`data/affix-db/v1`）按基底/位置修正 roll 范围 | 支持 | 数据来源自报游戏 1.5.2 |
| 构筑知识检索（`knowledge rebuild/search/show`） | 支持 | JSON 权威源 + 可重建 SQLite 索引 |
| Void Cleave / Flame Reave / Black Hole execution-v1 | 支持（限定范围） | plan-v4 / result-v6；各自覆盖缺口见结果与证据 |
| Ballista / Armed Construction | **partial** | 爆炸口径已接入；玩家侧未建模来源不阻断该口径，但不得当作完整技能支持 |
| 玩家离线 HTML 报告 + 本机条件重算 | 支持 | 页面只读结果 JSON，前端不重算伤害规则 |
| 其它技能、天赋树效果 | 不支持 | 可作为研究/条件来源登记，但没有可用 DPS 结论 |
| 组队 Buff、队友、法力消耗与恢复、完整循环/资源/操作序列 | 不支持 | 报告是限定来源、限定目标场景的模型 |
| 完整战斗模拟、任意构筑通用化 | 不支持 | 不承诺所有存档、所有技能可算 |

历史计划/结果版本（v1–v3 / result-v3–v5）保留为**兼容与历史边界**，不是当前默认输出格式；现行入口只应产生 plan-v4 / result-v6。规则包的现行版本以各案例 `case.json` 的绑定和 `tools/phase3/rules/` 中的 manifest 为准。

## 3. 数据与知识版本

| 项 | 版本/标识 | 位置 |
|---|---|---|
| dataset | `phase1-dataset-003`，目标游戏版本 **1.5.2**，`dataset_status=valid`，`partial=false` | `data/current.json`（指针）、`data/releases/phase1-dataset-003/`（Release 下载，大文件不入库） |
| dataset 文件指纹 | `file_sha256 = afa79e9fda0023b2ed09f4cbe8e07da436e0c74c03f34f80772da471c0adb8ab`；`logical_content_sha256 = 979d1820…` | `data/current.json` |
| 词缀库 | `data/affix-db/v1`（Tunklab 自报 1.5.2 的规范化结果；`affix-db.sqlite` 随仓库） | `data/affix-db/v1/normalized/` |
| 映射 | `data/mappings/crosswalk.json`、`data/mappings/game-build.json`、`data/mappings/version-compat.json` | `data/mappings/` |
| 知识 | `knowledge/` JSON 权威源 + `data/cache/knowledge.sqlite`（生成物，可重建） | `knowledge/`、`knowledge/INDEX.generated.md` |
| 游戏版本边界 | dataset 基于 1.5.2；部分逆向证据来自 1.5.3，文档分别标注，**不承诺跨版本等价** | [source-inventory.zh-CN.md](source-inventory.zh-CN.md) |

`data/reference/`、affix-db 的 `source-snapshot/`、原始来源快照、游戏本体与用户存档都**不随仓库分发**（见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)）。

## 4. 结果语义与验证状态

**结果语义**：

- `status=assumption` 表示结果包含显式假设，必须原样保留、不得当“已确认数值”。
- `issues` 记录未解决/未实现项；`coverage` 记录声明范围内的覆盖缺口（partial 不等于完整）。
- 完整性/指纹检查、来源检查、语义门禁分别证明“产物未变”“输入身份”“声明范围自洽”，三者互不替代；已有正式门禁与测试位于 `tools/**/tests/`。
- 规则状态（confirmed / derived / assumed / unresolved）与证据入口见 [mechanisms.md](mechanisms.md)。

**最近验证（作者本地，2026-11-13）**：Ballista Armed Construction partial 交付完成（统一 plan-v4 `v4-actor` 路径；结果 validate PASS、HTML 已生成）。这属于作者本地候选验证，**不是**干净 clone 上的公开发布验证。

**公开发布验证**：由发布者在干净 clone 上按 [README](../README.md)“快速开始”执行后填写，见 README 末尾的“验证状态”区块。当前候选树内：

- 代码、配置、测试与测试夹具保留；依赖作者本地案例/报告的测试在缺少数据时会显式 **SKIP**（不是失败）。
- `test --suite <name>` 按套件运行；完整 `--suite all` 不属于默认验证要求。
- 真实用户存档导入、从 GitHub Release 下载 dataset 这两项只能在发布后验证。

## 5. 已知限制与问题

| # | 限制 | 影响 |
|---|---|---|
| 1 | 完整首接依赖可选外部工具（Node + `lastepochsupertools v1.3.1`），不随仓库分发 | 干净 clone + dataset 不能直接对任意存档出 DPS；缺依赖时明确 `needs_input`，只能骨架导入/planner 手工流程 |
| 2 | 只有 VC/FR/BH 三个 execution-v1 切片 + Ballista partial | 其它技能只有数据接入/研究注册，没有 DPS 结论 |
| 3 | 结果是限定来源、限定目标场景的模型 | 不是实战 DPS；不要与游戏面板直接等同 |
| 4 | 结果含 assumed/unresolved/coverage 缺口 | 不能称“精确值”；缺口须随报告展示 |
| 5 | dataset 1.5.2 vs 部分逆向证据 1.5.3 | 机制结论与数值须分别标注版本 |
| 6 | Ballista partial | 玩家侧未建模来源存在；不得据此扩大为任意 Minion 技能支持 |
| 7 | 公开版不含作者本地案例/历史 run | 依赖这些数据的测试/复核会 SKIP；历史证据以 `docs/evidence/` 文本为准 |
| 8 | 平台限定 Windows 10/11 + PowerShell 5.1+ + Python 3.12+ | 其它平台/解释器未验证 |
| 9 | 项目由 AI 协作生成 | 可能存在错误，使用前自行复核 |
| 10 | 浏览器自动验收需要额外环境（如 Chromium 等） | 不属于普通计算依赖；未做完整网页矩阵 |

## 6. 如何确认某项能力

1. 先看本节支持矩阵与 [docs/contracts/](contracts/) 中的对应契约；
2. 再看 `knowledge/` 中该技能/机制条目的 `support.level` 与证据状态；
3. 最后在当前案例结果中核对 `coverage` / `issues` 与实际计算组件；
4. 不要用本文或历史文档中的数值当预期值——从当前输入重新运行得到的结果才是本轮事实。
