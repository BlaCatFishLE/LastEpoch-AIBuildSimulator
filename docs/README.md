# docs 索引

本目录是公开仓库的文档入口。新存档先按仓库根 [AGENTS.md](../AGENTS.md) 的日常顺序导入，再按当前构筑匹配知识；下列资料按需读取，不需要在导入前通读。

## 1. 现行文档（优先）

| 文件 | 作用 |
|---|---|
| [current-status.md](current-status.md) | 当前能力、支持矩阵、限制、dataset/词缀库版本与最近验证状态 |
| [workflow.md](workflow.md) | 从导入到报告的操作流程；新机制接入与最小语义验证规则 |
| [architecture.md](architecture.md) | 模块职责、数据流、格式契约与绑定/不可变规则 |
| [version-control.md](version-control.md) | 跟踪与忽略策略、不可变数据、提交/tag/Release 流程、发布检查清单 |
| [../tools/bd_editor/README.zh-CN.md](../tools/bd_editor/README.zh-CN.md) | 本机 BD 编辑器：启动、配置、条件与计算边界 |
| [mechanisms.md](mechanisms.md) | 机制证据索引与登记规程（哪些是 confirmed / assumed / unresolved） |
| [rea-guide.zh-CN.md](rea-guide.zh-CN.md) | 可选的逆向研究工具链（REA/Node/Ghidra/JDK）；工具不随仓库捆绑 |
| [data-dictionary.zh-CN.md](data-dictionary.zh-CN.md) | 规范化 dataset 的 schema 语义、值状态与单位约定 |
| [save-import.zh-CN.md](save-import.zh-CN.md) | 离线存档容器与 `ItemData` v6 字节布局、通道语义、已知偏差 |
| [affix-db.zh-CN.md](affix-db.zh-CN.md) | 词缀 × 基底 × 位置 roll 范围库的用法、公式与取整策略 |
| [source-inventory.zh-CN.md](source-inventory.zh-CN.md) | 数据来源、载体、版本证据与再分发边界 |
| [architecture-decisions.zh-CN.md](architecture-decisions.zh-CN.md) | ADR-001…009（数据层关键决策） |

## 2. 契约（字段/流程的权威定义）

| 文件 | 内容 |
|---|---|
| [contracts/bd-editor-player-projection.md](contracts/bd-editor-player-projection.md) | 编辑器玩家投影与校验：只读快照、完整装备词条、范围与名称显示 |
| [contracts/save-onboarding.md](contracts/save-onboarding.md) | 新存档正式接入：来源冻结、BuildSpec 发布、prepare 与报告接入 |
| [contracts/workflow-interface.md](contracts/workflow-interface.md) | 流水线工作流接口：阶段执行/领域查询/研究交接/提交恢复 |
| [contracts/component-knowledge-v1.md](contracts/component-knowledge-v1.md) | 构筑组件稳定 ID 与共享知识匹配、当前实例绑定 |
| [contracts/calculation-route-v1.md](contracts/calculation-route-v1.md) | 计算路由、compile_plan 阶段图、退出码、缓存与 legacy 边界 |
| [contracts/calculation-plan-v1.md](contracts/calculation-plan-v1.md) | 统一计算计划 v1：编译/执行边界与计划字段 |
| [contracts/calculation-plan-v2.md](contracts/calculation-plan-v2.md) | 计划 v2 / 结果 v4：多命中组件、具名频率与场景评估 |
| [contracts/calculation-plan-v3.md](contracts/calculation-plan-v3.md) | 计划 v3 / 结果 v5：纯 DoT 组件、调度与多实例 |
| [contracts/calculation-plan-v4.md](contracts/calculation-plan-v4.md) | 现行计划 v4 / 结果 v6：受限通用能力、显式 target_side、applications/charge |
| [contracts/definitions-v3.md](contracts/definitions-v3.md) | 可执行技能定义 v3：selector 绑定、FrozenContext 与错误边界 |
| [contracts/conditional-interface.md](contracts/conditional-interface.md) | 条件 Buff 配置阶段接口：效果/状态/场景/求值/展示 |
| [contracts/calc-channel.md](contracts/calc-channel.md) | 跨技能 Buff 输入通道（条件重算）实现契约 |
| [contracts/player-report-ui.md](contracts/player-report-ui.md) | 玩家 HTML 与本地重算契约；**§6 为新存档/换 agent/共享工程的统一质量基线** |

## 3. 证据

| 位置 | 内容 |
|---|---|
| [evidence/](evidence/) | 随仓库分发的可复用文本证据（逆向结论、机制证据、数据证据） |
| `evidence/extracted/` | 从作者本地归档提取的可复用证据条目；原文用 `archive://` 引用（目录索引 `_index.json`） |
| `evidence/reverse-engineering/` | 只读逆向进展与结论 |
| `docs/mechanisms.md` | 机制证据分类、状态与入口索引 |

约定：`docs/evidence/**` 只追加新证据，不改写旧证据数值；作者本地的原始截图/大文件/历史报告不随公开仓库分发，需要时以 `archive://` 前缀引用。证据与规则的状态区分见 [mechanisms.md](mechanisms.md)。

## 4. 不随公开仓库分发的内容

为减小仓库体积并避免分发第三方/个人数据，以下内容不在公开仓库中（文档若引用会显式标注）：

- 历史阶段计划/交接/审查/验收记录、文件迁移与清点元数据（作者本地归档）；
- 作者本地案例输入、历史 run 产物与截图（`examples/`、`reports/` 属用户本地目录，已 gitignore）；
- `data/reference/`、affix-db 的 `source-snapshot/`、原始来源快照与游戏资源；
- 第三方站点原始 JS/页面缓存；
- 用户原始存档。

## 5. 按角色选读

- **第一次接手（人或 AI）**：仓库根 [README.md](../README.md) → [AGENTS.md](../AGENTS.md) → [current-status.md](current-status.md) 的支持范围 → [workflow.md](workflow.md)。
- **只想跑出报告**：[README.md](../README.md) 快速开始 → [workflow.md](workflow.md) 操作入口。
- **要改计算/加技能**：[architecture.md](architecture.md) → [mechanisms.md](mechanisms.md) → [workflow.md](workflow.md) 的“新机制接入与最小语义验证”。
- **要查某数值凭什么**：[mechanisms.md](mechanisms.md) → 对应规则包或 `docs/evidence/` 证据。
- **要改页面/重算**：[contracts/player-report-ui.md](contracts/player-report-ui.md)。
- **要发布/交接**：仓库根 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) → [version-control.md](version-control.md)。
