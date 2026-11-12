# 统一计算计划 v2 与结果 v4 契约（`le-calculation-plan-v2` / `le-dps-result-v4`）

状态：现行（第四步 S4 FR 切片实现 + 复验 P1 收窄，2026-11-11）。适用：需要多命中组件、
具名频率策略或具名场景评估的技能迁移（首个使用方：Flame Reave / `fr11mv`）。
位置：
- schema：`tools/phase3/schemas/calculation-plan-v2.json`
- 版本身份常量/校验：`tools/phase3/src/le_calc/calculation_plan.py`
- 装配器：`tools/phase3/src/le_calc/plan_compiler.py`（`compile_from_resolved_model`，按定义声明分派）
- 执行器：`tools/phase3/src/le_calc/plan_executor.py`（公开入口）+ `plan_executor_v2.py`（v2 语义）
- 定义/绑定：`knowledge/definitions/fr-flame-reave-v3.json`、`definitions_v3.py`
- 正式 run：作者本地案例输出目录（`reports/<case>/<run-id>/`，不随公开仓库分发；复验 P1 补修后交付）

## 1. 为什么版本化

plan-v1 的三个硬约束对 FR/BH 是破坏性变化，不能放宽旧版本：组件恰好 1 hit + 1 dot、
`schedule.driver=fixed_repeat`、结果单场景/单 `single_hit` 语义。v2 因此新增独立 schema、
独立结果 schema 与独立执行路径，旧 v1 计划/结果继续按原校验执行，不做字段兼容性放宽。

| 维度 | plan-v1 | plan-v2 |
|---|---|---|
| 组件基数 | 恰好 1 hit + 1 dot | 当前实现 1..N hit（hit 必带 `hits_per_cast` 与 `damage_weights`）；dot 与 ailments 在结构校验入口拒绝（BH/S5 实现后再扩版本） |
| 组件类型 | 由 damage type 循环推导 | hit 组件可声明 `damage_type`；显式类型 added 只进对应类型组件 |
| 频率 | `fixed_repeat` + `max(action_time, cooldown)` | `le-schedule-v2`：具名策略列表、主策略、期望施放数窗口、周期平均因子；`driver=steady_state`、`first_cast_s=0`、无 `every_nth_hit`（未实现即拒绝） |
| 每秒施放/命中 | 只含 `interval_s` | `casts_per_second` 与 `hits_per_second` 分开（命中数 ≠ 权重当量） |
| 场景 | 单场景 | `evaluations[]` 具名评估，每个评估独立 scenario 文件/路径/哈希；顶层执行身份按 evaluation_id 聚合全部评估场景哈希 |
| 结果 | `le-dps-result-v3` | `le-dps-result-v4`（保留 v3 兼容投影字段 + `evaluations`/聚合场景身份/应用组件引用） |

`execute_plan(plan, scenario, runtime=None)` 仍是唯一公开执行入口；v2 计划在
`plan_executor.py` 内按 schema 分派到 `plan_executor_v2.execute_plan_v2`，不维护第二套
技能公式（组件/暴击/增伤/目标算子复用 `dps_components`/`mechanics`）。

## 2. 计划字段（相对 v1 的增量）

v1 全部顶层字段保持（`subject`/`compiler`/`executor_requirements`/`input_refs`/`sources`/
`facts`/`parameters`/`controls`/`conditions`/`components`/`effects`/`evaluation`/
`diagnostics`/`assumptions`/`scope`/`presentation`），另外：

- `schema = "le-calculation-plan-v2"`；
- `parameters.skill.base_attack_rate`：必填正数，攻击频率基数（dataset skill `extensions.as × speedMultiplier`）；
  action 间隔 = `1 / (base_attack_rate × (1+Σincreased AS) × Π(1+more AS groups))`，
  `parameters.skill.action_s` 保留 dataset castTime 供目录一致性校验与报告展示；
- `components[]`：当前执行器只实现 hit 组件；每个 hit 组件额外要求 `hits_per_cast`（≥1 整数）与
  `damage_weights`（等长、非负有限数、和 >0）。dot 组件与 `parameters.ailments` 非空在结构校验
  入口明确拒绝（BH/S5 实现 DoT 频率/窗口积分后再升级契约），不得声明后被忽略或按零值计入。
- `schedule`（`le-schedule-v2`，当前实现子集）：
  ```json
  {
    "schema": "le-schedule-v2",
    "driver": "steady_state",
    "first_cast_s": 0,
    "windows_s": [5, 30, 120],
    "window_semantics": "expected_casts",
    "policies": [
      {"policy_id": "cooldown_limited", "binding_constraint": "cooldown", "interval_policy": "max(action_time, cooldown)"},
      {"policy_id": "attack_speed_limited", "binding_constraint": "attack_speed", "interval_policy": "action_time"}
    ],
    "primary_policy": "cooldown_limited",
    "rotation": {"kind": "fixed_more_factor", "factor": 1.325, "applies_to": ["direct_hit_dps"], "status": "assumed", "label": "...", "evidence": []},
    "every_nth_hit": null
  }
  ```
  - `driver` 只支持 `steady_state`；`first_cast_s` 只支持 0；`every_nth_hit` 只支持 null；
  - 策略 ID 与 `binding_constraint`/`interval_policy` 固定对应（cooldown_limited ↔ cooldown +
    max(...)；attack_speed_limited ↔ attack_speed + action_time）；
  - 冷却口径：`effective_cooldown = cooldown_s × (1 - cooldown_reduction) / (1 + ΣICR)`；
  - 主策略 interval 取 `max(action_interval, effective_cooldown)`；attack_speed_limited 只取 action_interval；
  - `window_semantics=expected_casts` 时窗口施放数 = `T / interval`（可为小数），`complete_casts` 仅作透明列；
  - `rotation` 的周期平均因子只作用于 `direct_hit_dps`，不改变首击/每击组件伤害；
- `evaluations[]`：`{evaluation_id, label, metrics, primary, scenario_path, scenario_sha256}`；
  - `scenario_path` 必须出现在 `input_refs`（`role="scenario"`，文件哈希另行绑定）；
  - `scenario_sha256` = canonical JSON `{"states": ..., "target": ...}` 的 SHA-256；
  - 恰好一个 `primary=true`；primary 必须声明 `windows` 指标（顶层 DPS 投影来源）；
    仅 single-hit 指标的 primary 在结构校验入口拒绝；
  - 评估独立：每个评估从自己的场景文件取 `states`/`target`，并在结果中写回自己的场景哈希。

## 3. 执行语义与执行身份

对每个具名评估分别执行（贡献条件按该评估场景求值）：

1. `single_hit`：首击伤害 = `Σ(组件伤害 × damage_weights[0])`；
2. `per_cast_hit`：每次施放命中伤害 = `Σ(组件伤害 × Σdamage_weights)`；
3. `hits_per_cast`（事件计数）与 `effective_hits_per_cast`（伤害权重当量）分开；
4. dps 评估按每个具名策略给出 `interval_s`/`casts_per_second`/`hits_per_second`/
   `damage_per_cast`（含周期平均因子）/`damage_per_second`/`windows`；
5. 顶层兼容字段取 `primary` 评估：`single_hit`/`sustained_dps`/`hit_dps`/`dot_dps`/
   `interval_s`/`windows` 等，供现有报告/校验复用；v4 另有 `policies`、`evaluations`、
   `hits_per_cast`、`casts_per_second`、`hits_per_second`、`damage_per_cast`、
   `damage_per_second`、`primary_evaluation`、`application_component_refs` 门禁字段。

**执行身份（复验 P1 修复）**：v2 顶层 `scenario_sha256` 不再是主场景哈希，而是按稳定
`evaluation_id` 排序后聚合全部评估场景哈希：

```text
scenario_sha256 = sha256(canonical_json({
  "evaluations": [{evaluation_id, scenario_sha256}, ...]  # 按 evaluation_id 排序
}))
```

- 任一评估（含非主评估）场景变化都会改变顶层 `scenario_sha256` 与 `execution_fingerprint`；
- `primary_scenario_sha256` 单独保存主评估（scenario.json 的 states/target）哈希；
- `evaluation_scenarios` 按 evaluation_id 排序列出每个评估哈希；
- `scenario`/`states`/`target` 仍是主场景投影，`evaluations[].scenario_sha256` 保持独立校验。

窗口行字段：`window_id`、`seconds`、`casts`、`complete_casts`、`hits`、`hits_per_second`、
`damage_per_cast`、`hit_damage`、`dot_damage`、`total_damage`、`dps`。

**组件引用（复验 P1 修复）**：组件装配后按声明的 `component_id` 替换内部装配 ID，并同步映射
`applications[].component_id`；结果门禁校验每个 application 的组件引用存在于
`components`/`ailments` 中，不允许出现内部 ID（如 `hit:Fire`）或跨组件串用。

## 4. 拒绝边界

- plan-v2 缺 `base_attack_rate`、hit 组件缺 `hits_per_cast`/`damage_weights`、权重长度或数值非法；
- 策略 ID/binding_constraint/interval_policy 不在枚举或与 policy_id 不匹配；`primary_policy` 不在策略列表；
- `driver` 不是 steady_state、`first_cast_s` 非零、`every_nth_hit` 非 null（当前未实现语义，入口拒绝）；
- `rotation.kind`/`applies_to`/`status` 非法；`window_semantics` 非法；
- dot 组件、非空 `parameters.ailments`（plan-v2 当前未实现 DoT 频率/窗口积分，入口拒绝；
  BH 纯 DoT 已改用 plan-v3 实现，见 `calculation-plan-v3.md`）；
- 组件 ID 重复；
- `evaluations` 为空、多 primary、primary 未声明 `windows`、缺场景引用/哈希格式非法、
  `scenario_path` 不在 `input_refs`；
- 未支持策略不近似执行；plan-v1 计划出现 `evaluations` 顶层字段直接拒绝；
- 评估场景文件内容/哈希与编译期声明不一致（simulate 阶段拒绝，不回退默认场景）。

## 5. 装配器与规则

`compile_from_resolved_model` 只按 `definition.plan_version` 分派；plan-v2 定义可用规则
（全部在 `effect_binding.KNOWN_RULE_HANDLERS` 白名单，结构在加载期校验）：

| 规则 | 语义 | 实现 |
|---|---|---|
| `rules.hit_events` | base_hits + 返回源节点（需分配点数）+ 50% less 权重 | `_resolve_hit_events_v2` |
| `rules.rotation` | 策略列表/主策略/窗口/周期平均（节点消费加成绑定） | `_resolve_rotation_v2` |
| `rules.item_effectiveness` | 同一装备定义来源数值 → 目标槽位全部来源乘数；显示稀有度前提由当前词缀层级判定 | `_display_item_rarity` + 装配器 |
| `rules.added_damage_conversion` | 指定标签闭包的 added 行池 → 除以 divisor × 节点值，输出 added 行 | 装配器 |
| `rules.row_normalizations` | 显式、可追溯的旧基线口径归一（如 OQ6 未验证公式、mana 固定条件）；稳定锚点未命中即拒绝 | 装配器 |

规则数据进入 `sources`（`rule_source:*`）、`facts` 与 `assumptions`；`row_normalizations`
命中不到当前来源时抛 `PlanCompileError`，不允许用规则掩盖缺失来源。

## 6. 结果/报告/重算绑定

- `le-dps-result-v4` 保留 v3 的组件/账本/应用/覆盖/来源字段与 KPI 投影，报告渲染函数
  `dps_report.render` 对 v4 追加“多命中与频率”区块（命中数、权重、每秒施放/命中、策略表、
  评估场景哈希）；它只影响渲染阶段，不进入数值阶段指纹；
- `validate_stage` 按计划 schema 选择 v3/v4 校验：v4 额外校验 `evaluations` 一致性、
  聚合 `scenario_sha256`/`primary_scenario_sha256`/`evaluation_scenarios` 复算、首击/每cast/
  权重当量复算、窗口期望施放数、每秒施放与每秒命中分离、application 组件引用；只读复验
  （write=False）还校验 `report-binding.json.scenario_binding` 与 result 场景身份一致；
- `run-manifest.json` 增加 `primary_scenario_sha256` 与 `evaluation_scenarios`；
  `scenario_sha256` 为聚合执行身份；
- `report-binding.json` 增加 `scenario_binding`：v2 记录聚合身份、主场景身份与
  `{evaluation_id, scenario_sha256}` 列表（v1 记录单场景哈希）；报告能力元数据同步保存；
- `serve` 重算对 v2 报告使用已保存计划；单场景请求会应用到全部具名评估（临时结果不重写
  已发布 run，评估哈希按请求场景重新计算；页面重算缓存键包含结果/绑定摘要）。

## 7. 复验 P1 补修（2026-11-11）

独立复验提出的 3 类 P1 已修复，正式 run 已按补修后代码重新交付（作者本地 `reports/<case>/<run-id>/`，不随公开仓库分发）：

1. **多评估执行指纹**：顶层 `scenario_sha256`/`execution_fingerprint` 改为按 evaluation_id 聚合
   全部评估场景哈希；`primary_scenario_sha256` 另存；workflow validate/run-manifest/report-binding
   同步；补“只改非主评估场景 → 聚合身份变化、主场景身份不变”负例（single_hit ignited 后
   该评估 92827.76671860272，DPS 评估不变，聚合哈希/执行指纹变化）。
2. **应用记录组件引用**：`build_hit_components_v2`/dot 装配在替换声明 component_id 时同步映射
   applications；validate 增加 `result.application_component_refs`；补双命中组件（Fire/Cold）
   用例确保不串组件、不出现内部 ID。
3. **契约收窄**：`every_nth_hit`/非零 `first_cast_s`/`hit_windows` driver/仅 single-hit 指标的
   primary/DoT 组件/非空 ailments/策略形状不匹配全部改为结构校验入口拒绝（`unsupported_plan_v2_feature`），
   schema 同步收窄；已声明的命中权重、双策略、窗口语义、周期因子、多评估仍驱动执行。

上一轮 run（作者本地历史产物，不随公开仓库分发）保留为历史对照，不删除/不改写；它按补修前
执行身份契约生成，用补修后的只读 validate 不再视为当前交付（不重写、不预填 PASS）。
补修后的 run 为当前交付 run。

## 8. 旧链路边界

- `docs/contracts/calculation-plan-v1.md` 的 v1 语义不变；VC 已签收 run 的计划/结果保持只读；
- 执行器实现文件变化会使 v1 旧计划的 `executor_fingerprint` 不再匹配当前实现，重放时按 v1
  契约明确失败并要求重新编译（历史报告仍可离线阅读）；
- FR/BH 迁移后的生产路径不得调用 `hit_inputs`/`hit_damage`/`hit_simulate`（定向
  monkeypatch 测试证明）；旧模块在确认无引用后逐步降为只读迁移证据。

