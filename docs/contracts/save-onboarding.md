# 新存档正式接入（V1）

**网页验证默认最小化**：仅生成数值/复用模板时只确认本次 HTML 和链接；修改页面时最多一次打开，修改交互时只确认一条直接相关路径。用户未明确要求时不执行完整网页验收。具体规则见 `AGENTS.md` 的“网页验证的默认最小范围”。


## 日常执行顺序（用户要求）

1. 先冻结、解析用户本次指定的存档，生成独立 BuildSpec 与 calculation-input。只检查当前输入和解码必需依赖；不要运行默认案例 doctor，也不要在导入前完整检查任何旧案例。
2. 导入后按当前构筑的技能、节点、被动、装备、神像、祝福及词条稳定 ID 查询沉淀知识，绑定当前实例的数值和点数。只按命中记录追溯必要证据；历史案例没有默认优先级。
3. 复用统一 DPS 流程的属性、乘区、暴击、目标、时间与报告组件。hit/DoT 仅表示击中与持续伤害的计算组件差异；技能行为由数据和规则表达，不能再按技能建立独立 DPS 模型。
4. 优先生成当前 BD 的结果和网页；区分研究/候选产物与可用于当前声明范围的计算结果。机制未知可以显式假设或缩小范围继续；已知来源、执行或账本错误不能用 partial 掩盖，也不能据此更新可用 canonical。
5. 复用已验证能力且未改变语义时，默认不追加测试阶段。新增或修改计算、绑定、条件、投影或门禁语义时，按受影响边界做少量正例/反例；既有同边界证据可复用，不按技能数或词条数铺测试。候选网页可先生成，相关最小检查在宣称该范围可用前完成；直接决定可计算性的检查可前置。细则见 `docs/workflow.md`“新机制接入与最小语义验证”。完整回归仍须用户单独明确要求。

所有历史案例地位相同，均为以前使用或建立流程留下的实例。案例名不决定默认输入、必读材料、知识检索顺序、回归基线或交付门槛。“走全套流程”不等于授权全仓测试。


新 BD 主技能确认（用户要求）：导入新 BD、首次选择 DPS 主技能前，先检查用户在当前请求或会话中是否已明确指定主技能；未指定或表述含糊时，必须主动询问并等待确认。可以先冻结、解析存档并列出候选技能。即使知识库只有一个可用技能模板，也只能作为候选建议，不能替用户决定或自行填写 --main-skill。用于最终验收的“某 BD 微调”等提示不替代主技能确认。用户已确认的同一案例后续 run/reproduce 不必重复询问。

CLI 的显式参数应落实用户已确认的选择；agent 根据知识命中自行补参数，不等于用户确认。

入口（G1/G2 后）：`le.ps1 import --case <新案例>/case.json --save <存档> --main-skill <用户确认 ID> --model research [--tool-home <目录>]`；
`--model black-hole` 仅保留为旧 BH 显式兼容入口。

- 不传 main-skill/model 时保留骨架导入；完整首接要求显式选择主技能。新输入由 `knowledge/definitions/` 的 v3 候选按能力/适用性判定绑定，不按技能 ID/文件名直接选模板，不回退 profile/hit/legacy。
- 原始来源复制到新案例 source/，记录 SHA；拒绝覆盖任何既有案例。外部 tool-native 依赖来自 CLI/环境变量/项目配置，缺失在写入前报告。
- 正式适配器生成并归一 wire，发布独立 BuildSpec revision，导出计算输入，声明结构计数，生成空人工修正与未确认折射的显式假设。不得复制旧案例参数。
- 自动生成快照、默认 build_base 场景与绑定、结构/状态 expected。首次计数只检查后续链路完整性，不是独立正确性证明；expected 不含本轮计算出的伤害值。
- import 只完成数据接入 + prepare（编译计划/分析产物），**不隐式 run 或发布 HTML**；用户要报告时继续显式 run。
- onboarding-report 保持原始输入、route、candidate、binding、prepare-context/plan 引用；`status/readiness/blocked_stages` 由实际 route/prepare/compile 结果投影，状态集合为 `prepared|partial|needs_input|needs_research|rejected|failed`，不再固定写 needs_research/return 3。
- 场景由当前输入与定义的合法中性默认生成；false/0 只在字段契约允许时使用（持续时间、间隔等不能机械填 0）。无中性值时使用有依据且显式记录的场景假设，关键口径无法合理界定再询问；assumptions 写入场景文件。不从历史案例复制 Buff/层数/怪物等级/抗性/corrections/scenario/expected，也不以场景默认覆盖缺失的节点/装备/actor 身份。
- 案例操作必须显式 `--case`；不再读取 `config.default_case` 或全局 latest。`doctor` 无 case 时只检查共享环境。重复 prepare/resume 复用当前任务已验证产物与阶段缓存，不覆盖既有案例/历史 run。
- bootstrap 写入 onboarding-report.json，逐步骤记录耗时/状态，并单独保留 BuildSpec/prepare 退出码，失败保留诊断；重跑已有案例使用 prepare/run/reproduce，import 不覆盖。
- 纯存档 reproduce 的 stash/planner 均可选；有归档 wire 时以正式适配器从冻结存档重新生成并比较后继续。固定样例路线保持原行为。
- serve 的案例身份、注册表和运行标识来自所选案例/报告，旧默认仅用于直接底层调用。
- 知识匹配见 [组件知识契约](component-knowledge-v1.md)；未实现的模型和缺失依赖明确报告。完整网页行为验收与数值/API 验证分别记录。

## 知识收尾（结果先交付，任务结束前完成）

本轮新增、修正或临时写进案例 profile 的可复用机制，完成实现后必须收尾：先查已有条目，复用或更新；没有则在 `knowledge/mechanisms/` 登记稳定 ID、当前参数绑定、规则/处理器与证据入口，并把通用规则从案例实例配置中分离。确属案例私有的输入可保留在案例中，但要写明不能共享的原因。普通属性已有映射时不强制逐行新建机制文章。

证据不完整可登记为 assumed/unresolved，尚未实现可登记 draft/not_implemented；不能把“没有全部实测”“没跑回归”或“先输出网页”当作不登记的理由。实现可复用与证据确认是两个独立维度，不得为入库伪造 validated 状态。

最小收尾只需：条目随正常加载可读、重建知识索引、本轮代表性稳定 ID 能命中。不要重跑导入、计算、网页或全套测试，不逐一巡查其它历史案例。已有完整共享知识且本轮未新增/修改规则时直接复用即可。

结果与网页可以先给用户；“先交付”不表示任务终止。最终说明本次工作完成前，应明确本轮机制是已复用、已更新、已新增，还是保留为案例私有并说明原因。仅保存 case profile、报告 JSON 或 mechanisms.md 链接，不等于已完成共享知识沉淀。

## 研究/原生 v3 首接（G1/G2）

`import --case <新目录>/case.json --save <当前存档> --main-skill <已确认 ID> --model research`
冻结当前存档并完成解码、独立 BuildSpec revision、计算输入、词缀修正；随后扫描
`knowledge/definitions/*.json`（元数据来自 `knowledge/definitions/index.json`），按当前
FrozenContext 事实（ability/class/tags 等）判定候选：单一适用模板 → 生成中性默认场景并显式绑定
`definitions_v3`；多候选/缺事实 → `needs_input`；零候选/能力缺口/模板损坏/不适用 → `needs_research`。
文件损坏与同技能不适用均保留候选/拒绝原因，不选第一条、不回退旧模型。

- 绑定后 prepare 执行 `resolve_skills → compile_plan → min_validate`；就绪时 report 状态 `prepared`，
  partial 时 `partial`，其余由 route/prepare 实际结果投影并写 blocked_stages/next_actions。
- 尚无可执行范围时不生成伪 DPS 或可重算服务；可以生成明确标为研究/输入分析的 HTML。部分范围和显式假设已可执行时，可继续该范围的候选/partial 报告；不静默改走 legacy hit/BH。
- 研究案例的 build/ruleset/registry 不得从 config.default_case/default build 或旧案例继承；
  validate 仅验证当前来源、revision、计算输入指纹，明确属于数据准备验收。
- 工具位置只从参数/环境/配置读取；禁止扫描历史 reports、examples 或聊天以寻找新输入。
- 候选目录是派生导航，模板正文仍是权威；索引不参与计算配置。

## 验证范围（用户要求，2026-11-10）

默认只做当前改动必要的定向验证。新 BD、新模型、新机制与机制知识入库均不自动触发完整回归；不追加 all suites 或 BH/FR/变体成组复现。只有用户单独明确要求时才执行完整回归；详见 `AGENTS.md` 的默认验证范围。
