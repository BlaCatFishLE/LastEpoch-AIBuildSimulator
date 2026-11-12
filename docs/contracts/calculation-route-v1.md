# 计算路由与统一正式流水线契约 v1

状态：现行（第三步实现契约，2026-11-11）。适用：`prepare` / `run` / `validate` / `serve`
的统一 DPS 路径；旧 BH/FR 数值路径仍是独立 legacy 分支。

## 1. 共享路由

唯一实现：`tools/phase3/src/le_calc/calculation_route.py`。CLI 与 workflow 只调用该模块，
`le_calc` 不反向 import `scripts`。

`resolve_calculation_route(case, root, explicit=None) -> le-calculation-route-v1`（纯 JSON）：

| 字段 | 说明 |
|---|---|
| `engine` | `unified_dps` / `legacy` / `needs_research` / `needs_input` / `unsupported` |
| `status` | `ready` / `needs_research` / `needs_input` / `route_conflict` / `unsupported_route` |
| `route_id` | 稳定路由 ID，例如 `unified:profile_v2`、`unified:definitions_v2`、`legacy:hit` |
| `origin` | `explicit_calculation` / `legacy_dps_profile_compat` / `research_definition_match` / `explicit_legacy_model` |
| `source` | 来源声明与文件引用（含每条 `input_refs` 的 path/sha256） |
| `diagnostics` | 结构化冲突/缺失说明；不回落旧计算器 |

### 1.1 case.calculation 声明

```json
{"engine":"unified_dps","source":{"kind":"profile_v2","path":"examples/<case>/inputs/dps-profile-v2.json"}}
```

```json
{"engine":"unified_dps","source":{
  "kind":"definitions_v2",
  "template":"knowledge/skills/<template>.json",
  "mechanisms":["tools/phase3/rules/<mechanisms>.json"],
  "rules":["tools/phase3/rules/<rules>.json"]
}}
```

`definitions_v2` 的每个路径都是仓库相对文件引用，加载时逐项校验存在、JSON、schema
（`le-skill-template-v2`）、稳定 ID、sha256 以及模板 `ability_id` 与 `case.main_skill.ability_id`
一致；每个文件进入计划 `input_refs`。未支持能力（如需要 `current:matched_instances` 的机制）
明确拒绝，不做近似实现。

### 1.2 路由规则

| 情况 | 结果 |
|---|---|
| 显式 `calculation.engine=unified_dps` 且来源合法 | 对应统一编译入口 |
| 仅旧 `dps_profile`，`model=research`/未声明 | 归一 `profile_v2`（记录 `legacy_dps_profile_compat`） |
| 新旧来源指向同一 profile | 允许兼容别名，一份权威来源 |
| 两份统一来源不一致 / 显式 legacy model + 统一来源 / 未知 engine/source | `route_conflict` 或 `unsupported_route`，禁止静默选择 |
| 显式旧 `black-hole`/`hit` 且无统一来源 | `legacy` 隔离分支，保持旧行为 |
| research 无来源但主技能唯一命中 v2 定义 | 自动进入 `definitions_v2` |
| research 无来源、无 v2 定义 | `needs_research`（不生成伪 DPS） |
| 未声明主技能且无法确定来源 | `needs_input` |

## 2. compile_plan 阶段与产物

`workflow/stage.py` 注册 `compile_plan`（固定顺序位于 `resolve_skills` 之后、
`min_validate` 之前；legacy 路由直接拒绝）。共用实现：
`workflow/unified.py::compile_stage`。

产物：

- `calculation-plan.json`（第二步契约；`plan_sha256` 内容身份）；
- `plan-diagnostics.json`；
- `compile-result.json`：路由、执行/就绪状态、plan_ref、input_refs、编译/执行实现指纹。

`min_validate` 的统一分支只校验：case schema、路由、计划完整性/结构、上述产物；
**不要求**旧 `expected` / `inputs.snapshot` / `ruleset.path`。expected 只作独立声明核对。

## 3. 正式 run 阶段图

统一路径固定顺序：

```
compile_plan → simulate → validate_result → render_report → commit
```

- `none` 与 `staged` 共用阶段职责；`staged` 仅在 commit 阶段用既有租约/候选/原子发布协议发布。
- `simulate` 必须读取 `calculation-plan.json`（带 plan sha 绑定），执行完整场景；
  默认场景来自计划 controls，实际场景写入 `scenario.json`，result 绑定其 canonical sha。
  每次完整 run 只编译一次、默认场景只执行一次；阶段不重算公式。
- `validate_result` 校验 result-v3 结构、plan/scenario/execution 绑定、贡献顺序、账本一致、
  数值/稳定态一致性、coverage 计数、必需产物；不重新编译、不重新计算。
- `render_report` 由 result + plan 生成 `dps-report.html`、`report-binding.json`、
  `report-capabilities.json`；计划只作为内容身份，不写入 staging 路径。
- `commit` 门禁必须校验 validation passed、accepted 非空、result sha 绑定、plan sha 绑定与
  必需产物；随后写 `run-manifest.json`（文件清单+sha）并发布。

统一 run 最低产物：`calculation-plan.json`、`plan-diagnostics.json`、`compile-result.json`、
`scenario.json`、`result.json`、`source-ledger.json`、`input-snapshot.json`、
`validation-result.json`、`dps-report.html`、`report-binding.json`、`report-capabilities.json`、
`run-manifest.json`、`pipeline-state.json`；staged 另有既有操作/租约/提交记录。

## 4. 状态与退出码

| 情况 | 阶段 | run 退出 | 发布 |
|---|---|---|---|
| 计划合法，coverage complete | completed/ready | 0 | 发布 |
| 计划合法，coverage partial | completed/partial | 3（`EXIT_PARTIAL`） | 发布 |
| 需要输入/研究 | waiting/needs_input 或 needs_research | 3，无伪数值 | 不发布数值报告 |
| 路由冲突/结构非法/校验失败/发布失败 | failed | 1 | 不发布 |
| CLI 参数错误 | 未执行 | 2 | 不发布 |

`status=assumption`、coverage、validation.passed 是不同维度；partial 结果可通过完整性门禁
并发布，但退出码仍为 3。从缓存/replay 完成时必须从实际结果读取 coverage，不能返回错误 0。

## 5. 本机重算

`serve` 选择报告后：若报告目录含 `calculation-plan.json`，走统一重算路径：

1. 载入已保存 plan/scenario/binding/manifest，校验归属、计划完整性/结构；
2. 校验报告绑定、端点端口、当前依赖文件 sha（plan.input_refs）与实现指纹；
3. 请求按计划 `controls` 校验场景（沿用只读/范围/目标/loopback/request/session/revision 防护）；
4. 仅调用 `execute_plan(plan, scenario)`，不重新编译、不打开 catalog 做来源绑定；
5. 响应 binding/身份/接受版本/结果/HTML 同属一次执行；临时响应不改写已发布 run。

旧报告缺少计划契约时明确要求新 run，不静默重新绑定。

## 6. 缓存与依赖（当前实现边界）

统一路径的阶段缓存按阶段 handler、input_refs、参数与实现指纹工作；同依赖
`compile_plan/simulate/validate_result` 可命中并 materialize 产物，输出目录/端点只影响
`render_report`/commit。输入、profile/规则、实现指纹变化会使相应阶段失效。

当前契约要求但第三步**未完整实测**的缓存负例：独立场景仅重执行、renderer/端点仅重渲染、
损坏缓存拒绝、恢复/中断注入；留待复验或第四步，不宣称已全部覆盖。

## 7. legacy 与后续

- 显式旧 `black-hole`/`hit` 继续使用既有隔离实现与 prepare_snapshot/min_validate 旧门禁；
- unified 路径不使用伪 snapshot/expected/ruleset；
- `reproduce` 对统一来源明确返回 `unsupported_operation` 并指引 prepare/run，不退回旧计算器；
- BH/FR 全量数值迁移、完整原始链 reproduce、缓存负例与故障恢复注入留第四步。

## 8. 复验补修（2026-11-11）

- `validate_stage(write=False)` 为只读校验；`validate --run-dir`、`domain.validate_result`、commit
  门禁复验均只读，只有 run 的候选/工作目录写入 `validation-result.json`。已发布 run 的校验不得改写其文件。
- 结果门禁新增：plan subject 与 case build/revision 绑定；显式 expected 必须存在并按共享 expected 口径核对；
  applications 结构/摘要复算；coverage 必须等于计划基线缺口 + 实际应用运行时缺口（`project_coverage`）；
  plan limitations/静态 issues/scope 保留；partial 缺口摘要；单次/稳定态一致性。commit 需通过只读复验和
  28 项必需检查、plan/result sha 绑定。
- 统一 prepare：有原始输入时执行 freeze/parse/normalize/export/affix/resolve 后再 compile_plan；已冻结输入可跳过
  导入但至少执行 resolve_skills + compile_plan + min_validate；`prepare-context.json` 记录实际准备阶段、
  `input_handoff` 与 calculation-input/build/profile/属性规则/分析产物 artifact_ref；research 无来源也写 context 并返回 3。
- 发布指纹 v2 使用实际输入文件 sha（calculation-input/build/dataset/profile/属性转换规则/route 来源）；
  stage cache 的 `compile_plan` 专属依赖组包含 route/plan_compiler/plan_handlers/calculation_plan/unified 等。
- 原生 mechanisms 多文件：同 ID 同定义合并来源；同 ID 异定义 `mechanism_definition_conflict` 阻断。
  rules 多文件同键同值合并、异值 `rule_definition_conflict`；外部规则与模板内联冲突阻断；非冲突规则并入
  模板 `rules` 供已支持规则键使用。
- `serve` 仅从已保存 plan/scenario 重算，不重新编译；历史报告缺少计划契约时要求新 run。

## 9. 第二轮复验补修（2026-11-11）

- 交接 override 必须校验实际 artifact sha，并核验 calculation-input 的 `build_id/revision_id/content_sha256`
  与 BuildSpec（`meta.build_id`、`meta.revision.*`）完全一致；不一致明确阻断，不能沿用原 case subject。
  plan subject 使用实际 BuildSpec 身份；新构筑必须绑定新 revision。
- 统一输入 refs 与发布指纹必须包含 dataset 指针指向的 SQLite 数据库实际内容 sha（`dataset_content`），
  不能只看 dataset 指针或声明值；测试使用临时小数据库夹具，不修改冻结 dataset。
- 数值阶段（simulate/validate_result）实现依赖清单排除 renderer-only 文件：
  `dps_report.py`、`report.py`、`hit_report.py`、`report_ui` 等；仅修改渲染代码不得使 simulate/validate 失效。
  `render_report` 仍依赖完整渲染模块；端点变化已有“只 render miss”的缓存断言。

## 10. 输入契约收口（2026-11-11 第三轮验收）

- `calculation_input`/`build_spec` override 必须同时携带合法 `*_sha256`；缺失、空串、非字符串、非 64 位
  小写十六进制均拒绝，不允许“有 ref 无 sha”绕过交接校验。CLI prepare 合法路径总是成对写入。
- calculation-input 的 `build_id/revision_id/content_sha256` 必须与 BuildSpec `meta` 身份一致；
  plan subject 采用实际 BuildSpec 身份。
- plan `input_refs` 按 role/path/sha 规范化排序并保留 case 引用；相同依赖集合的 plan_sha 不因加载顺序变化。
- 报告 binding 的外部依赖指纹不混入 plan 自身哈希；plan 哈希单独绑定。

## 11. definitions_v3（第四步 S2/S3 契约）

- 新增来源 `case.calculation.source.kind=definitions_v3`，引导
  `knowledge/definitions/void-cleave-fire-main-v3.json`；`route_id=unified:definitions_v3`。
- 真实 `FrozenContext`（calculation-input/BuildSpec/dataset/catalog）先经 `prepare_context` 加载校验，
  再由 `definitions_v3.bind_template` 解析 selector；生产路径不读旧 `dps-profile-v2.json`。
- selector 唯一命中当前 items/mods/description；缺失、歧义、身份/文本/property 不符均明确拒绝；
  不允许 synthetic fallback。
- VC 当前仍使用 plan-v1/result-v3（组件基数/调度未破坏性变化）；plan `input_refs` 增加 template、
  不包含 profile；`profile_sha256` 为空，来源身份由 template hash/input_refs 承担。
- FR/BH 所需 plan-v2/result-v4（多组件、有限调度/充能）留 S4/S5，不提前放宽 v1。

## 12. definitions_v3 多场景与 plan-v2（第四步 S4 FR 契约，2026-11-11）

- `case.calculation.source` 在 `kind=definitions_v3` 下新增互斥形态 `scenarios[]`
  （`{evaluation_id, path}`，与旧单 `scenario` 二选一）；FR 使用该形态把单次命中/持续输出
  拆成两个具名评估。
- 路由解析阶段校验：scenario 路径存在、evaluation_id 不重复、`scenario` 与 `scenarios` 不得同时声明；
  模板 `definition.evaluations` 必须声明且恰一个 primary；案例侧 scenarios 与模板 evaluations
  的 id 集合必须一致，否则 `route_conflict`/binding rejected。
- 每个 scenario 文件作为独立 `role="scenario"` `input_refs` 进入计划/发布指纹；文件内容哈希用于
  缓存失效与 simulate 阶段核对，评估执行身份用 canonical `{states,target}` 哈希
  （`plan.evaluations[].scenario_sha256`）。
- 编译产物 schema 由 `definition.plan_version` 决定：缺省 plan-v1（VC 不变），显式
  `le-calculation-plan-v2` 才启用多命中/具名频率/具名评估；`simulate` 对 plan-v2 从计划声明的
  场景文件加载每个评估并核对哈希，缺文件/哈希不符/场景 states 改写均拒绝，不回退默认场景。
- `validate_result` 按计划 schema 分派：v2 额外校验 evaluations 绑定、首击/每cast/权重当量复算、
  窗口期望施放数与 Hit/Cast 计数分离、应用组件引用；顶层执行身份按 evaluation_id 聚合全部
  评估场景哈希（主场景哈希另存 `primary_scenario_sha256`），任一评估场景变化都会改变
  `execution_fingerprint`；commit 门禁沿用同一必需检查集合。
- FR 正式 run 证据：作者本地运行产物（`reports/<case>/<run-id>/`，staged committed，退出码 3=partial；
  复验 P1 补修后交付；历史 run 保留只读，不随公开仓库分发）；
  契约细节见 `docs/contracts/calculation-plan-v2.md`、`docs/contracts/definitions-v3.md` §7–§9。

### 12.1 BH（S5）plan-v3 路由与证据

- `kind=definitions_v3` 的模板可声明 `plan_version=le-calculation-plan-v3`；`scenarios[]`
  每项对应一个 evaluation，evaluation 额外声明 `action_policy`（eager/patient 各一个）；
- 执行器按计划 schema 分派到 `plan_executor_v3`，结果 `le-dps-result-v5`；commit 门禁按
  `unified.required_validation_checks(schema)` 选择 v5 必需检查（46 项）；
- 正式 run 证据：作者本地运行产物（`reports/<case>/<run-id>/`，复验 P1 补修后交付，staged committed，
  退出码 3=partial；历史 run 只读，不随公开仓库分发）；契约见 `docs/contracts/calculation-plan-v3.md`、
  `docs/contracts/definitions-v3.md` §10。

### 12.2 canonical case 入口（S6，2026-11-11）

- canonical 入口约定：`case.json` 的 `calculation.source.kind` 必须是 `definitions_v3`；`le.ps1 run/validate/serve --case <case.json>` 直接解析到 `unified:definitions_v3`，不得静默回退 profile/hit/legacy 模型。公开版不提供作者案例；用户新建案例按 `examples/_template/` 或 `import` 生成。
- 迁移文件策略：历史 profile/hit/legacy case 仅作迁移证据，不参与新 run 的默认解析；该约定适用于任何保留迁移输入的案例。
- 详细验证、适配器边界与未迁移案例清单属于作者本地实施记录，不随公开仓库分发；以 `docs/contracts/` 与 `docs/current-status.md` 为准。

## 13. definitions_v3 候选发现与适用性判定（第五步 G1，2026-11-11）

- 自动候选来自 `knowledge/definitions/*.json`，元数据来自 `knowledge/definitions/index.json`
  （schema `le-definition-catalog-v1`）；扫描不来自 examples/reports，也不读取模板里的 canonical_case。
- 候选元数据至少包含 template_id、schema/binding_version、ability_id、plan_version、revision、
  sha256、capabilities、applicability。
- `capabilities` 必须是已知能力；候选发现会比较当前 plan 版本支持的能力白名单，缺口返回
  `capability_gap`，不按文件名/日期选第一条。
- `applicability` 只使用稳定事实：ability_id、class、mastery、技能标签、build revision、dataset id。
  definite 不匹配（职业/标签）返回 `not_applicable`；缺事实返回 `undetermined`；不适用/损坏/能力缺口
  都保留原因。不允许用旧 item_instance_id/roll/节点点数倍率作为匹配键。
- research/未声明来源且有主技能时：单一 applicable 返回 `needs_input` + source.candidate +
  `scenario_required`（场景需由 G2 按当前输入生成后显式绑定）；多 applicable 或 undetermined 返回
  needs_input；零 applicable/能力缺口/不适用/坏模板返回 needs_research。禁止回退 profile_v2/
  definitions_v2/legacy。
- 显式选择的模板同样执行适用性/能力校验；definite 不适用或未知必需能力拒绝（capability_gap），
  缺事实不伪造匹配。
- 相关实现：`calculation_route.discover_definition_candidates`、`definitions_v3.check_applicability`；
  测试：`tools/phase3/tests/test_definition_candidates.py`。
