# 统一计算计划契约 v1（`le-calculation-plan-v1`）

状态：现行（第二步实现契约，2026-11-10）。适用范围：`dps` 组件的统一计算入口。

> 术语：**计划**指编译器产出的 `calculation-plan.json`；**场景**指用户/默认的 Buff 与目标配置；
> **执行器**指 `le_calc.plan_executor.execute_plan`。计划不是 profile、不是结果、不是历史
> `analysis-plan-v1.json`，也不负责获取过程与动作模拟。

## 1. 唯一数值执行链

```
当前输入（calculation-input / build-spec / dataset / profile-v2 或原生技能模板）
  → prepare_context（加载、哈希校验、冻结上下文；此阶段可读文件）
  → compile_from_profile_v2 / compile_from_definitions（生成计划 + 诊断；可读 catalog）
  → calculation-plan.json（纯 JSON，含 plan_sha256）
  → execute_plan(plan, scenario, runtime)（唯一执行器；只消费计划/场景/handler registry）
  → le-dps-result-v3（追加 plan 绑定字段）
```

当前 `dps.calculate` 与 `scripts/le_commands.py run` 都走这条链；旧的“先算再拼报告”路径已移除。
计划编译与执行分离：执行期不读取 case/profile/catalog/dataset，也不调用旧计算入口。

## 2. 文件与模块

| 对象 | 位置 | 说明 |
|---|---|---|
| JSON Schema | `tools/phase3/schemas/calculation-plan-v1.json` | 顶层字段/类型/枚举契约；结构校验实现见 `calculation_plan.validate_plan_structure` |
| 契约工具 | `tools/phase3/src/le_calc/calculation_plan.py` | canonical JSON、`plan_sha256`/`plan_id`、完整性/结构校验、读写 |
| 编译器 | `tools/phase3/src/le_calc/plan_compiler.py` | `prepare_context`、`compile_from_profile_v2`、`compile_from_definitions` |
| 执行器 | `tools/phase3/src/le_calc/plan_executor.py` | `validate_plan`、`validate_scenario`、`execute_plan`、账本/覆盖投影 |
| handler registry | `tools/phase3/src/le_calc/plan_handlers.py` | 编译期/执行期 handler 白名单、版本与实现指纹 |
| 原生模板 schema | `knowledge/schemas/skill-template-v2.json` | 原生定义入口（`le-skill-template-v2`） |
| 覆盖/账本口径 | `tools/phase3/src/le_calc/dps_result.py` | 编译期静态缺口与运行时缺口合并 |

运行目录（`dps.run`）新增：

- `calculation-plan.json`：真正参与计算的计划；
- `plan-diagnostics.json`：编译诊断（覆盖缺口、重复注入、来源缺失等）；
- `run-manifest.json` 增加 `plan_sha256` / `plan_id` / `plan_readiness` / `scenario_sha256` /
  `execution_fingerprint` / `executor_fingerprint`；
- `report-binding.json` 的 `ruleset` 指纹覆盖计划依赖（profile、属性转换规则、编译器/执行器实现）；
- `input-snapshot.json` 记录 `calculation_plan` 与计划依赖清单。

## 3. 计划字段

| 字段 | 语义 |
|---|---|
| `schema` / `plan_id` / `plan_sha256` | 版本、`cp-<sha256>`、canonical JSON 哈希（去掉 `plan_id`/`plan_sha256` 后计算） |
| `subject` | case/build/revision/main_ability 稳定身份 |
| `compiler` | `version`、`origin`（`profile_v2`/`native_template`）、实现文件指纹 |
| `executor_requirements` | 执行期 handler ID+版本、执行器实现指纹 |
| `input_refs` | 所有编译依赖（role、路径、sha256）；含计算输入、build、dataset 指针与内容、profile/内容、属性转换规则、编译器来源文件、模板/机制库（原生入口） |
| `sources` | 来源实例身份 + 语义 + 绑定状态 + 证据入口；不含未解析的裸字符串数值 |
| `facts` | 场景无关事实：角色、属性结果、装备事实、技能/异常常量、覆盖基线缺口、静态问题、显示数据 |
| `parameters` | 技能基础值/组件参数、暴击规则、时间规则、异常声明、穿透规则、冻结参数 |
| `controls` | 状态与目标控制项（key、类型、默认、范围、只读）；执行前按此校验场景 |
| `conditions` | 受限条件程序：`always|all`，term 仅 `control_true/control_equals/control_count/target_true/code` |
| `components` | hit/dot 组件声明（标签、暴击能力、自适应类型）；数值参数在 `parameters.skill` |
| `effects` | 按执行顺序排列的贡献/规则效果；含 handler ID/版本、来源、条件、值模式、覆盖状态、账本模板 |
| `evaluation` | 执行阶段顺序与依赖（静态→节点→属性→效果→状态→移速→冷却） |
| `schedule` | `fixed_repeat`、间隔策略、首次施放、窗口、每第 N 次命中（只允许 `hit`/`from_first_hit`） |
| `diagnostics` | 编译诊断；`affects_coverage=true` 的条目必须与 `facts.coverage_base_gaps` 相互背书 |
| `assumptions` / `scope` | 假设、范围与固定条件口径 |
| `presentation` | 报告显示数据（角色、装备、控制项、技能名）；执行器不再查目录 |

### 3.1 effects 的 value_mode

| `value_mode` | 执行器取值 |
|---|---|
| `static` | 编译期冻结值（能力/来源/条件只在 `included_static` 与 `condition_code` 中表达） |
| `state_count` | `per_state × 激活状态数 × (1 + scaling_sum)`，状态数来自场景 |
| `stack_scale` | `base_value × 场景层数`；有 `cap_per_point` 时按当前点数封顶（节点每层加成） |
| `movement_conversion` | `ratio × 已生效 movement_speed 汇总`（运行时值，不在编译期冻结） |

场景条件不参与编译期删除规则：编译只冻结来源与参数；开关 Buff、层数、目标标记由执行期
`condition_ref`/`condition_code` 求值。条件无法判定时贡献不生效并在账本/`exclusion_reason`
中写明“无法判定”，不得静默当 false。

### 3.2 单一权威字段（禁止镜像）

执行器只读取以下权威字段；计划内同义镜像字段视为非法（`authority_conflict`）：

| 语义 | 唯一权威字段 | 说明 |
|---|---|---|
| hit/dot 组件标签、暴击能力、自适应类型 | 顶层 `components`（恰好一个 hit + 一个 dot） | 不得在 `parameters` 内重复声明组件 |
| 技能基础参数 | `parameters.skill`（基础伤害、added scaling、暴击率/倍率、冷却、动作、技能名） | 校验覆盖执行器实际读取值；基础伤害缺失/全 0 拒绝 |
| 每第 N 次命中 | `schedule.every_nth_hit` | `parameters.every_nth_hit` 为废弃镜像，出现即拒绝 |
| 窗口/首次施放 | `schedule` | 窗口正数、首次施放非负 |
| 异常声明 | `parameters.ailments` | 名称/伤害类型/持续/基础伤害校验 |
| 场景变量 | `controls` | 执行前按此校验场景 |

计划必须先经过 JSON 序列化/反序列化再校验；校验器使用的是同一权威字段，不允许内存共享
引用掩盖“校验一套、执行另一套”。

### 3.3 来源、所有权与覆盖裁决

- 每个 `emits_contribution` 的 effect 必须带非空 `source_refs`，且每个引用都能在 `sources`
  中找到；`sources` 的 `source_id` 唯一、`kind` 在白名单、`identity` 非空、保留 `evidence`
  与 `binding_status`。
- `source_refs` 是执行约束，不只是展示：来源绑定失败（source_absent/unresolved/
  identity_mismatch/semantics_mismatch/unverified）不得支撑“已生效/已计入”的效果
  （`source_status_conflict`），否则拒绝。
- 每个可执行 effect 必须带 `ownership_key`（来源实例 + mechanism/rule + 输出 stat/operation/
  标签/目标组分）。
  - **同所有权且同语义**的重复引用（同一来源实例、同一输出通道、同数值/单位/条件）可以合并：
    保留一个 effect，合并 `source_refs`/证据，记录 `duplicate_merged`；
  - **同所有权但不同定义**（数值/单位/条件程序/值模式不同）是冲突：编译必须返回
    `ownership_conflict` 并阻止生成计划，不得保留首条或末条；
  - **不同合法实例**（不同来源实例或不同输出通道）分别保留，不得相互覆盖；合法实例之间
    的正常叠加仍按乘区语义处理。
- profile 的 `coverage.confirmed_inactive/not_applicable_mods/assumed_active_mods` 必须编译为
  `facts.coverage_decisions`：每条已应用裁决包含当前输入解析出的身份、证据入口、前提
  （require 事实或明确的作用域排除/假设）与状态（verified/assumed）；缺失 reason/evidence/
  mod 实例、身份锚点不一致或前提不满足 → 声明被拒绝（`coverage_decision_rejected`），
  原缺口保留。禁止仅凭 mod_id 把来源加入“已处理”集合。
- 已应用的覆盖裁决同样参与计划校验：缺身份/证据/满足前提的裁决会被拒绝
  （`missing_decision_identity`/`missing_decision_evidence`/`invalid_decision_premise`）。

### 3.4 固定阶段与依赖

`evaluation` 只支持固定阶段，不接受任意 DAG/脚本声明：

- `stage_order` 必须等于
  `baseline → pre_attribute → pre_attribute_effects → attribute_conversion →
  attribute_scaling → derived_pools → damage_components → frequency → projection`；
- `dependencies` 必须等于上述相邻阶段的固定依赖链；任意/反向/循环/未来阶段声明被拒绝
  （`invalid_stage_order`/`invalid_dependencies`）；
- `effect_stage` 必须覆盖全部 effects，且每个 effect 的 `stage` 与其 category 的固定阶段一致；
  属性转换阶段必须早于属性缩放（`stage_violation`/`invalid_effect_stage`）；
- `facts.attribute_views` 保存 original/converted/effective 属性视图，用于属性依赖核对。

## 4. 两个编译入口

### 4.1 profile-v2 适配器

`compile_from_profile_v2(context, profile)`：

- 校验 profile schema/控制项/组件/来源锚点/规则参数（复用第一步 `effect_binding.validate_profile`）；
- 全局绑定：装备事实、基础/词条/被动映射、技能节点、属性转换/缩放、装备词条/来源规则、
  状态缩放、移速转换、异常额外概率、冷缩节点；
- 当前版本只冻结当前构筑的可静态确定部分：节点点数、roll、词条数值、属性结果等在编译期解析；
  实例身份在编译期校验（定义 ID + 语义锚点），执行期不再读取目录；
- `attribute_conversion` 在加载期与运行期都要求有效 `expected` 身份锚点（第一步修复保持）；
- 覆盖基线：未映射/未验证/来源缺失/常量不一致/声明不生效等形成 `diagnostics` 与
  `coverage_base_gaps`；结果是否 `partial` 由执行期合并运行时异常施加记录后给出。

### 4.2 原生定义入口

`compile_from_definitions(context, template, mechanisms=None, rules=None)`：

- 输入 `le-skill-template-v2`：技能参数、hit/dot 组件、静态效果、条件、异常、控制项、调度；
- 可用 `mechanism_refs` 合并共享规则库中**不依赖实例绑定**的条目（如目标易伤、每第 N 次）；
  需要 `current:matched_instances` 或目录绑定的条目会被明确拒绝，不得用近似实现兜底；
- 与 profile 计划使用同一 schema、同一 `executor_requirements`、同一执行器；
- 至少两份独立最小合成定义（`tools/phase3/tests/fixtures/calculation-plan/`）覆盖：
  不可暴击 Hit、Melee Hit + DoT（标签隔离）——它们只是验证夹具，不冒充真实游戏技能；
- 新增定义只需增加数据，不新增执行器分支；技能 ID 不得参与编译/执行分支。

## 5. 执行器边界与校验

`execute_plan(plan, scenario, runtime=None)` 执行前必须通过：

1. `verify_plan_integrity`：schema、`plan_sha256`、`plan_id` 一致（手改内容被拒绝）；
2. `validate_plan_structure`：必填字段、类型、枚举、引用、唯一 ID、handler 字段；
3. handler 校验：effect 的 handler 必须已注册且版本匹配；`executor_requirements` 声明的
   handler 必须存在于当前 runtime；执行器实现指纹必须一致；未知 handler 不得走近似实现；
4. 覆盖诊断背书：`coverage_base_gaps` 与 `diagnostics[affects_coverage]` 必须一一对应；
   执行期新增的“异常施加来源未建模”缺口来自实际组件装配证据，会合并进最终 coverage，
   不要求在编译诊断中预先出现；
5. 来源/所有权：`source_refs` 必须可解析、失败来源不得支撑生效效果、`ownership_key` 唯一；
6. 覆盖裁决：已应用 `coverage_decisions` 必须有身份/证据/满足前提与合法状态；
7. 固定阶段/依赖：`stage_order`、`dependencies`、`effect_stage` 必须匹配固定契约，拒绝环、
   反向、未来阶段或任意依赖；
8. 场景校验：状态/目标键完整、类型/范围、只读值、非法层数一律拒绝；
9. 失败边界：计划被拒绝/阻塞时不执行；覆盖 partial 可以执行，但结果与 HTML 明确展示
   `coverage.status=partial` 与缺口；CLI 返回 `EXIT_PARTIAL=3`。

执行器唯一允许的数值能力来自 `plan_handlers.EXECUTOR_HANDLERS`：条件求值、hit/dot 组件装配、
时间/窗口、目标侧、增伤/更多/暴击/标签、攻速/冷却恢复池、账本与覆盖投影。registry 之外的
实现（包括未来新增分支）必须在计划 schema/契约中显式扩展，不得由计划文本动态导入。

## 6. 结果与重算绑定

`le-dps-result-v3` 保持第一步字段，并追加：

- `plan_ref = {schema, plan_id, plan_sha256}`；
- `plan_sha256` / `scenario_sha256` / `execution_fingerprint`。

重算/报告交互继续使用第一步绑定契约：

- `report-binding.input_fingerprints` 的四键不变；`ruleset` 汇总 profile、属性转换规则、
  编译器/执行器实现指纹；
- 输入变化时绑定校验拒绝“用新规则重算旧报告”；新规则必须生成新报告；
- 因此第一步旧报告（`dps-step1-coverage-3`）与补修前旧契约计划（`dps-step2-plan-1`）的
  依赖/实现指纹与当前执行器不同，交互重算/直接执行会被明确拒绝并要求新报告或重新编译；
  `dps-step2-plan-2` 是补修中间产物，最终以 `dps-step2-plan-3` 为准；旧报告 HTML/历史产物
  本身不被改写；
- 同一输入重算由同一 `dps.calculate` 入口重新编译，确定性得到相同 `plan_sha256`，
  数值与覆盖与运行目录一致（定向测试 B14）。

## 7. 版本与兼容边界

- 计划 schema 版本化：`le-calculation-plan-v1`；新增 handler/字段需升级版本并保留校验，
  不得静默扩展语义；
- 旧 `analysis-plan-v1.json` 不被重命名/覆盖，也不作为执行输入；
- 第一步的旧 profile（`dps-profile.json`）只作迁移基线；当前 case 的 `dps_profile` 指向 v2；
- 计划执行只依赖计划自身与运行时 registry，因此历史计划能否重放取决于：
  计划完整性、handler 版本/实现指纹、场景兼容性；不满足时明确失败并要求重编译。

## 8. 非目标

- 不把历史 `reports/**` 冻结产物改写成新计划的输出；
- 不把“计划可执行”解释为所有机制已建模：未建模/未验证来源仍以 coverage 缺口呈现；
- 不引入 workflow/staged/black-hole/Flame-Reave 等其它模型路径的改道；
- 不实现动作/资源/获取过程模拟（固定条件估算边界不变）。
