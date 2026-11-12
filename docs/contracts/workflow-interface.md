# 流水线工作流接口契约（workflow-interface）

**网页验证默认最小化**：仅生成数值/复用模板时只确认本次 HTML 和链接；修改页面时最多一次打开，修改交互时只确认一条直接相关路径。用户未明确要求时不执行完整网页验收。具体规则见 `AGENTS.md` 的“网页验证的默认最小范围”。


## 日常执行顺序（用户要求）

1. 先冻结、解析用户本次指定的存档，生成独立 BuildSpec 与 calculation-input。只检查当前输入和解码必需依赖；不要运行默认案例 doctor，也不要在导入前完整检查任何旧案例。
2. 导入后按当前构筑的技能、节点、被动、装备、神像、祝福及词条稳定 ID 查询沉淀知识，绑定当前实例的数值和点数。只按命中记录追溯必要证据；历史案例没有默认优先级。
3. 复用统一 DPS 流程的属性、乘区、暴击、目标、时间与报告组件。hit/DoT 仅表示击中与持续伤害的计算组件差异；技能行为由数据和规则表达，不能再按技能建立独立 DPS 模型。
4. 优先生成当前 BD 的结果和网页；区分研究/候选产物与可用于当前声明范围的计算结果。机制未知可以显式假设或缩小范围继续；已知来源、执行或账本错误不能用 partial 掩盖，也不能据此更新可用 canonical。
5. 复用已验证能力且未改变语义时，默认不追加测试阶段。新增或修改计算、绑定、条件、投影或门禁语义时，按受影响边界做少量正例/反例；既有同边界证据可复用，不按技能数或词条数铺测试。候选网页可先生成，相关最小检查在宣称该范围可用前完成；直接决定可计算性的检查可前置。细则见 `docs/workflow.md`“新机制接入与最小语义验证”。完整回归仍须用户单独明确要求。

所有历史案例地位相同，均为以前使用或建立流程留下的实例。案例名不决定默认输入、必读材料、知识检索顺序、回归基线或交付门槛。“走全套流程”不等于授权全仓测试。


日期：2026-11-07。状态：**P0 定稿逻辑契约；已按用户裁定 1-C / 2-A / 3-A 与独立 review R1/R2 修订**。
模块与 schema 路径在本文件确定，实现与契约验证在 P1–P4 落地。本文不声称下列接口、目录或命令已经存在。

修订记录：2026-11-07 P0 review 后 —— §5.1 增加逐条匹配语义（R1）；§10.3 改为完整场景 + 双向绑定（R2）；
新增 §10.4 端点配置与报告选择（3-A）；玩家文本定义见 [player-report-ui.md](player-report-ui.md) §2.3（1-C）。

依据：作者本地的《BD 导入与 DPS 机制复用第一版实施方案》（实施记录，不随公开仓库分发）§9.1（四类必须预留的接口）、
§8（增量执行与手动更新）、§9（布局与产物）、§10（P0 完成定义）；玩家报告与本地重算边界引用
[跨 BD 玩家 HTML 与本地重算契约](player-report-ui.md)（HTML-01～12、LIVE-01～08）。

## 1. 范围

本契约定义四类接口的数据形状、边界与模块落点：

| 类别 | 接口 | 本契约章节 |
|---|---|---|
| A 阶段执行 | `execute_stage(request, context) → StageResult` | §4 |
| B 领域查询与计算 | `lookup_mechanisms`、`resolve_build`、`simulate`、`validate_result` | §5 |
| C 研究交接 | `make_research_task`、`validate_proposal` | §6 |
| D 结果提交与恢复 | `commit_operation`、`StateStore.load/save` | §7 |

同时确定：§8 模块与文件落点、§9 知识目录的权威源与生成物、§10 模型视图与本地重算能力边界。

**不在本契约范围**：伤害公式、规则数值、机制结论、HTML 视觉规范（后者见 player-report-ui.md）、
LangGraph 或任何外部编排框架的具体接入方式。

## 2. 通用约定

### 2.1 版本

- 契约代号：`le-workflow-v1`。每个 payload 的 `schema_version` 取各自独立值（见各章），
  不共用一个大版本号；未知 `schema_version` **明确报错**（`unsupported_schema_version`），不做静默降级。
- 只允许**增加可选字段**保持兼容；破坏性变化必须定义迁移，或明确拒绝恢复旧状态。
- `schema_version` 只管理程序数据契约，**不改变**游戏版本更新政策（见实施方案 §8）。

### 2.2 引用与路径

所有跨阶段、跨接口的输入输出使用 `artifact_ref`，不传对象实例：

```json
{
  "artifact_id": "calc-input:<case>",
  "path": "examples/<case>/inputs/calculation-input.affix-db.corrected.json",
  "asset_id": null,
  "schema_version": "calculation-input-v1",
  "sha256": "b6d2aaa222dcc71b97ed5d037f22bf2e503c4d89bf0efce882d67ecd0204b8e2"
}
```

- `path` 为**仓库根相对**路径；外部归档用 `asset_id`（解析依据作者本地资产清单；公开仓库不包含该清单与归档文件），二者互斥。
- 读取引用时必须核对实际文件 `sha256`；不一致返回 `stale_ref`，不得继续使用。
- payload 中不得出现本机绝对路径、打开的文件、数据库连接、Python 对象或模型会话。

### 2.3 状态词表（必须分开表达）

| 字段 | 取值 | 含义 |
|---|---|---|
| `execution_status` | `completed` / `waiting` / `failed` | 本次执行发生了什么 |
| `readiness` | `ready` / `partial` / `needs_research` / `needs_input` / `null` | 业务目标是否具备条件；尚无法评估时为 `null` |
| `issue.severity` | `error` / `warning` / `info` | 问题严重度 |
| `cache.status` | `hit` / `miss` / `disabled` | 缓存结果 |
| `evidence_state` / `implementation_state` | 沿用现有机制词表 | **与 readiness 独立**；软件验证通过不等于游戏机制确认 |

禁止把 `execution_status=completed` 当成“BD 接入完成”；禁止把“解析成功”写成“DPS 已支持”。

### 2.4 错误与控制流

- 业务函数返回结果或抛出明确异常；由阶段包装层转换为结构化 `issues`。
- **不得**通过 `print` 文本、`sys.exit`、终端 `input()`、隐藏全局变量或模块级单例控制下一步。
- 终端日志可保留，但不能成为唯一机器接口。
- 需要外部信息时返回 `execution_status=waiting` + `issue.required_input`，由调用方决定如何提问与恢复。
- 领域函数不内置无限重试；重试上限、分支与暂停由调用方负责。

### 2.5 幂等与不可变

- 同一 `operation_id` 重复提交的语义见 §7.3。
- 正式产物（dataset/revision/run/规则发布）不覆盖；新输入产生新 id。

## 3. 四类接口总表

| 接口 | 签名（逻辑） | 输入 | 输出 |
|---|---|---|---|
| 阶段执行 | `execute_stage(request: StageRequest, context: StageContext) → StageResult` | 阶段请求 + 显式上下文 | 阶段结果（产物引用、状态、issues、metrics、cache） |
| 机制查询 | `lookup_mechanisms(query: MechanismQuery) → MechanismQueryResult` | 结构化查询 | 机制条目、适用性、规则/证据引用、未决项 |
| 构建解析 | `resolve_build(request: ResolveBuildRequest) → ResolveBuildResult` | 冻结输入引用 + 主技能 | analysis-plan、已绑定机制、缺口清单 |
| 计算 | `simulate(request: SimulateRequest) → SimulateResult` | 快照/规则/场景/实现依赖引用 | 数值结果/账本或产物引用 |
| 结果校验 | `validate_result(request: ValidateResultRequest) → ValidationResult` | 结果/产物引用 + 校验项 | 结构化校验结果 |
| 研究任务 | `make_research_task(gap: ResearchGap) → ResearchTask` | 具体缺口 | 可交接的研究任务 |
| 提案校验 | `validate_proposal(proposal: ResearchProposal) → ValidationResult` | 研究提案 | 可接受项、错误、后续动作 |
| 结果提交 | `commit_operation(request: CommitRequest) → CommitResult` | 逻辑操作、输入指纹、候选产物、前置条件 | 正式引用或冲突 |
| 状态存储 | `StateStore.load(run_key) / StateStore.save(state)` | 运行键 | 执行状态 |

CLI 只负责参数转换与显示；CLI、本地编排与未来图节点调用**同一个**核心函数。

## 4. A. 阶段执行契约

### 4.1 StageRequest（`le-stage-request-v1`）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schema_version` | string | 是 | `le-stage-request-v1` |
| `stage_id` | string | 是 | 稳定阶段标识（见 §4.4） |
| `case_id` | string | 是 | 案例 ID（`examples/<case>/case.json` 的 `case_id`） |
| `run_id` | string | 是 | 本次运行的稳定 ID；同一运行内所有阶段一致 |
| `input_refs` | artifact_ref[] | 是 | 本阶段声明的输入；读取时校验 sha256 |
| `parameters` | object | 否 | 阶段参数（如 `main_skill`、`scenario_override`）；不含业务状态 |
| `dependency_fingerprints` | object | 是 | `{input: sha256, rules: sha256, implementation: sha256, dependencies: {name: sha256}}` |

`dependency_fingerprints` 用于缓存键与失效判断，纳入真正影响结果的输入、配置、规则、处理器代码与共享依赖；
**排除** `generated_at`、输出目录、绝对路径等非语义字段。

### 4.2 StageContext（不持久化）

| 字段 | 说明 |
|---|---|
| `repo_root` / `asset_root` | 仓库根与外部资产根（后者按作者本地资产清单解析；公开仓库不包含外部归档） |
| `output_root` | 本次运行的输出根（仓库根相对或已解析路径） |
| `adapters.clock` | 唯一时间来源；禁止各模块自行 `datetime.now()` 写语义字段 |
| `adapters.knowledge_index` | 机制查询适配器（§5.1） |
| `adapters.simulate_engine` | 计算适配器（§5.3） |
| `adapters.report_renderer` | 报告渲染适配器（能力描述见 §10） |
| `adapters.fs` | 原子写入/临时目录适配器 |

`context` 显式传入，不作为业务状态持久化，也不混入伤害公式；不得通过模块级可变全局传递。

### 4.3 StageResult（`le-stage-result-v1`）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schema_version` | string | 是 | `le-stage-result-v1` |
| `stage_id` | string | 是 | 与请求一致 |
| `execution_status` | enum | 是 | `completed` / `waiting` / `failed` |
| `readiness` | enum \| null | 是 | 见 §2.3 |
| `output_refs` | artifact_ref[] | 是 | 本阶段产物；空数组表示无产物 |
| `issues` | issue[] | 是 | 无问题时为 `[]` |
| `metrics` | object | 是 | 至少含 `elapsed_ms`；可含计数与体积 |
| `cache` | object | 是 | `{status, key, reason, invalidated_by, input_fingerprint, implementation_fingerprint, parameters_fingerprint, dependencies, written}`；P4 缓存实现见 §4.6 |

`issue` 结构：

```json
{
  "code": "missing_skill_mapping",
  "severity": "error",
  "message": "技能节点 24 的语义不在任何已登记机制中",
  "related_ids": ["fr11mv:24"],
  "retryable": false,
  "required_input": {"kind": "mechanism_mapping", "artifact_hint": "knowledge/mechanisms/"},
  "details": {}
}
```

### 4.4 稳定 stage_id

`freeze_input` → `parse_input` → `normalize_build` → `export_calc_input` → `apply_affix_ranges` →
`resolve_skills` → `prepare_snapshot` → `min_validate` → `simulate` → `validate_result` →
`render_report` → `commit`。

- 每个阶段分别可调用，可单独返回 `waiting`；下游阶段不得读取上游未发布的临时产物。
- 阶段顺序与依赖在 P3 落为编排表；阶段 id 一经发布不得改名（改语义需新 id）。

### 4.5 示例（阶段成功）

```json
{
  "schema_version": "le-stage-result-v1",
  "stage_id": "resolve_skills",
  "execution_status": "completed",
  "readiness": "needs_research",
  "output_refs": [
    {"artifact_id": "analysis-plan:<case>",
     "path": "reports/<case>/<run-id>/analysis-plan.json",
     "asset_id": null, "schema_version": "le-analysis-plan-v1", "sha256": "<hex>"}
  ],
  "issues": [
    {"code": "unmapped_skill_node", "severity": "warning",
     "message": "节点 22 的交互尚未登记", "related_ids": ["fr11mv:22"],
     "retryable": false, "required_input": null, "details": {"blocks": false}}
  ],
  "metrics": {"elapsed_ms": 42, "mechanisms_bound": 31, "gaps": 5},
  "cache": {"status": "miss", "key": "<sha256>", "reason": "first_run", "invalidated_by": []}
}
```

### 4.6 阶段缓存与指纹（P4 落地）

- **fingerprints**：`input_fingerprint`（规范化 `input_refs` 内容 sha + 依赖阶段指纹）、`implementation_fingerprint`（handler 源 + 共享实现 + 阶段相关规则/源码）、`parameters_fingerprint`（规范化参数，排除 `generated_at`/输出目录/run_id 等非语义字段）；均由 canonical JSON sha256 计算，确定性、无机器绝对路径。
- **cache_key** = `sha256({schema, stage_id, input_fingerprint, implementation_fingerprint, parameters_fingerprint, dependencies})`；`dependencies` 为上游 stage → 上游 cache_key 链。
- **缓存条目**：`data/cache/pipeline/<stage_id>/<cache_key>.json`（`le-stage-cache-v1`；原子写），含 stage/cache_key/指纹/outputs[{name,relative_path,sha256,bytes,cached_path}]/readiness/issues/producer/created_at；payload 副本与条目同目录。命中前逐项校验 payload 存在与 sha，任一缺失/篡改 → miss 重跑。
- **命中行为**：不执行 handler/子进程；将 payload materialize 到本次 run 目录，`StageResult.cache={status:"hit",key,...,written}`。
- **`--no-cache`**：跳过缓存读取、强制执行；`cache.status="disabled"`（reason=`no_cache_requested`），成功后仍刷新写缓存；`LE_PIPELINE_CACHE_DISABLE=1` 同义。缓存写失败只记录 warning，不影响主流程。
- **级联**：实现/参数/输入变化只使该阶段及下游 miss；上游与无关阶段保持 hit（per-stage 参数相关性在编排层声明）。
- 缓存目录可用 `--cache-root` 或 `LE_PIPELINE_CACHE_ROOT` 隔离；误删/损坏缓存可由完整执行重建（见 §7.4 StateStore 恢复）。

### 4.7 run 阶段图与验证门禁（P4 G1 落地）

- 正式 `run`（BH/FR）按阶段图执行：`simulate → validate_result → render_report → commit`，通过 `execute_stage` + StageCache + FileStateStore；`--publish none|staged` 共用同一阶段图。
- `simulate` 只产结果/中间物（`render_html=false`，domain additive `output_dir_display` 指向最终目录）；`render_report` 独立渲染离线 HTML（报告缓存/端点/渲染器指纹接线不变）。simulate/validate 缓存键不含目标/run-id，materialize 后按当前 final 路径重绑定；render 缓存键含 `recompute_endpoint` + `renderer_fingerprint`（+target）。
- `validate_result` 写 `validation-result.json`（passed/accepted/checks + 语义 `result_sha256`，排除 run_id/out_dir/repro/generated_at 等非语义字段）；`commit` 阶段要求 `passed=true`、accepted 非空且当前 result 语义 sha 与 validation 绑定一致，未接受/失败不得发布（无正式目录、无 operation record）。
- `commit` 阶段调用 `commit_operation`/`publish_run_dir`；staged 由候选租约 → 路径稳定 → 门禁 → 原子发布；`run` 的 commit 阶段永久不缓存（避免跳过提交）。
- 默认 `--publish none`：最终目录原地执行，pipeline-state 写入但不进 run-manifest、无 operation record；`--publish staged`：候选+租约，完成后原子发布，pipeline-state 含四阶段与 `last_commit`。
- `update_pointer` 已实现（白名单指针文件、expected 前置、intent 恢复、原子替换，见 §7.5）；`run --resume` 未单独新增（同候选重试+阶段缓存覆盖中断恢复）。

P4 第二轮审查修复补充（G1 集成边界）：
- **run-id 重绑定**：候选生成使用唯一占位符 token，最终只替换 token 并按 schema 重绑 `run_id`/`out_dir`/`result_ref`/产物路径；不替换任意键名、数值或业务字符串（`baselines`/`build_base` 等保持原样）。
- **StateStore 输出引用**：阶段 output_refs 在缓存物化/路径重绑的同一界面对应最终实际字节；`run-manifest.json`/`pipeline-state.json` 不作为阶段 output_refs（避免 state↔manifest 循环哈希）。成功 cold/warm/staged run 的 `FileStateStore.verify()` 必须通过。
- **实现指纹分组**：simulate/validate 的实现依赖排除纯 UI/渲染资源（report_ui/**、renderer、html/report renderer 等）；render_report 保留全量渲染依赖。纯 UI 资源变化只使 render_report 失效。
- **commit 发布意图**：发布前写 `.intents/<safeOp>.json`（operation_id、input fingerprint、候选树/文件 sha、target、attempt）；记录写失败返回 `record_write_failed`（已发布、未记录、可重试）；同 op+同 input 重试以意图原始发布摘要核验目标，补记录并返回 `replayed`，正式目录字节不变。无意图的外部既有目标仍为 `target_exists`。

## 5. B. 领域查询与计算接口（可绕过编排独立调用）

四条接口都不要求聊天上下文、全局当前案例或运行中的 agent；均可用纯 JSON 输入输出。

### 5.1 lookup_mechanisms

```text
lookup_mechanisms(query: MechanismQuery) -> MechanismQueryResult
```

`MechanismQuery`（`le-mechanism-query-v1`）：`schema_version`、`queries[]`、`filters`、`limit`、`index_ref`、`namespaces`。

- `queries[]`：每项 `{query_id, term, kind, namespace?}`，`kind ∈ {id, alias, keyword, stat_key, node_id, ability_id, item_id}`；
  `query_id` 在本次请求内稳定，用于逐条回报匹配结果。
- `filters`：`component`（`hit`/`dot`/`tick`/…）、`tags`、`node_ids`、`conditions`。
- **命名空间必填**：节点/属性/物品/技能标识必须带命名空间（如 `ability:fr11mv`、`node:fr11mv:16`、`stat:fire_damage`）；
  无命名空间的裸数字不作为可绑定 ID。
- `index_ref`：显式索引/资产根（仓库相对路径或 `asset_id`）。可由调用方配置或服务实例提供，**不得**依赖隐藏的
  "当前案例"或聊天状态。

按实施方案 §5 的查询次序执行：精确 ID → 组件/标签/节点/条件过滤 → 别名/stat_key/计算块/关键词 → 候选。

`MechanismQueryResult`（`le-mechanism-result-v1`）：`entries[]`、`unmatched_queries[]`、`summary`、`index_digest`。

`entries[]` 每条至少包含：

| 字段 | 说明 |
|---|---|
| `mechanism_id`、`revision` | 稳定机制标识 |
| `matched_queries[]` | 每条 `{query_id, selector, match_quality, match_reason}`；`match_quality ∈ {exact, candidate}`，记录命中该条目的**具体查询项与选择器** |
| `applicability` | `{status ∈ {satisfied, unknown, not_applicable}, evaluated_on[], missing_conditions[]}`；必要条件不足返回 `unknown`，**不得与 `satisfied` 混同** |
| `rule_refs`、`evidence_refs`、`implementation_refs` | 规则/证据/实现入口 |
| `evidence_state`、`implementation_state`、`unresolved[]` | 与 readiness 独立的状态 |

- `unmatched_queries[]`：`{query_id, term, reason}`，**批量请求不得因部分命中而丢掉其它来源的查询项**。
- `summary.match_quality`：只作结果集摘要，**不参与自动绑定**。
- 自动绑定只接受"至少一条 `match_quality=exact` 且 `applicability.status=satisfied`"的条目；候选与 `unknown` 进入缺口/人工确认。
- 查询只读，不修改规则、不触发研究。

### 5.2 resolve_build

```text
resolve_build(request: ResolveBuildRequest) -> ResolveBuildResult
```

输入：`case_id`、`main_skill`（`{ability_id, name?}`）、`input_refs`（BuildSpec/calculation-input/dataset）、
`compatibility_baseline`。输出：`analysis_plan`（`le-analysis-plan-v1`）、`bound_mechanisms[]`、
`coverage`（included/excluded_with_reason/unresolved/out_of_scope）、`gaps[]`、`readiness`。
匹配过程中**不启动研究、不修改规则**；缺失项进入 `gaps`。

### 5.3 simulate

```text
simulate(request: SimulateRequest) -> SimulateResult
```

输入：`snapshot_ref`、`ruleset_ref`、`scenario_ref`、`implementation_fingerprints`（含共享算子/handler 版本）、
`model_capabilities_ref`（见 §10）。输出：结果数值/账本（或产物引用）、`readiness`、`issues`、`warnings`。
同一输入与实现指纹必须得到同一结果。研究出的新规则先按声明语义做必要验证、记录证据/假设并绑定版本，再显式重新解析；这不要求游戏机制已完全确认或先完成全部研究。不能由 `simulate` 内部偷偷改规则。

### 5.4 validate_result

```text
validate_result(request: ValidateResultRequest) -> ValidationResult
```

输入：结果/产物引用、校验项列表（结构、绑定指纹链、双基线口径、一致性与覆盖）、容差。
输出（`le-validation-result-v1`，`kind="result"`）：`passed`、`checks[]`（`id`、`status`、
`expected`、`actual`、`tolerance`、`detail`）、`blocking[]`。不得修改 artifacts。

### 5.5 新执行能力的语义门禁（现行要求）

按 `docs/workflow.md`“新机制接入与最小语义验证”选择本次边界，不扩大全量测试。`validate_result` 的 PASS 必须说明实际检查项；文件 SHA、字段存在、枚举合法和数值互相重复不等于语义正确。

所有正式分支（含 assembly/actor）都需要来源/所有权、组件引用、应用摘要/coverage 投影、分项与窗口/总量一致性的适用检查；可以委托公共算子。引用不能从待校验记录自身扩张合法集合。新增/修改门禁时选一个相关篡改反例，必须调用阶段真正使用的验证入口，不只调用未接线的 helper。

必需事实缺失/身份冲突不能回退成场景默认成功；可选来源不存在/零点与数据无法验证要分别表达。未知游戏规则允许显式假设、缩小范围或 needs_research；已知实现错误不以 partial 继续发布受影响数值。未实现门禁是实施缺口，本文更新不构成其已落地证明。

## 6. C. 研究交接契约

### 6.1 ResearchTask（`le-research-task-v1`）

| 字段 | 说明 |
|---|---|
| `schema_version`、`task_id`、`gap_id` | `task_id` 稳定可追踪；同一输入指纹下同一 `gap_id` 不重复创建 |
| `input_fingerprint` | 缺口所基于的输入/规则指纹 |
| `question` | 具体缺哪条来源语义、乘区分组、节点交互或时间行为 |
| `source_refs[]` | 稳定 ID、原始字段、原文位置 |
| `related_mechanism_refs[]` | 最相关候选机制与证据入口 |
| `researched_already[]` | 已查来源与为何仍未解决 |
| `affected` | 受影响组件与计算块；`blocks_target`（布尔） |
| `deliverable_kind` | `mapping` / `parameter_rule` / `handler` / `bounded_assumption` |
| `verification_required[]` | 期望的验证要求（正向 + 边界/反向场景） |

### 6.2 ResearchProposal（`le-research-proposal-v1`）

| 字段 | 说明 |
|---|---|
| `schema_version`、`proposal_id`、`task_id` | 必须指向存在的任务 |
| `based_on_fingerprint` | 与任务输入指纹比对；不同则 `stale_proposal` |
| `outcome` | `proposed_change` / `unresolved` / `needs_input`（与证据确认程度分开） |
| `mechanism_refs[]`、`mapping_refs[]`、`code_change_refs[]` | 候选变更，均以产物/变更引用提交 |
| `evidence_refs[]` | 证据定位 |
| `assumptions[]` | 显式假设及其影响范围 |
| `verification_refs[]` | 已验证项与场景 |

### 6.3 validate_proposal

返回 `ValidationResult`（`kind="proposal"`）：`accepted[]`、`errors[]`、`next_actions[]`、
`outcome_effective`（提案声明与证据强度是否一致）。校验任务存在、指纹未过期、契约完整、证据定位存在、
验证项齐备；**不把研究文本直接变成可执行规则**。通过验证后的发布走 `commit_operation`。

V1 允许人工或当前 agent 填写同一提案文件；后续自动研究节点只需遵循本契约。

## 7. D. 结果提交与恢复契约

### 7.1 CommitRequest（`le-commit-request-v1`）

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | 是 | `le-commit-request-v1` |
| `operation_id` | 是 | 一次**逻辑操作**的稳定 ID（恢复/重试沿用，不每次新生成） |
| `attempt_id` | 是 | 一次执行尝试的 ID（仅用于审计） |
| `operation_kind` | 是 | `new_revision` / `publish_dataset` / `write_run` / `publish_rule` / `register_mechanism` / `update_pointer` |
| `input_fingerprint` | 是 | 本次操作输入的内容指纹 |
| `candidate_refs` | 是 | 候选产物引用（已写临时位置、已通过结构与指纹检查） |
| `expected_current_revision` | 否 | 需要更新指针时必填 |
| `preconditions` | 否 | 例如目标 run_id 不存在、revision 未被引用 |

### 7.2 CommitResult

`{operation_id, status: committed|replayed|conflict|failed, committed_refs[], current_revision,
issues[], attempt_id}`。`replayed` 表示同操作同输入命中已有正式产物并原样返回。

### 7.3 幂等矩阵

| 情形 | 行为 |
|---|---|
| 同 `operation_id` + 同 `input_fingerprint` | 核验已有正式产物后返回同一 `CommitResult`（`replayed`），不重复发布 |
| 同 `operation_id` + 不同 `input_fingerprint` | 返回 `conflict`，不覆盖已有记录 |
| 临时产物存在但未发布 | 视为未成功；恢复时核验正式产物，或在清理本操作未完成的临时产物后重做 |
| 文件已发布、状态记录未完成 | 恢复时核对已发布产物并补齐操作记录（不得重复创建 revision/规则/报告） |

写入顺序：**候选产物通过校验 → 原子发布正式产物 → 更新指针/操作记录**。
P4 实现范围说明：`commit.py` 已实现 `write_run` / `write_file` / `write_dir` 的幂等提交、原子发布与恢复；
P5 落地：`update_pointer` 已实现——白名单指针（`data/current.json`、`data/builds/<source>/<share>/current.json`）、
`expected_current_revision`/`expected_current_sha256` 前置（锁内重读当前内容）、同卷 staging + fsync + `os.replace` 原子替换、
R2 intent 恢复、幂等/冲突/并发语义（§7.5）。`write_run`/`write_file`/`write_dir` 仍为通用发布路径；
`new_revision` / `publish_dataset` / `publish_rule` / `register_mechanism` 的**专用事务**仍未实现（各自可用通用 candidate 发布组合，
但未提供 kind 级事务 API，需在后续按需补齐）。`run --publish staged` 是当前 CLI 热路径集成（workflow.md §2.2）；
prepare 的阶段状态仍在最终 run 目录按阶段原子写，不做整目录发布。`recover_operations` 目前未把 `.intents`/pointer 巡检纳入报告。

正式产物与指针更新之间允许中断，但必须可恢复；使用最小必要的锁/事务或原子替换。

### 7.5 update_pointer（P5 落地）

```text
update_pointer(candidate_file, target_pointer, *, operation_id, attempt_id, input_fingerprint,
               expected_current_revision=None, expected_current_sha256=None, operations_root, root, ...) -> CommitResult
```

- **白名单目标**：`data/current.json`、`data/builds/<source>/<share>/current.json`；其他目标 → failed `pointer_target_not_allowed`；路径穿越拒绝。
- **前置必填**：`expected_current_revision` 或 `expected_current_sha256`（可接受当前指针 `dataset_id`/`revision_id`/`build_id`/`revision` 等标识）；目标必须已存在且为完整 JSON，candidate 必须完整非空 JSON。缺失/不符/损坏/非法分别返回明确 failed 原因。
- **原子替换**：`atomic_io.atomic_replace_file` 在目标锁内校验 candidate sha 与**锁内重读的当前内容**，同卷 staging + fsync + `os.replace`；不允许半写/空文件。
- **幂等/恢复**：同 op+同 input → replayed（不重写）；不同 input → conflict；记录写失败 → `record_write_failed`（intent 保留可重试）；重试用 intent 原始新指针 sha 核验正式目标 → 补记录 `replayed`，指针字节不变；替换前崩溃的 stale intent 清理后可继续；并发同旧内容最多一个 committed。
- **可变指针与不可变产物区分**：pointer 目标允许 subsequent operation 在正确 expected 下连续推进（old→A→B），历史 operation 记录保留审计但不永久占有目标；不可变 run/file/dir 仍保持 owner/no-clobber。指针被合法推进后，旧 operation 同 op+同 input 重试返回 `replayed` + `pointer_advanced_after_commit`，不改变当前新指针、不误判历史提交损坏。
- **expected CAS 绑定同一内容**：指针字节只读取一次，用同一份 bytes 校验 SHA/revision 标识并计算固定 sha；该固定 sha 同时作为 intent `previous_sha256` 与原子替换的 `expected_current_sha256`，锁内再次读取当前内容只与该固定值比较，不一致 → failed `current_revision_mismatch`，不重新采样绕过调用者 expected。
- **推进前恢复待决记录**：任一 pointer 更新在原子替换前，若存在同 target、无 operation record 且 candidate sha == 当前目标字节 sha 的完整 intent，先补写该 operation 的 record（recovered）并删除 intent，再推进；补记录失败/锁不可用 → 返回 retryable `pending_pointer_recovery`，不推进指针。
- **stale 清理重新确认（R4）**：扫描结果仅供候选分类；取得原 operation 锁后，再取得与原子替换相同的目标发布锁，重读 record/intent 与目标字节。仅当前 SHA 仍等于 previous_sha256 时可清理未发布 intent；已变化或读取/锁失败时保留 intent，返回 retryable `pending_pointer_recovery`，由原操作重试补记录。不得以扫描前 SHA 删除恢复证据。
- **intent target 绑定**：新 pointer intent 写 top-level `target_path`（须等于唯一 candidate path）；读取/恢复扫描校验 top 与 candidate path 一致。top/path 不一致、结构非法、candidate sha 不符或目标内容篡改 → fail-closed（B 不推进，A 重试 `intent_corrupt`），不把篡改/未发布 intent 标记 committed、不回退当前指针。
- 仍未实现：`new_revision`/`publish_dataset`/`publish_rule`/`register_mechanism` 的专用事务；`recover_operations` 未纳入 `.intents`/pointer 巡检。

### 7.4 StateStore

```text
StateStore.load(run_key: str) -> PipelineState | None
StateStore.save(state: PipelineState) -> None
```

`PipelineState`（`le-pipeline-state-v1`）至少含：`run_key`（`case_id` + `run_id`）、
`stages[]`（`stage_id`、状态、输入/实现指纹、产物引用、耗时、缓存信息）、
`pending_input[]`、`last_commit`（`operation_id`、结果）。每个阶段边界保存。

- V1 落点：`reports/<case>/<run>/pipeline-state.json`（单文件，最小实现），未来由适配器接到新的检查点存储。
- **每次运行只有一个执行状态权威源**；`run-manifest.json` 记录审计事实、机制库记录跨 BD 知识，
  两者都不替代执行状态。
- 更换代码或依赖后恢复时先比对指纹，按实施方案 §8 失效相关阶段；不承诺任意旧检查点跨代码恢复。
- 未来框架的任务 ID 在外层映射，不嵌入 BuildSpec、机制 ID 或 DPS 结果语义。

P4 落地补充：`pipeline-state` 每阶段新增 `parameters_fingerprint`、`cache_key`（additive），`last_commit` 字段记录
`{operation_id, status, attempt_id, input_fingerprint, committed_refs[{path,file_count}], recorded_at}`。
staged run 的 `last_commit` 在发布前写入候选（避免发布后改写破坏 replay tree sha），因此 replay 时正式目录内保留
原 `committed` 记录、CLI 输出 `replayed`；提交权威结果以操作记录 `data/cache/operations/` 为准。
`--resume` 按输出存在+sha、阶段完成状态与 input/implementation/parameters/cache_key 指纹级联校验；坏 state 备份为
`pipeline-state.corrupt-<timestamp>.json` 后重建。


## 8. 模块与文件落点（P0 定稿）

| 职责 | 路径 | 引入阶段 |
|---|---|---|
| 本契约 | `docs/contracts/workflow-interface.md` | P0（本次） |
| 阶段/提交/研究/状态的数据类与校验 | `tools/phase3/src/le_calc/workflow/contract.py` | P2 |
| `execute_stage` 与阶段注册表 | `tools/phase3/src/le_calc/workflow/stage.py` | P3 |
| `resolve_build` / `simulate` / `validate_result` 薄封装 | `tools/phase3/src/le_calc/workflow/domain.py` | P2 |
| `make_research_task` / `validate_proposal` | `tools/phase3/src/le_calc/workflow/research.py` | P2 |
| 原子写/临时目录/目录树 sha | `tools/phase3/src/le_calc/workflow/atomic_io.py` | P4 |
| 阶段缓存存储与 materialize | `tools/phase3/src/le_calc/workflow/stage_cache.py`（`le-stage-cache-v1`） | P4 |
| `commit_operation` 幂等提交适配 | `tools/phase3/src/le_calc/workflow/commit.py` | P4 |
| `StateStore` 适配 | `tools/phase3/src/le_calc/workflow/statestore.py` | P4 |
| 机制查询与索引构建 | `tools/knowledge/src/le_knowledge/` | P1 |
| 共享算子包装与 handler 注册 | `tools/phase3/src/le_calc/mechanics/` | P2 |
| prepare 轻量编排 | `scripts/le_pipeline/`（输入路线与依赖描述；由 `scripts/le_commands.py` 调用） | P3 |
| 阶段执行实现 | `tools/phase3/src/le_calc/workflow/stage.py`（`execute_stage` + `STAGES` 注册表） | P3 |
| 阶段状态存储（V1） | `tools/phase3/src/le_calc/workflow/statestore.py`（文件版 `StateStore`；P4 增加指纹/cache 字段与恢复校验） | P3/P4 |
| expected 检查共享实现 | `tools/phase3/src/le_calc/workflow/expected_check.py`（CLI 与 `validate_result` 共用） | P3 |
| 接口 schema（stage/research/commit/analysis-plan） | `tools/phase3/schemas/workflow-v1/*.json` | P1 起草，P2 定稿 |
| 知识记录 schema（mechanism / skill template） | `knowledge/schemas/*.json` | P1 |
| 机制/技能权威内容 | `knowledge/mechanisms/`、`knowledge/skills/` | P1 |
| 可重建查询索引 | `data/cache/knowledge.sqlite` | P1 |
| 可再生阶段缓存 | `data/cache/pipeline/`（条目 `le-stage-cache-v1` + payload） | P4 |
| 运行伴随产物 | `reports/<case>/<run>/{analysis-plan,coverage,research-queue,pipeline-state,reuse-summary,report-capabilities}.json` | P2–P4 |

命名说明：编排包用 `scripts/le_pipeline/` 而非 `scripts/pipeline/`，避免与既有
`tools/save_import/scripts/pipeline/` 混淆。

**不改名既有稳定产物**：`result`（`result-v1`）、`source-ledger`、`rule-ledger`、`run-manifest`
保持现有消费者可读；新增诊断信息先放上述伴随文件。需要扩充既有 schema 时同步更新消费者与测试。

## 9. 知识目录：JSON 权威源 + 脚本生成索引

用户决策（2026-11-07）：**JSON 为唯一权威源，Markdown 索引页由脚本生成。**

| 对象 | 位置 | 提交 | 说明 |
|---|---|---|---|
| 机制元数据 | `knowledge/mechanisms/*.json` | 是 | 手工/agent 维护的权威源；字段见实施方案 §5 |
| 技能模板 | `knowledge/skills/*.json` | 是 | 组件、参数绑定、调度/频率、支持交互、验证用例 |
| 记录 schema | `knowledge/schemas/*.json` | 是 | 上述两类记录 + analysis-plan 的最小 schema |
| Markdown 索引页 | `knowledge/INDEX.generated.md` | 是 | **脚本生成，禁止手改**；带 `GENERATED` 头、`source_digest` 与生成命令；手工改动在 rebuild 时被覆盖并告警 |
| 运行分析计划 | `reports/<case>/<run>/analysis-plan.json` | 随 run | 由 `resolve_build` 产出，schema 在 `tools/phase3/schemas/workflow-v1/` |
| 查询索引 | `data/cache/knowledge.sqlite` | 否 | 可删除重建；不作为知识源 |

规则：

1. `knowledge/` 只保存**元数据、选择条件、参数绑定说明、规则/证据引用**；可执行规则仍在
   `tools/phase3/rules/`，正式证据仍在 `docs/evidence/`，**不复制第二份**。
2. `le.ps1 knowledge rebuild` 从 JSON 文本源重建 SQLite 与 Markdown 索引；记录集合或内容指纹变化时
   标记并重建相应记录（索引不得长期滞后）。
3. 查询默认输出短摘要，详情按 ID 获取；无 FTS5 时功能仍可用（普通索引 + 维护的别名/关键词）。
4. `docs/mechanisms.md` 增加统一查询入口链接（人工维护一次），历史结论保留不回改。
5. 外部归档只索引清单与明确证据入口，不在每次查询时扫描逆向目录；路径解析用 `asset_id` + 配置，
   复制工程后不依赖原机器绝对路径。

## 10. 模型视图与本地重算能力边界（P0 定稿）

### 10.1 能力描述（capabilities）

每个模型/技能适配器必须声明一份 `capabilities`（`le-model-capabilities-v1`），供报告器与服务路由消费，
不允许由渲染层或 JS 自行硬编码：

| 字段 | 说明 |
|---|---|
| `model_id`、`skill_id`、`ui_contract_version` | 模型标识与所遵循的玩家报告契约版本（当前 `le-player-report-ui-v1`） |
| `pages[]` | 启用的通用页（`overview`/`composition`/`sources`/`buffs`）与模型专页（`ignite`/`hit`/`timeline`/…） |
| `target_fields[]` | 目标面板可编辑字段（`monster_level`、`resistances.*`、`enemy_state.*`）及其范围 |
| `buff_controls[]` | Buff 卡控件声明（`state_key`、类型 `toggle`/`stacks`、上下限、只读、默认值） |
| `windows[]`、`modes[]` | 支持的聚合窗口与策略（如 FR 的 5/30/120s、cooldown/attack-speed 口径） |
| `profile_states` | baseline profile → states 映射（切预设时页面据此更新控件；服务端按 profile 默认 states 计算双基线） |
| `recalculation` | `{supported, request_kind, response_kind, bindings_kind, report_config_schema, session_scoped_revisions, client_session, capabilities_version, binding_checks[], commands[]}`；`client_session` 声明 `client_session_id` 是否必填/格式/生成方式/旧客户端策略 |
| `informational[]` | 已知不支持项（进入数据与短范围标签，不做成假开关） |

渲染器指纹的**唯一权威实现**是 `le_calc/report_fingerprint.py`（O-C：`report_capabilities` 与 `report_ui.document` fallback 都委托它，
覆盖 `report_ui/**` 全部文件与 assets + `report.py`/`hit_report.py`/`html.py`）；页面 meta、bindings、artifact、报告缓存键必须同值。
能力描述落在运行伴随文件 `reports/<case>/<run>/report-capabilities.json`，**不塞进 `result-v1`**；
如需进入 `run-manifest` 必须同步 schema 与消费者。相关元数据至少记录
`ui_contract_version`、`renderer_fingerprint`、`capabilities_digest`、`acceptance_refs[]`。

### 10.2 serve 路由

- `le.ps1 serve --case <case.json>` 按案例的模型能力选择适配器，绑定该案例的快照、规则、场景与端口。
- 未知模型/能力缺失时**明确报不支持**，不得回落 BH ruleset loader 或默认 registry（Stage7 已复现该失败形态）。
- 网页与 CLI 调用同一 Python 领域计算入口与同一场景规范化逻辑；JS 不复刻伤害公式、命中权重或技能政策。

### 10.3 重算请求/响应契约（完整场景 + 双向绑定，裁定 3-A / R2）

请求（`le-recalc-request-v1`）**提交完整可编辑场景**，不使用未定义含义的 `states_edit` 大口袋：

| 字段 | 说明 |
|---|---|
| `schema_version`、`request_id`、`client_edit_revision`、`client_session_id` | `request_id` 仅回显用于关联；`client_edit_revision` 在同一 `client_session_id` 内严格递增（服务端按 `(report run_id, client_session_id)` 隔离 accepted revision，旧/重复版本拒绝 `stale_edit_revision`）；`client_session_id` 每次页面加载生成，跨标签页/刷新互不干扰；缺省=旧客户端兼容（隔离 request_id 作用域，响应 `client_session_legacy=true`） |
| `case_id`、`model_id`、`capabilities_version` | 案例/模型与能力描述版本 |
| `report_ref` | 本次计算所基于的报告引用（见 §10.4），含 run 与页面端点 |
| `input_fingerprints` | `{calc_input, ruleset, snapshot, dataset}` 的 sha256 |
| `scenario` | **完整场景**：`baseline_profile`、`target`（等级/抗性/敌人状态）、`states`（Buff/层数）、`mode_policy`（模式、窗口、策略）、`model_parameters`（如命中次数/权重覆盖）；字段集合由 `capabilities` 声明 |

- 若改用 patch：必须定义**完整字段清单**、合并规则与 `base_scenario_revision`；未声明字段一律拒绝，不能静默合并。
- 服务端校验并规范化场景（schema、字段白名单、数值范围/层数上限、过指纹），按**规范化后的完整场景**计算
  `applied_scenario_digest`；非法值/不支持字段/错误案例/过期规则明确拒绝。

响应（`le-recalc-response-v1`）：

| 字段 | 说明 |
|---|---|
| `schema_version`、`request_id`、`client_edit_revision`、`client_session_id`、`client_session_legacy` | 回显；客户端据此判定是否仍为最新有效请求（会话/序号不匹配必须丢弃）；`accepted` 响应的 `accepted_revision` 必须等于本次 `client_edit_revision` |
| `accepted` | 布尔；`false` 时附 `issues[]`，不得把旧结果标为已应用 |
| `case_id`、`model_id`、`capabilities_version`、`input_fingerprints` | **双向绑定**：客户端校验与服务端一致 |
| `applied_scenario` / `applied_scenario_ref` + `applied_scenario_digest` | 实际应用场景（内联或产物引用 + 摘要） |
| `result_digest`、`view_digest`/`html` | 结果摘要与页面（V1 允许整份 HTML，由共享渲染器生成并保留配置与视图状态） |
| `recompute_endpoint` | 本页面后续重算应使用**实际**端点（见 §10.4） |

客户端只有在**绑定一致、`client_edit_revision` 未变化、且响应对应最新有效请求**时才应用结果；
仅回显 `request_id` 不足以防旧响应覆盖（需配合序号/revision 或 AbortController）。
保存配置、载入配置、切换预设、重置与编辑控件走**同一场景模型**；不支持字段与非法值明确拒绝。
保存/载入文件契约为 `le-report-config-v1`：`{schema_version, binding, scenario}`，`binding` 为页面 bindings 载荷、`scenario` 为完整规范化场景；
Python 权威校验器 `recalc.validate_report_config(payload, *, current_bindings, capabilities)`（文件版 `validate_report_config_file`）与页面 JS
使用同一 schema（`tools/phase3/schemas/le-report-config-v1.json`）、同一错误码与顺序（共享样例 `le-report-config-v1.examples.json`）；
无 binding 的历史配置返回明确 legacy 结论并拒绝自动应用（错误码 `legacy_config_no_binding`），不静默视为有效绑定。

### 10.4 端点配置与报告选择（裁定 3-A）

1. **唯一端点来源**：`run`/`reproduce`、共享渲染器与底层入口使用同一 `recompute_endpoint` 配置
   （默认值 `http://127.0.0.1:8765/recompute` 只是**配置默认值**，JS 不再写死常量）。
2. **首次生成与每次重算都带实际端点**：初始 HTML 与每次重算返回的整份 HTML 都必须内嵌当前实际端点；
   只改首次生成会让重算后页面回落默认端口。
3. **报告选择可校验**：新增 `--report`（或显式 `report_ref`）绑定本次 run/case/model/输入与规则指纹/页面端点；
   默认可选规范 run，但输出必须写明所选报告；非规范报告必须可显式指定。
4. **serve 启动校验**：所选报告元数据（case/model/指纹/端点）与实际监听地址、CLI 端口一致；不一致时**明确报错**，
   并给出"仅重新渲染该报告"或"生成新 run"的命令。不得回改历史 run 的 HTML。
5. **逐请求绑定校验**：启动校验不能替代每次请求/响应的绑定校验（§10.3）；同端口的错误案例/过期报告不得因"服务存在"判成功。
6. **元数据载体**：优先使用计划中的伴随文件 `report-capabilities.json`；不未经同步 schema 直接改旧 `run-manifest`。
   服务连接配置与模型能力描述分开记录。
7. **缓存边界**：报告缓存键纳入端点/端口与 `ui_contract_version`/渲染器指纹；**数值缓存键不因端口变化而失效**。
   改端口可复用既有数值，仅生成新的 HTML/报告产物，不覆盖历史报告。
8. **离线行为**：页面加载不主动访问本机服务，只有用户点击计算才请求；默认 8765 不构成通用连接保证，
   "显示连接失败"也不证明端口绑定正确。
9. **用户明确要求完整服务验收时的参考范围（默认不执行）**：非默认端口初次生成 → 连续两次重算仍用该端口；BH/FR 两个服务不同端口并存；
   端口不一致时明确失败；错误报告/案例绑定被拒绝；改端口只失效报告缓存；历史报告保持不变。

### 10.5 缓存边界

- 数值缓存键：输入/规则/实现指纹 + 场景 + 模型能力版本。
- 报告缓存键额外纳入视图适配器（`le_calc.report_fingerprint.renderer_fingerprint()`，O-C 单一来源，覆盖 `report_ui/**` 与全部 assets + 模型渲染器）、共享模板/文案/能力描述、`ui_contract_version`、bindings/result 摘要与**端点/端口**；
  仅改页面或端口时复用数值并**重建 HTML**，不得因数值缓存命中而输出旧界面。
- 端口变化只失效报告缓存，不失效数值缓存；历史报告产物不被覆盖。

## 11. 与验收用例的映射

| 实施方案 §11 | 本契约覆盖 |
|---|---|
| 13 接口独立性 | §5 四条接口可在无 LangGraph、无聊天上下文环境独立调用；与 CLI 同输入同结果 |
| 14 状态与交接 | §4 StageRequest/StageResult、§6 ResearchTask/Proposal 可 JSON 往返；§7.4 StateStore 支持“执行→等待输入→重启→恢复” |
| 15 研究提案边界 | §6.3 错误 task_id / 过期指纹 / 契约不完整 / 验证失败均返回明确结果且不发布；unresolved 正常结束 |
| 16 写入可重试 | §7.3 幂等矩阵与中断恢复 |
| 18 本地重算 | §10.2 路由、§10.3 完整场景请求/响应与双向绑定、§10.4 端点/报告选择、§5.3 唯一计算入口 |
| 19 状态与绑定 | §10.3 绑定校验、§10.4 报告/端口校验、LIVE-04～08 的请求/响应语义 |
| 21 报告缓存与新 BD | §10.4 报告缓存键（含端点/端口）、§10.5 缓存边界 |

## 12. 待定（P1–P4）

1. 各接口 schema 的**必填/可选字段最小集**与 JSON Schema 文件内容（P1 起草，P2 定稿）。
2. `knowledge/` 记录字段最终形态与 revision 策略（P1）。
3. 重算响应用“整份 HTML”还是“结构化视图”的最终选择（P3R 决定；V1 允许整份 HTML）。
4. `report-capabilities.json` 是否并入 `run-manifest`（当前决定：先做伴随文件）。
5. 阶段编排的具体依赖表与并发/锁粒度（P3/P4）。
6. `data/cache/**` 是否纳入打包与 `.gitignore`（P1 连同 `scripts/package.py` 一起确认）。
7. `--report` / `report_ref` 的 CLI 形态与默认选择规则（3-A 落地时定稿；默认规范 run 但必须显式输出所选报告）。
8. 细分计时口径（把模拟、条件重算、报告组织与 HTML 渲染拆开）在 P4 前给出（R5）。

**P3 落地决策（2026-11-07，实施中）**：

- 阶段 id 固定为 §4.4 的 12 个；`prepare` 默认执行到 `prepare_snapshot` + `resolve_skills`（产出 analysis-plan/coverage/research-queue 与 pipeline-state），不默认 `simulate`；`run` 继续负责计算与报告。
- `le.ps1 prepare --case <case.json> [--save <存档> --main-skill <id> | --planner <json>] [--resume]`；首次创建与恢复必须显式区分（`--resume`）。
- expected 检查统一到 `workflow/expected_check.py`，CLI（`le_commands._check_expected*`）与 `domain.validate_result` 共用；check 名/指针/容差/run_expectations 语义保持不变。
- 修复 P2 #18：`validate_result` 对 reproduce 产物按 corrected-input 派生关系校验绑定（`case.calculation_input` + `corrections` → `steps/04`），不再直接比较 case 原始 sha；`simulate` 产物保持原语义。
- 输入路线与外部依赖描述放 `scripts/le_pipeline/`（RouteSpec + dependency check）；缺依赖返回 `needs_input`，不静默切换语义不同的解析路线。
- `StateStore` V1 = `reports/<case>/<run>/pipeline-state.json`（阶段边界保存；恢复时重算输出 sha 校验，不信任进度标记）。


### 新存档首接 V1 入口补充（2026-11-09）

正式首接契约见 `docs/contracts/save-onboarding.md`。`import --main-skill bh2 --model black-hole` 先创建独立 BuildSpec 与案例输入，再复用 prepare/run 阶段图；
既有 prepare 的 pinned revision 语义与 run 的验证发布门禁保持。首接报告独立记录执行步骤、耗时、BuildSpec 导入退出码、knowledge readiness；
初始 expected 不含本次计算的伤害预期。知识适用条件不在此次变更范围。

## 组件知识复用 V1（2026-11-10）

resolve_build 从完整 BD 的稳定组件 ID 批量检索共享知识，按当前实例绑定参数；case_scope 对 shared 记录表示已验收案例。沿用 analysis-plan/coverage/research-queue，新增字段与当前参数语义见 `docs/contracts/component-knowledge-v1.md`。计时仅放在 metrics，分析计划保持确定性；存档接入继续调用现有 prepare 阶段接口。

## 统一计算计划接入（第三步）

统一 DPS 正式路径在既有 `le-workflow-v1` 词表内新增 `compile_plan` 阶段，随后使用
`simulate` / `validate_result` / `render_report` / `commit`；统一分支不创建旧
`inputs.snapshot`/`expected`/`ruleset.path` 伪文件，计划产物与门禁见
[calculation-route-v1.md](calculation-route-v1.md)。legacy `black-hole`/`hit` 的既有阶段门禁
保持不变；`calculation_plan` 与 `analysis-plan.json` 是不同对象，不能互相替代。

