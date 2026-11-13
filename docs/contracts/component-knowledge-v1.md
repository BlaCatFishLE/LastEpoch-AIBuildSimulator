# BD 组件知识匹配 V1

**网页验证默认最小化**：仅生成数值/复用模板时只确认本次 HTML 和链接；修改页面时最多一次打开，修改交互时只确认一条直接相关路径。用户未明确要求时不执行完整网页验收。具体规则见 `AGENTS.md` 的“网页验证的默认最小范围”。


## 日常执行顺序（用户要求）

1. 先冻结、解析用户本次指定的存档，生成独立 BuildSpec 与 calculation-input。只检查当前输入和解码必需依赖；不要运行默认案例 doctor，也不要在导入前完整检查任何旧案例。
2. 导入后按当前构筑的技能、节点、被动、装备、神像、祝福及词条稳定 ID 查询沉淀知识，绑定当前实例的数值和点数。只按命中记录追溯必要证据；历史案例没有默认优先级。
3. 复用统一 DPS 流程的属性、乘区、暴击、目标、时间与报告组件。hit/DoT 仅表示击中与持续伤害的计算组件差异；技能行为由数据和规则表达，不能再按技能建立独立 DPS 模型。
4. 优先生成当前 BD 的结果和网页；区分研究/候选产物与可用于当前声明范围的计算结果。机制未知可以显式假设或缩小范围继续；已知来源、执行或账本错误不能用 partial 掩盖，也不能据此更新可用 canonical。
5. 复用已验证能力且未改变语义时，默认不追加测试阶段。新增或修改计算、绑定、条件、投影或门禁语义时，按受影响边界做少量正例/反例；既有同边界证据可复用，不按技能数或词条数铺测试。候选网页可先生成，相关最小检查在宣称该范围可用前完成；直接决定可计算性的检查可前置。细则见 `docs/workflow.md`“新机制接入与最小语义验证”。完整回归仍须用户单独明确要求。

所有历史案例地位相同，均为以前使用或建立流程留下的实例。案例名不决定默认输入、必读材料、知识检索顺序、回归基线或交付门槛。“走全套流程”不等于授权全仓测试。


主技能由用户确认。组件并集匹配负责寻找可复用知识；唯一命中技能模板可用于提出候选，但不能替代用户确认主技能。缺少用户明确选择时，agent 应先询问，再以确认的主技能调用 resolve_build/prepare/run。

知识来源为整个 BD 的组件并集：专精技能、节点、被动、装备、神像、祝福及词条。定义 ID 用于检索，实例 ID 用于参数和贡献。普通属性沿用现有输入适配器；共享算子由固定依赖引入。无需案例继承、组件两两组合、额外服务或表达式执行器。

## 知识收尾（结果先交付，任务结束前完成）

本轮新增、修正或临时写进案例 profile 的可复用机制，完成实现后必须收尾：先查已有条目，复用或更新；没有则在 `knowledge/mechanisms/` 登记稳定 ID、当前参数绑定、规则/处理器与证据入口，并把通用规则从案例实例配置中分离。确属案例私有的输入可保留在案例中，但要写明不能共享的原因。普通属性已有映射时不强制逐行新建机制文章。

证据不完整可登记为 assumed/unresolved，尚未实现可登记 draft/not_implemented；不能把“没有全部实测”“没跑回归”或“先输出网页”当作不登记的理由。实现可复用与证据确认是两个独立维度，不得为入库伪造 validated 状态。

最小收尾只需：条目随正常加载可读、重建知识索引、本轮代表性稳定 ID 能命中。不要重跑导入、计算、网页或全套测试，不逐一巡查其它历史案例。已有完整共享知识且本轮未新增/修改规则时直接复用即可。

结果与网页可以先给用户；“先交付”不表示任务终止。最终说明本次工作完成前，应明确本轮机制是已复用、已更新、已新增，还是保留为案例私有并说明原因。仅保存 case profile、报告 JSON 或 mechanisms.md 链接，不等于已完成共享知识沉淀。

## 未知知识的渐进接入

检索命中、证据确认、处理器可执行与当前实例绑定是独立事实；任何一项通过都不自动证明其它项。候选 mechanism 可以保留 derived/unresolved 与 draft/not_implemented；无需先把所有条目升为 confirmed 才推进可计算部分。

只研究当前范围必需的关系；已有依据但未实测的公式可显式建模为假设，未实现且可分离的组件先排除并登记范围。实际来源缺失不能伪造成知识匹配成功，已知数值/账本错误不能登记成“机制未知”后继续当有效贡献。必要语义检查与交付状态见 `docs/workflow.md`“新机制接入与最小语义验证”；知识收尾只重建受内容变化影响的索引，不额外重跑导入/计算。

## 匹配与知识源

`knowledge/` JSON 仍是唯一权威源。`reuse_mode=shared` 的记录按稳定 ID 复用；`case_scope` 是已验证案例列表。`case_bound` 和未迁移记录保留旧案例限制。`required_abilities` 是来源技能条件（与当前主技能不同，辅助技能也可提供效果）。旧实例槽位不能作为共享装备定义选择器。

`selectors` 为候选入口；`applicability.required_nodes` 检查必要节点。共享记录中的 `conditions` 描述运行时启用条件，知识检索不以 Buff 当前关闭排除规则；具体激活、标签过滤、单位及叠加仍由已引用的规则/注册表/处理器负责。技能模板只绑定请求的主技能，不能被其它专精模板覆盖。

## 当前参数与计算

`parameter_bindings.source=current:matched_instances` 表示在本次 analysis-plan 的 `bound_instances[mechanism_id]` 取匹配实例。节点提供当前 points；词条提供当前 value、property、tags、modifier_type。历史案例路径仅作证据。规则文件依然拥有公式和乘区定义，不复制计算实现到知识库。

已有明确 `value_per_point` 的 more 效果在相应伤害组件入口绑定当前节点点数，未选节点为零；不从旧总值倒推公式。兼容旧快照时，从其哈希绑定的 calculation-input 读取点数；没有本地输入的独立旧快照沿用原冻结规则。动态点数不扩展为任意结构节点移除/转换的自动模型生成，现有模型范围仍适用。

知识去重不等于贡献去重：同种词条多个实例分别保留；已有 stats/conditional 入口负责贡献，不从知识计划额外注入第二份效果。条件值和常驻效果仍使用现有当前输入求值。

## 输出与扩展接口

沿用 `resolve_build`、`stage.resolve_skills` 和正式 onboarding/prepare 接口。内部函数 `collect_components(calc)`、`component_queries(components)`、`bind_instances(entry, components)` 可独立调用。analysis-plan 增加 `build_components`、`bound_instances`、`component_coverage`；分项耗时放在返回 metrics.component_matching.*.elapsed_ms，避免污染分析计划与缓存语义。bound_mechanisms 保留 reuse_mode、matched_instances、known_limitations。

coverage 的 `unclassified` 仅表示没有独立知识文章，不等价于已支持。普通属性若已有 ledger 映射，标记 existing_adapter；既有适配器也无法解释的 unresolved 行进入映射队列。已知实现缺口产生 handler 任务；共享知识的已知假设留在 known_limitations，不每次重新发起研究。原有范围限制保留。

索引按现有知识内容指纹重建/失效，运行时批量查询一次。知识 schema 只在加载/重建时验证；沿用阶段缓存和现有结果门禁。小版本变化不新增自动阻断。

## 验证范围（用户要求，2026-11-10）

默认只做当前改动必要的定向验证。新 BD、新模型、新机制与机制知识入库均不自动触发完整回归；不追加 all suites 或 BH/FR/变体成组复现。只有用户单独明确要求时才执行完整回归；详见 `AGENTS.md` 的默认验证范围。

## 统一 DPS 第一步的组件账本接口（2026-11-10，最小事实更新）

统一 DPS 路径的 profile 已版本化为 `le-dps-profile-v2`（案例示例：`examples/<case>/inputs/dps-profile-v2.json`）：组件声明、效果来源绑定与受限规则都在 profile 内显式给出，不再由核心代码硬编码技能/状态特例。

结果 JSON（`le-dps-result-v3`）新增与知识匹配无关、但与组件账本对齐的字段：

- `applications[]`：组件级应用记录（`applied` / `not_applicable` / `condition_not_satisfied` / `source_absent` / `unresolved` / `duplicate`）；共享 `included` 只在映射阶段确定，计算后不再修改。
- `coverage`：`status=complete|partial` 与 `gaps[]`；只有“可能影响当前伤害且未解释”的来源进入缺口，已确认防御属性与已声明假设不使结果 partial。
- `effect_ledger`、`equipment_facts`、`skill_constants`、`ailment_constants`：来源绑定与数据集常量校验记录。
- `scope` 动态生成，`scope_detail` 记录技能/组件/异常/固定条件。

该变更不改变知识匹配契约（`resolve_build` / `collect_components` / `bind_instances` 等接口与 coverage 的 `unclassified` 语义保持原样）；知识查询仍按稳定组件 ID 绑定当前实例数值与点数。

**验收补修（2026-11-10 晚）**：`coverage` 现在把未验证（`unverified`）的相关效果计入缺口、把可选来源缺失/节点未分配视为不适用而非缺口；来源绑定要求“稳定定义身份 + 效果语义”双锚点（equipment 词条与 unique 说明行缺一即 `unverified`）；属性转换在加载与运行时都强制 `expected` 身份锚点；`every_nth_hit.n` 必须为正整数、计数策略/相位必须被显式支持；条件代码求值已抽到共享 `condition_codes` 模块，新旧路径共同调用。

## 物品特殊效果与暗金说明行（2026-11-13 补充）

- `unique_description` 不是可计算 mod；dataset 可能只有翻译文本、没有数值编码。涉及伤害/属性/频率的说明行必须逐条核实并登记：可计算项落模板 effect/rule，不适用项写明机制理由，未解决项登记缺口；不得因 dataset 无编码而静默丢弃，也不得用说明文本猜数值。
- 每个可复用的物品特殊效果应有稳定 `knowledge/mechanisms/` 条目（选择器可用 item 定义 ID + mechanism ID），并引用 IL/数据/输入证据与参数绑定（例如 per-attribute、装备数量条件）。
- 条件型或按属性/装备数量缩放的 effect 必须用通用声明能力；effect 字段白名单外的键必须拒绝（禁止 `condition` 拼错后静默变常驻）。
- 机制条目示例（稳定 ID，不代表公开版全部机制）：`mourningfrost-cold-per-dexterity`、`phantom-grip-crit-chance-per-dexterity`、`urzil-pride-all-attributes-corrupted-condition`。
