# plan-v4 / result-v6 契约（现行约束与演进记录）

> 本文中作者本地的历史 run 路径统一记作 `reports/<case>/<run-id>/`；这些运行产物不随公开仓库分发。运行/复现方式是 `prepare → run → validate/serve`（见 `docs/workflow.md`），不要从本文历史 run-id 推断当前报告。

- 状态：新增版本，**不修改 v1/v2/v3** 的字段、schema、已签收计划与结果。
- 实现：`tools/phase3/src/le_calc/plan_v4.py`；schema
  `tools/phase3/schemas/calculation-plan-v4.json`、`tools/phase3/schemas/result-v6.json`。
- 现行生产链：真实 FrozenContext → definitions_v3 绑定 → ResolvedModel/装配 → plan-v4 的 `execution.execution_v1` → 公共执行与 result-v6。`compile_plan_v4(template, case, scenario)` 的合成夹具入口不能替代真实来源绑定。
- 能力按当前模板、schema、handler 和组合约束声明；VC/FR/BH 等历史切片只证明其已验收范围，不自动证明新 actor/转换/事件能力。当前报告引用从所选 case 读取，不从本文历史 run-id 推断。
- 唯一运行时权威为 `execution.execution_v1`；外层镜像与 `assembly.legacy_plan` 是兼容/来源记录，不得继续驱动另一套结果。静态折叠可保留，但推导关系要可校验，条件/场景变化不能被旧常量覆盖。
- 来源身份和必需绑定失败不得回退到成功默认；类型/作用域、来源所有权、条件、应用账本与 computed coverage 的约束对每个正式分支一致。新增受限能力允许先交付明确假设下的部分结果，不要求先完成任意战斗模拟。
- 新增语义/门禁的最小证据按 `docs/workflow.md`“新机制接入与最小语义验证”，阶段验证按 `docs/contracts/workflow-interface.md` §5.5。文档提出约束不等于代码已实现；缺失检查应如实记录，不能用旧分支 PASS 继承新能力签收。

**阅读顺序**：以上为现行约束；下文 §1–§5 保留初版字段设计，§6–§18 保留迁移演进与当时 run 证据。其中“尚未迁移”“下一步”“当前 canonical”等只描述所在阶段，不作今日状态或新案例门槛；最新序列化字段以版本 schema 与当前执行契约共同核对。

## 1. 能力矩阵

| 能力 ID | 含义 | 需要的数据 |
|---|---|---|
| `hit_components` | 瞬时命中组件（可暴击） | `components[].kind=hit` |
| `dot_components` | 纯 DoT 逐跳组件 | `components[].kind=dot` + tick/duration |
| `applications` | 具名独立施加 cohort（ailment/damage/scaling/duration/clock/amount/source/target） | `applications[]` |
| `charge_schedule` | 充能/施放占用/多实例并发调度 | `schedule.kind=charge` + `charges` + `action_policies` |
| `presentation` | 数据化报告标题/组件/application 标签 | `presentation` |

未知能力或未声明所需能力在编译期返回 `capability_gap`，不静默忽略；超矩阵组合
应显式扩展 capability/契约，或拒绝该组合并登记能力缺口；不得静默回退另一计算路径。

## 2. target_side（显式目标侧）

每个组件/application 通过 `target_ref` 引用一条 target_side 记录，记录一次且只一次：

```json
{"ref":"syn-cold-hit","damage_channel":"cold","resistance":0.20,
 "penetration":0.10,"damage_reduction":0.50,
 "strategy":"separate_multiplier","negative_resistance_policy":"allow",
 "unit":"fraction","multiplier":0.44}
```

- 固定单位：`unit=fraction`；抗性/穿透/DR 均为 fraction，转换只在数据入口一次。
- 穿透只应用一次：
  - `separate_multiplier`：`(1 + penetration) × (1 - resistance) × (1 - DR)`；
  - `fold_into_resistance`：`(1 - (resistance - penetration)) × (1 - DR)`。
- `negative_resistance_policy=allow` 保留负有效抗性；`clamp_at_zero` 钳制到 0。
  `separate_multiplier + clamp_at_zero` 无已确认语义，构造策略直接拒绝。
- O1（base 100、res .2、DR .5、pen 0.1）：separate = **44**，fold = **45**。
- 未知 strategy/channel/unit、NaN/Inf、越界 DR、重复/缺失 target_ref 均在编译期拒绝。
- 旧计划兼容口径只做**命名映射**，不向旧计划写字段：
  v1 `legacy_profile_fold_negative_allow`；v2 `fr_separate_multiplier_table_dr`；
  v3 `bh_fold_scenario_dr`（见 `COMPATIBILITY_TARGET_POLICIES`）。

## 3. applications（通用独立施加）

```json
{"application_id":"syn-poison","ailment_id":"Poison","damage_type":"Poison",
 "base_damage":10,"duration_s":2,"tick_interval_s":0.5,
 "clock":{"kind":"periodic","first_at_s":0,"interval_s":0.5,"count":4},
 "amount":{"rule":"fixed_expected_stacks","stacks_per_roll":0.5},
 "target_ref":"syn-poison"}
```

- 每批独立时钟与到期；伤害按 `duration_s / tick_interval_s` 的均匀 tick 支付
  （半开窗口：`t < window_s`），以支持批次/窗口/尾伤复算。
- 施加量规则白名单：`fixed_expected_stacks`。BH 的“角色火抗驱动 Ignite 数量”保留在
  v3 显式配方中，不是 v4 通用默认，也不按技能名分支。
- O2（body 10/0.5s×4=40；Poison 0.5 层/roll、每层 10 总伤、2s、4 tick）：
  批次到期 2/2.5/3/3.5，完整总伤 20；半开窗口 [0,2)=**12.5**、[0,3)=**18.75**；
  Poison 通道不读取 Fire 抗性。

## 4. charge 调度（条件驱动）

- `schedule.charges`：`max_charges/recharge_s/cast_time_s/instance_duration_s`。
- `action_policies[].condition` 只允许：
  - `charge_available_and_action_free`（可用即施放，允许实例重叠）；
  - `wait_for_all_instances_expired`（等待全部实例结束）。
- `policy_id` 只是展示/稳定标识，行为完全由 condition 决定；重命名 policy_id 不改变
  调度。未知 condition 拒绝。
- O3（max2、recharge2、cast1、duration3、horizon10）：
  可用即施放 starts=[0,1,2,4,6,8,10]，max_concurrent=3；
  等待结束 starts=[0,4,8]，max_concurrent=1。

## 5. result-v6

- `policy_results[]` 为主评估在首位的策略结果；`windows[]/totals[]/components[]/
  applications[]/applications_ledger/contribution_application_summary` 保留；
  `presentation` 从 plan 数据投影。
- `scenario` 身份（states/target）与 `primary` 投影保留；`coverage` 显式声明。
- 门禁 `validate_result_v6` 从 `casts/dot ticks/application cohorts` 复算窗口
  （hit/dot/application/total/dps/tail/cast_count）、totals、max_concurrent 与
  evaluation 摘要，并校验 application cohort/tick 总额、账本摘要、presentation/title；
  篡改任何窗口/DPS/tail/总数/评估/标签均拒绝。
- `render_report_v6` 只读取 `presentation` 与结果数据，不出现 BH/Ignite/Heat Wave
  等历史默认文案；现有 v5 BH renderer 作为显式兼容保留。

## 6. 生产边界与 VC 切片（2026-11-12）

- v4 生产入口复用 `load_compilation_inputs → prepare_context(FrozenContext) →
  definitions_v3.bind_template → ResolvedModel → effect_binding/contribution_mapping/现有装配`；
  不再 template+case 直编。输入 override/SHA、构筑身份、适用性、selector、sources/effects、
  来源所有权和 coverage 由既有真实装配链产生。
- **VC v4 原生执行切片（2026-11-12）**：v4 plan 顶层 `execution` 保存真实 effects/sources/parameters/schedule/strategies，并由 `execution_sha256` 绑定；`plan_v4.execute_plan_v4` 由 `execution` 声明驱动 `plan_handlers`/公共 mechanics，**不调用** `plan_executor.execute_plan(legacy_plan)`。`execution.components` 与 plan.components 引用一一对应；修改 effect 数值会实际改变结果。VC run 为作者本地运行产物。
- FR/BH 同样已改为 native execution：v4 plan 顶层 `execution` 携带真实 effects/sources/parameters/schedule/strategies，`execution_sha256` 绑定；v4 executor 由 execution 驱动公共 `plan_handlers`/mechanics，**不调用** `plan_executor.execute_plan`/`execute_plan_v2`/`execute_plan_v3`。`assembly.legacy_plan` 仅保留来源证据。
- VC 生产切片：v4 模板 `knowledge/definitions/void-cleave-fire-main-v4.json` 经真实装配生成
  **v4 封装计划**；计划 `assembly` 携带完整 legacy v1 计划（真实 effects/sources/input_refs/
  场景/窗口），result-v6 由既有真实执行映射，保留 `contributions`、`applications_ledger`、
  `effect_ledger`、`contribution_application_summary`、`scenario`、`windows`、`coverage`。
  这是正式 v4 计划/结果（capabilities/subject/presentation/指纹均有真实断言），不是旧计划
  兼容 run 的改名。VC canonical 见 `reports/<case>/<run-id>`；
  旧 v1 run 保留。
- 顶层 `sources`/`effects` 字段为兼容占位；唯一权威在 `execution`（以及 provenance `assembly`）。`execution.effects/sources/components/parameters/schedule/strategies` 必须完整存在并由 `execution_sha256` 校验；不得只复制 effects 到顶层冒充修复。
- v4 result/plan schema 已正式扩展：plan 增 `assembly`、`coverage`、`issues`、`assumptions`、
  `execution_fingerprint`、`scenario_sha256`、schedule kind `assembly`、coverage status
  `from_execution`；result-v6 增真实 `contributions`/`effect_ledger`/`scenario` 指纹等字段。
  v1/v2/v3 schema/已签收 run 不改。
- **FR/BH 尚未迁移**：仍为 plan-v2/v3；后续需按 VC 同样的真实装配切片迁移。v4 生产默认入口
  目前只对已支持的 VC 模板走通；未支持能力返回 `capability_gap`，不静默忽略。
- v4 HTTP `/recompute`：VC 已支持；请求场景变化影响结果、计划 `plan_sha256` 不变、
  `scenario_sha256`/`execution_fingerprint` 更新，再由真实执行器重算。
- 旧 v1/v3 handler 的 executor 私有反向依赖已在 G4/M7 闭合；不因 v4 改动重新引入。

## 7. 通用 execution 契约（S1，2026-11-12）

- JSON Schema：`tools/phase3/schemas/execution-v1.json`；Python 结构校验：`plan_v4.validate_execution_contract`。
- 唯一权威字段（顶层不再用技能名/ability_id/legacy_schema 分派）：
  - `components[]`：component_id/kind(hit|dot)/unit/damage_channel/damage_type/resistance_key/scope/duration_s/tick_interval_s/cast_ref；
  - `applications[]`：application_id/source_component/damage_channel/damage_type/resistance_key/scope/duration_s/max_stacks/condition/cohort_policy/window_policy/tail_policy；
  - `effects[]`/`sources[]`：真实贡献与来源数据；
  - `frequency`：driver/horizon_s/windows_s/policies(condition/interval_policy)/rotation/tail_policy；
  - `charge`：enabled/max_charges/recharge_s/cast_time_s/instance_duration_s/condition；
  - `coverage`：status/gap_count/gaps。
- 兼容：v1/v2/v3 plan/result 与旧 run 不改；现有 legacy execution 在 S2/S3 迁移前标注 `legacy-assembly`，S1 校验只对目标通用契约生效。
- S2 将从旧 FR/BH 抽取通用 application/coverage/window/tail/charge primitives 到 mechanics；S3–S5 逐案例切换，禁止按 ability_id/技能名/legacy_schema 分派。

## 8. S2 通用执行纵向切片（2026-11-12）

- 公共 primitives：`tools/phase3/src/le_calc/mechanics/execution.py` 的 `execute_execution_slice`，直接消费 execution 的 component/application/effect/source/frequency/target 字段；不读取 legacy rules/ailments。
- 字段：component 的 channel/scope/unit/duration/tick/base；application 的 source_component/channel/scope/duration/condition/cohort_policy/window_policy/tail_policy/damage_per_tick；frequency 的 horizon/windows/cast_interval/tail_policy；effect 的 channel_tags/scope/operation/value/condition/coverage_class。
- 结果：components 数值、applications_ledger、windows/totals、由实际 applied/not_applicable/unresolved 计算出的 coverage gaps（不复制声明 status）。
- 未知 unit/kind/scope/operation/cohort/window/tail policy 明确拒绝。
- 本切片仅证明通用数据+primitives；VC/FR/BH 尚未接入该数据模型，S3–S5 未开始。

## 9. S3 VC 通用 primitives 阶段（2026-11-12）

- execution-v1 扩展：component 增 condition/ref、damage_weights/hit_count/channel_tags/added_damage/increased/more；application 增 ailment_id/application_unit/condition_ref/chance/tick_interval_s/expected_stacks/ownership/ledger_ref；execution 增 condition_programs/ownership/ledger_refs/coverage_input。
- mechanics/execution 新增通用 primitives：`weighted_hit_damage`（added scaling/crit/weights/hit_count）、`ailment_expected_damage`（chance/stacks/duration/tick）、`project_computed_coverage`（由 ledger 排除计算）、`window_tail_total`（tail policy）。
- 测试 `test_step5_v4_execution_slice` 5 项通过（含这些 primitives 的正反例）。
- 尚未完成：真实 VC 装配输出这些 execution 字段、VC 执行接入、canonical/HTTP；S3 未闭合，S4/S5 未开始。

## 10. VC execution-v1 闭环

- 真实 VC v1 装配输出 `execution.execution_v1`：components/applications/effects/condition_programs/frequency/target/coverage_input；execution 执行只消费该对象与公共 handlers/mechanics。
- 运行期不按 ability_id/技能名/legacy_schema 分派；execution_v1 校验拒绝未知 component kind/cohort/window/tail。
- 新 canonical VC run（作者本地 `reports/<case>/<run-id>/`，plan-v4/result-v6，validate PASS）。

## 11. P1 唯一权威与来源约束

- 序列化后 `execution.execution_v1` 唯一权威；`_execute_native_v4` 不回读 `execution.parameters/schedule/controls/components/effects` 等 legacy 字段（compile-time 可从真实装配派生，运行期只读 execution_v1）。
- execution_v1 effects：每 emitted effect 的 source_refs 非空且解析到 sources；sources.source_id 唯一，binding_status 不得为 missing/unverified/unresolved；ownership_key 必填。来源正常不适用仍是 normal exclusion；来源缺失/未验证拒绝。
- 新 canonical VC run（作者本地运行产物）。

## 12. P1-B 来源/所有权强校验

- sources.binding_status 白名单：ok/verified/bound/mapped/applied；其余（identity_mismatch/missing/unverified/unresolved/conflict）拒绝。
- emitted effect 的 canonical ownership_key 必须唯一；同 key 不同定义（含改 effect_id/value）在执行前拒绝。
- 该次补修前后 plan/execution/scenario 指纹不变，结果仍为当前可复验输出。

## 13. FR execution-v1 闭环

- FR v2 装配输出 execution_v1：components/hit weights/effects/condition_programs/frequency driver+policies+rotation+evaluations+windows、target/controls、sources/ownership。
- native hit-events 只从 execution_v1 构造公共 primitive 输入；legacy execution 字段删除后仍全等。
- 新 canonical FR run（作者本地运行产物）。

## 14. BH execution-v1 闭环

- BH v3 装配输出 execution_v1：dot components、Ignite application、effects/conditions、frequency driver charge_dot_cycle + policies/charges/ignite、evaluations、sources/ownership、target。
- native charge/dot 只从 execution_v1 构造公共 primitive 输入；legacy execution 字段删除后仍全等。
- 新 canonical BH run（作者本地运行产物）。

## 15. FR/BH P1 修正

- FR component.base_damage 唯一权威；parameters.skill.base_damage 镜像若保留且冲突执行前拒绝。
- BH application 必须通过公共能力矩阵（scope=ailment、damage_type 属于组件类型、ailment_id 必填）；Poison/body 拒绝。
- BH charge 由每-policy cast_condition 枚举驱动；policy_id/label 仅身份；未知 condition、charge.condition 与 policy 冲突拒绝；wait_for_all_instances_expired 支持并有行为差异。
- 新 canonical：VC/FR/BH 各自的 plan-v4 execution run（作者本地运行产物）。

## 16. BH application 身份

- 声明 application_id/ailment_id/damage_type/damage_channel/resistance_key/scope 必须实际进入执行与 result applications_ledger；不得回退 Ignite/fire。
- scope=ailment、damage_type/channel/resistance 必须匹配 source_component；Poison/body 等超出矩阵拒绝。
- 新 canonical BH run（作者本地运行产物）。

## 17. BH application 归因分区

- 本体组件行（bh-binary-fire/cold）保持 body/本体语义；独立 application 输出单元使用声明 application_id/ailment_id/type/channel/resistance/scope。
- 609 行 applications_ledger 按 203 本体 Fire + 203 本体 Cold + 203 application 分区；声明身份仅作用于 application 单元。
- 新 canonical BH run（作者本地运行产物）。

## 18. BH application 输出组件引用

- application 输出组件唯一 ID：`bh-ignite`；`application_units.component_id=bh-ignite`，`application_id` 可为声明身份，关系显式。
- applications_ledger/application_units 引用的 component 必须在 result.components ∪ application_units；悬空拒绝。
- 新 canonical BH run（作者本地运行产物）。
