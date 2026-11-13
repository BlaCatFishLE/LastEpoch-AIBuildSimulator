# AGENTS.md — AI 工作入口（公开版）

本文件是 AI agent / 维护者在本仓库工作的入口。项目由 AI 协作开发，结果按 **AS IS** 提供；不要把它当作游戏官方数据。先读本文件，需要字段或实现细节时再按“契约导航”查 [docs/](docs/README.md)，不要先读历史产物。

## 本机 BD 编辑器（v0.4.2 新增）

- 启动：`\scripts\le.ps1 editor --port 8766`，浏览器打开 `http://127.0.0.1:8766/`。
- 用途：导入 BuildSpec / 编辑器导出 JSON，复制方案并调整装备、词缀、完整被动、当前职业专精、神像和祝福；在“条件与 Buff”应用主技能与场景后点“计算当前配置”。
- 计算复用统一 compile → simulate → validate → render 链与共享报告；A 复用计划 / B 重新绑定 / C 研究任务按输入变化分流，不另建技能伤害模型。
- 本机工作区 `data/editor-workspace/` 已被 `.gitignore` 排除；详情见 [tools/bd_editor/README.zh-CN.md](tools/bd_editor/README.zh-CN.md) 与 [编辑器玩家投影契约](docs/contracts/bd-editor-player-projection.md)。

## 日常执行顺序（用户要求）

1. 先冻结、解析用户本次指定的存档，生成独立 BuildSpec 与 calculation-input；已有 BuildSpec/编辑器配置可直接在 `le.ps1 editor` 中导入调整。只检查当前输入和解码必需依赖；不要运行默认案例 doctor，也不要在导入前完整检查任何旧案例。
2. 导入后按当前构筑的技能、节点、被动、装备、神像、祝福及词条稳定 ID 查询沉淀知识，绑定当前实例的数值和点数。只按命中记录追溯必要证据；历史案例没有默认优先级。
3. 复用统一 DPS 流程的属性、乘区、暴击、目标、时间与报告组件。hit/DoT 仅表示击中与持续伤害的计算组件差异；技能行为由数据和规则表达，不能按技能建立独立 DPS 模型。
4. 优先生成当前 BD 的结果和网页；区分研究/候选产物与可用于当前声明范围的计算结果。机制未知可以显式假设或缩小范围继续；已知来源、执行或账本错误不能用 partial 掩盖，也不能据此更新可用 canonical。
5. 复用已验证能力且未改变语义时，默认不追加测试阶段。新增或修改计算、绑定、条件、投影或门禁语义时，按受影响边界做少量正例/反例；既有同边界证据可复用，不按技能数或词条数铺测试。候选网页可先生成，相关最小检查在宣称该范围可用前完成。完整回归仍须用户单独明确要求。

所有历史案例地位相同，均为以前使用或建立流程留下的实例。案例名不决定默认输入、必读材料、知识检索顺序、回归基线或交付门槛。“走全套流程”不等于授权全仓测试。

## 新机制与计算正确性的边界

- 允许新技能先以有限范围、明确场景假设或估计区间出结果；只有影响核心口径、无法合理界定的选择才需要询问用户，普通可逆建模决策由 agent 推进并记录依据。
- 区分三件事：游戏机制证据、代码对声明语义的实现、当前产物验证。软件自洽不等于游戏确认；机制未知也不豁免来源身份、类型/作用域隔离、条件求值和账本一致性。
- 只对本轮新增/改变的边界选最小正反例，并接一次当前真实输入链；不把合成算子通过、哈希一致或测试数量当作端到端证明。已有适用证据直接复用。
- 发现实现错误时修复或隔离受影响范围，保留可用部分与研究产物；不把错误数值作为 canonical，也不要求无关机制一并做完。
- 完整操作规则集中在 [docs/workflow.md](docs/workflow.md)“新机制接入与最小语义验证”；这是流程要求，不表示相关代码门禁已全部实现。

## 知识收尾（结果先交付，任务结束前完成）

本轮新增、修正或临时写进案例 profile 的可复用机制，完成实现后必须收尾：先查已有条目，复用或更新；没有则在 `knowledge/mechanisms/` 登记稳定 ID、当前参数绑定、规则/处理器与证据入口，并把通用规则从案例实例配置中分离。确属案例私有的输入可保留在案例中，但要写明不能共享的原因。普通属性已有映射时不强制逐行新建机制文章。

证据不完整可登记为 assumed/unresolved，尚未实现可登记 draft/not_implemented；不能把“没有全部实测”“没跑回归”或“先输出网页”当作不登记的理由。实现可复用与证据确认是两个独立维度，不得为入库伪造 validated 状态。

最小收尾只需：条目随正常加载可读、重建知识索引、本轮代表性稳定 ID 能命中。不要重跑导入、计算、网页或全套测试，不逐一巡查其它历史案例。已有完整共享知识且本轮未新增/修改规则时直接复用即可。

结果与网页可以先给用户；“先交付”不表示任务终止。最终说明本次工作完成前，应明确本轮机制是已复用、已更新、已新增，还是保留为案例私有并说明原因。仅保存 case profile、报告 JSON 或 mechanisms 链接，不等于已完成共享知识沉淀。

## HTML 输出质量与跨 agent 交接

- 生成/修改玩家 HTML 前，必须阅读 [docs/contracts/player-report-ui.md](docs/contracts/player-report-ui.md) §6（统一玩家报告质量基线）及涉及的 §2/§3；这是新存档、换技能、换 agent 和共享工程时的统一输出要求。
- 复用正式流水线和 `report_ui/unified_report.py` 等共享组件，按真实计算能力呈现；不得另建技能专用页面、复制历史存档数值，或退化为工程账本/仅 DPS 摘要。
- 已计算的乘区、真实来源、施加/调度细节和完整装备词条不得在统一结果转换中丢失。缺失先查投影链，未支持范围如实记录，不伪造数值/控件/时间图。
- Buff 来源必须覆盖当前构筑全部专精、被动及装备，不限主技能树；区分常驻、临时与下一次施法，不把 Preparation 类效果永久施加到循环。
- 装备页必须展示当前装备、神像、祝福的全部玩家可读词条（包括与主技能无关的防御/功能属性）；完整解码明细优先，禁止用摘要覆盖 mods，按实例与词条身份核对。
- 实现边界与跨案例待验证项见该契约 §6.7；不得宣称所有存档已通过验证。

### 网页验证的默认最小范围

- 只导入存档、更新数值或复用模板：确认本次 HTML 已生成、链接指向当前案例即可，默认不打开浏览器。
- 修改页面结构或样式：最多打开一次默认页面，确认能显示且无阻断；修改交互或重算：只走一条直接相关路径，成功即停。
- 只有用户明确要求完整验证时才运行完整矩阵。“完整报告”“新增机制知识”是功能交付要求，不自动授权完整网页验收。
- 最小验证发现问题时只修复该阻断并复查同一路径；先交付结果和链接，未做完整验证不作为扣留交付的理由，也不宣称已完成完整验证。

## 主技能与存档接入

用户已指定主技能时直接使用；未指定时可以先导入并列出技能，再询问主技能，不因模板命中替用户选择。

新案例使用不存在的 `examples/<case>/case.json`（`import` 拒绝覆盖已有路径）；推荐从 `examples/_template/` 理解字段，或直接让 `import` 生成骨架。不复制其它案例的输入、补值、场景或预期值。`examples/` 下除 `_template/` 外均为本地工作目录（gitignored）。

正式入口是 `scripts/le.ps1`，接入实现是 `scripts/le_pipeline/onboarding.py`。

```powershell
# 首次使用先获取 data dataset（详见 README）
.\scripts\fetch_dataset.ps1 -Repo BlaCatFishLE/LastEpoch-AIBuildSimulator
.\scripts\le.ps1 doctor

# 完整首接：需要用户自备 Node + lastepochsupertools（不随仓库分发；缺依赖返回 needs_input）
# 当前完整首接 V1 仅显式 BH 路线：--main-skill bh2 --model black-hole
.\scripts\le.ps1 import --case examples\<case>\case.json --save '<当前存档>' --main-skill bh2 --model black-hole --tool-home '<lastepochsupertools目录>'
# 其它技能：先研究接入（--model research），再在 prepare 指定主技能
.\scripts\le.ps1 import --case examples\<case>\case.json --save '<当前存档>' --model research --tool-home '<lastepochsupertools目录>'
# 没有外部工具：骨架导入（clean-room 解析，仅生成 BD/案例骨架，不会自动产出可计算输入）
.\scripts\le.ps1 import --case examples\<case>\case.json --save '<当前存档>'

# 以下命令只对已完整首接的当前案例按需执行；不把整段当作必跑清单。
.\scripts\le.ps1 prepare  --case examples\<case>\case.json --main-skill <ID>
.\scripts\le.ps1 run      --case examples\<case>\case.json
.\scripts\le.ps1 validate --case examples\<case>\case.json
.\scripts\le.ps1 serve    --case examples\<case>\case.json
```

- 接入后从本案例 `onboarding-report.json` 的产物引用继续；未知机制进入本案例研究队列，补齐本次计算需要的规则。
- 仅当主技能属于当前支持范围（VC/FR/BH 的 execution-v1 切片；Ballista partial）时才可能生成 DPS 结论；其它技能停在 `needs_research`，只做数据准备，不伪造结果。
- 没有 Node + `lastepochsupertools` 时不能完成完整首接：可做骨架导入与 planner 手工流程，但不能把骨架产物当作可运行案例或已支持能力；不要写“无需外部工具即可从自己存档算 DPS”。
- planner payload（Last Epoch Tools 本地 JSON）路线不依赖 Node：`le_build import --file ... --share-id ... --offline` → `le_build export` → 补齐案例字段 → prepare/run；见 `tools/build_import/README.md`。
- 现存 CLI 的历史 model 名称与技能专用适配器属于兼容实现债务，不能理解成项目要求多个独立 DPS 模型；具体支持范围按当前命中知识与实现核实（[docs/current-status.md](docs/current-status.md)）。

## 契约导航（需要时读取）

- 接入与组件知识：[docs/contracts/save-onboarding.md](docs/contracts/save-onboarding.md)、[docs/contracts/component-knowledge-v1.md](docs/contracts/component-knowledge-v1.md)。
- 流水线与数据绑定：[docs/contracts/workflow-interface.md](docs/contracts/workflow-interface.md)、[docs/architecture.md](docs/architecture.md)。
- 网页与本机重算：[docs/contracts/player-report-ui.md](docs/contracts/player-report-ui.md)。
- 计算计划/结果：[docs/contracts/calculation-plan-v4.md](docs/contracts/calculation-plan-v4.md)、[docs/contracts/calculation-route-v1.md](docs/contracts/calculation-route-v1.md)、[docs/contracts/definitions-v3.md](docs/contracts/definitions-v3.md)。
- 知识 schema：`knowledge/schemas/`；计算输入 schema：`tools/build_import/schemas/`。
- 当前案例的 `case.json` 是本次输入/规则/场景/输出的入口；规则包路径来自该案例，不使用其它案例默认值。
- [docs/current-status.md](docs/current-status.md) 是能力摘要与限制索引，不是导入前检查清单。

## 当前结果与必要验证

- 保留正式计算链已有的结构、指纹、来源和结果一致性检查，不额外启动旧案例运行或测试套件。
- 结果范围与实际计算组件一致；未知和假设留在结果/账本中。没有支持的组件不伪造 DPS。
- 先交付当前数值和网页；网页仅按默认最小范围验证，功能交付要求不自动触发完整交互验收。
- 未运行全量或无关测试如实记录，不因此扣留已验证范围的结果；新增语义的必要检查尚未完成时，可先交付候选产物并标注待验证，不能仅凭文件存在或 validate PASS 宣称签收。
- 不执行 `test --suite all`、全目录 discover、拆分套件全跑或历史案例成组重跑，除非用户单独明确要求完整回归。

## 这些数据/文件永远不要改写

| 对象 | 位置 | 正确做法 |
|---|---|---|
| 已发布 dataset | `data/releases/<dataset-id>/` | 新建 dataset-id 重跑 publish；`data/releases/` 下的大文件不入库 |
| 构筑 revision | `data/builds/**/revisions/<rev-id>/` | 新输入 → 新 revision（本地产物，gitignored） |
| 当前 dataset 指针 | `data/current.json` | 只由 publish 原子替换 |
| 历史 run 产物 | `reports/**/<run-id>/` | 新 run-id，不覆盖；`reports/` 是用户本地输出目录（gitignored） |
| 逆向证据/实测记录 | `docs/evidence/**` | 只追加新证据，不重写旧证据数值 |
| 原始来源快照 / 离线存档 | `data/raw/**` | 只读；需要修正就产生新快照（不随仓库分发） |

另外：不要用 `EXPECTED_*` 常量改写或 monkey patch 伪造结果；不要用“重新生成的预期值”掩盖与实测的差异；不要在运行时代码里硬编码某个 BD 的数值（案例私有输入应放 `examples/<case>/inputs/`）。

## 新增或修正机制怎么记账（缺一不可）

1. **不改现状默认值来“猜”**：先登记为假设/未解决，再实现可开关的建模。
2. 计算口径类 → 对应规则包（`tools/phase3/rules/*.json`）：加 `status: assumed|unresolved` + `evidence[].locator`。
3. 条件效果类 → 当前构筑所绑定的条件规则/注册表：`support.level` 取 `needs_rule_mapping|needs_action_or_time_simulation|mechanism_unresolved`，并在 `research.open_questions` 写清要验证什么。
4. 证据落盘：逆向 → `docs/evidence/reverse-engineering/`；实测 → `docs/evidence/…`；不要只留在临时目录（`.scratch/`）、临时日志或聊天里。作者本地的归档材料用 `archive://` 前缀引用（不随公开仓库分发）。
5. 按上文“知识收尾”更新正式知识 JSON 与通用规则，不只留在案例配置或文档中；随后同步索引：`docs/mechanisms.md` 增加一行（证据类别 / 状态 / 入口 / 适用范围）。
6. 结果 JSON/工程账本必须保留 `status=assumption` 与 `issues`；玩家显示遵守报告契约，不把技术审计重新堆入 HTML，也不得把实验假设写成已确认机制。

## 写作与协作纪律

- 本项目的新临时脚本/临时文件放在项目根目录下的 `.scratch/`（gitignored）；正式产物放 `reports/`、`docs/` 或对应功能目录。不迁移既有对话文件。
- 文档正文用简体中文，标识符/路径/字段名用英文原样；引用路径一律**仓库根相对**，不写个人机器绝对路径、用户名、邮箱或其它个人身份信息。
- 引用前先确认文件真实存在；不确定的写“未验证/未落地”，不要编造路径或数值。
- 事实分两类写：**现有记录**与**本轮实测**；不要把前者说成后者。
- 并行/多人改动时，先确认自己的写入范围，只改自己负责的文件；不删除不属于自己的文件，不改别人的报告产物。
- 不确定的用途先归档而不是删除；任何递归移动/删除前核对最终绝对路径在本次范围内。
