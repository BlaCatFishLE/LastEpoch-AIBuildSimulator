# 计算计划 v3（`le-calculation-plan-v3`）契约

状态：现行（S5 BH 切片实现，2026-11-11）。适用：纯 DoT 技能、独立施加时钟与
充能/多实例并发调度（首个使用方：Black Hole / `bh2`）。实现：`plan_compiler_v3.py`
（编译）、`plan_executor_v3.py`（执行/只读复验）、`le-dps-result-v5`。

## 1. 与 v2 的边界

| 维度 | plan-v2（FR） | plan-v3（BH） |
|---|---|---|
| 组件 | 只实现 hit；dot/ailments 拒绝 | 只实现 dot（`kind:"dot"`，`scope:"body"`）；出现 hit 入口拒绝 |
| 频率 | `le-schedule-v2`：cooldown/attack-speed 具名策略 + 期望施放窗口 | `le-schedule-v3`：充能（charges/recharge/CDR/ICR/flat）+ eager/patient |
| 追加伤害 | 只算命中 | 纯 DoT 本体：逐跳基础伤害 + added×scaling |
| 自然异常 | 不支持 | `schedule.ignite` 独立施加时钟：期望层数 × 逐批到期 |
| 结果 | `le-dps-result-v4` | `le-dps-result-v5`（`loops`/`single_cast`/`components`/`ignite`/`cooldown`） |
| 场景 | 每个评估一个场景文件 | 每个评估一个场景文件；评估差异还包含 `action_policy` |

未实现的语义（hit 组件、dot 不声明 tick/duration、非 `ceil_full_ticks` 余量策略、
非 `charge_dot_cycle` driver、非 spawn 首判、缺 ignite 模型、重复 action_policy 等）
一律在 `validate_plan_structure`/`validate_plan_v3` 入口拒绝，**不得声明后被忽略或按零值计入**。

## 2. 关键字段

### 2.1 components（纯 DoT）

```json
{
  "component_id": "bh-binary-fire",
  "kind": "dot",
  "damage_type": "Fire",
  "tags": ["Spell", "DoT"],
  "scope": "body",
  "crit_capable": false,
  "base_damage": 48.0,
  "added_damage_scaling": 2.4,
  "duration_s": 7.15,
  "tick_interval_s": 0.3,
  "damage_at_start": true,
  "remainder_policy": "ceil_full_ticks"
}
```

- `duration_s` 与 `base_damage` 由编译期从 dataset 技能常量 + 当前节点/装备绑定固化，
  执行器不再读取 case/catalog；
- 逐跳伤害 = `(base + added_total × added_damage_scaling) × (1 + inc_total) × more_product × target_multiplier`；
- 逐跳数 = `ceil(duration_s / tick_interval_s)`；`ceil_full_ticks` 表示末跳满伤害、不合并余量
  （1.5.3 `OnUpdateTick`/`OnDestroy` 语义）；
- 每个组件独立按 `spawn + k × tick_interval_s` 造成伤害，`spawn = cast_start + cast_time`；
- `damage_at_start` **只支持 true**（spawn 首跳）。false 在结构校验（`calculation_plan`）、
  执行期校验（`validate_plan_v3`）与定义绑定（`rules.dot_components`）入口明确拒绝；
  若要支持 false，必须同时实现首跳偏移/跳数/窗口边界并补手算 oracle，不得接受后忽略。

### 2.2 schedule（`le-schedule-v3`）

```json
{
  "schema": "le-schedule-v3",
  "driver": "charge_dot_cycle",
  "horizon_s": 120.0,
  "windows_s": [5, 30, 120],
  "cast": {"base_rate_per_s": 1.46667, "animation_speed": 1.8333375, "speed_multiplier": 0.8},
  "charges": {
    "base_max_charges": 1, "extra_max_charges": 1,
    "base_recharge_s": 13.99972, "flat_adjustment_s": 2.0,
    "cooldown_reduction": 0.6
  },
  "action_policies": [{"policy_id": "eager"}, {"policy_id": "patient"}],
  "primary_policy": "eager",
  "ignite": {
    "base_damage": 40.0, "base_duration_s": 2.5,
    "scaling_tags": ["Fire", "DoT", "Ailment"],
    "duration_stat_key": "ailment_duration",
    "rolls_per_second": 2.0, "first_roll_at": "spawn",
    "chance_total": 0.1
  },
  "target": {
    "monster_damage_reduction_control": "monster_damage_reduction",
    "monster_damage_reduction_default": 0.87
  }
}
```

- `cast_time = 1 / (base_rate_per_s × (1 + Σ increased cast speed))`；`spawn = cast_start + cast_time`；
- `effective_recharge = (base_recharge_s + flat_adjustment_s) × (1 - CDR) / (1 + ICR)`；
  充能顺序恢复（同一时间一个恢复计时器），`max_charges` 只提供起手爆发；
- **eager**：充能 ≥1 且施放动作空闲即施放，允许新旧黑洞同时存在；
  **patient**：还要求当前没有任何存活黑洞；两者都受 `cast_ready = cast_start + cast_time` 限制；
- 窗口 `casts` 只统计 `cast_start ≤ window`；窗口伤害只计窗口前已发生部分；窗口前已施加、
  窗口后才结算的 Ignite 记入 `unsettled_tail_damage`，不重复计入 DPS。

### 2.3 Ignite 独立施加时钟

- 每次施放在 `spawn + k × (1/rolls_per_second)`、`k` 从 0 起且偏移 `< duration_s` 时判定一次；
- 每次判定期望层数 = `chance_total × uncapped_fire_resistance_percent × roll_interval_s`；
- 每个判定批次独立计时（`roll_time + ignite_duration`）并各自到期，不合并、不延长既有层数；
- Ignite 单层伤害 = `base_damage × duration_multiplier × (1 + inc_total) × more_product × target_multiplier`；
  `duration_multiplier = 1 + Σ increased Ignite duration`（总伤害随持续时间线性放大）；
- 伤害/持续时间/穿透/更多乘区按 `scope` 过滤：默认行同时作用于本体与 Ignite；
  `raw.scope="body"` 的行（如 Weakened Reality）只作用于本体；`raw.scope="ignite"` 只作用于 Ignite；
- 目标侧：`(1 - (target_resistance - penetration)) × (1 - monster_damage_reduction)`。

### 2.4 evaluations

`evaluations[]` 额外要求 `action_policy`（`eager`/`patient` 各一个，恰好一个 `primary`），
场景哈希仍按各评估场景文件独立绑定；顶层执行身份用聚合身份（见 v2 契约 §3），
`primary_scenario_sha256` 为主评估场景哈希。

## 3. result-v5

- `schema="le-dps-result-v5"`；`evaluations[]` 各含 `action_policy/primary/scenario_sha256/cast_count/max_concurrent/horizon_dps`；
- `loops[]` **primary 在首位**，按策略给出 `casts`/`charge_events`/`resource`/`windows`/`totals`
  （`horizon_dps`/`horizon_damage`/`cast_count`/`max_concurrent_black_holes`/`cycle_s`/
  `steady_state_dps_cross_check`）；
- `single_cast`：单次施放（0 起手，spawn=cast_time）的本体/ Ignite / 尾伤 / 观测终点；
- `components[]`：dot 组件的逐跳伤害、tick 数、加算/独立/穿透/目标乘区；
- `ignite`：持续时间、单层伤害、施加模型（未封顶火焰抗性、每次判定期望层数、判定期数）；
- `cooldown`/`cast`：充能参数与施放时间；
- **顶层投影按 primary**：`scenario/states/target/components/contributions/cooldown/cast/single_cast`
  与 `loops[0]`/`evaluations[0]` 均取 `primary=true` 评估，不按评估顺序取第一个；
  聚合 `scenario_sha256` / `primary_scenario_sha256` 仍按已签收 FR 规则（聚合按 evaluation_id 排序）；
- **组件级应用账本**：`applications` 为每条 contribution × 每个应用单元
  （`bh-binary-fire` / `bh-binary-cold` / `bh-ignite`，`application_units` 声明）一条记录，
  `status ∈ applied|not_applicable|condition_not_satisfied|unresolved`；
  非 applied 必须带原因（标签/scope/条件/未解析）；`contribution_application_summary` 由账本复算，
  `contributions[].application` 指向该摘要，覆盖投影只消费账本中伤害/施加相关的未解析记录；
  `effect_ledger` 由计划账本模板 + 主评估运行时行投影；
- `coverage` 由 `facts.coverage_base_gaps` 投影；`issues` 保留 limitations 与覆盖摘要。

只读复验 `plan_executor_v3.validate_result_v5` **实际复算**而非预置 True：
- 由 `components`（per_tick/tick_count/interval）与 `casts` 复算每个窗口 body；
- 由 `ignite` 的 duration/per_stack/stacks_per_roll/roll_interval 与 `casts` 复算每个 Ignite 批次、
  窗口 Ignite 伤害与 `unsettled_tail_damage`（尾伤一致性）；
- 由复算窗口核对 `loops[].totals` 的 horizon_damage/DPS/body/ignite/tail/cast_count、
  `max_concurrent` 采样、`evaluations[]` 摘要、`single_cast`（含单次尾伤）；
- 核对顶层 projection 与 primary 场景哈希一致、应用账本结构与摘要可复算、贡献顺序、覆盖投影、充能公式。
必需检查集合由 `workflow.unified.required_validation_checks("le-calculation-plan-v3")` 给出
（46 项），commit 门禁按计划 schema 选取，不得用 v1/v2 列表替代。

## 4. 编译规则（BH 定义）

`knowledge/definitions/black-hole-v3.json` 的 `rules`（全部在 `effect_binding.KNOWN_RULE_HANDLERS`
白名单，加载期校验）：

| 规则 | 语义 |
|---|---|
| `dot_components` | 纯 DoT 组件、tick 模型、持续时间来源、Binary System 双组件与 Red Giant 转换所需节点 |
| `ignite_model` | 基础伤害/持续时间/缩放标签/roll 频率/Heat Wave 机会来源 |
| `charge_schedule` | charges/recharge/CDR/ICR/flat、horizon/windows、eager/patient |
| `dot_target` | 怪物 DR 场景控制项与默认值 |
| `attribute_scaling` | Intelligence ×4% increased damage（BH 本体与 Ignite 继承） |
| `row_normalizations`-style `row_reclassifications` | 显式、可追溯的旧口径行重分类/排除（Ignite duration special_tag=1、其它 ailment duration 排除、Fire Penetration with Ignite、Minion 行排除、idol altar 条件 ICR 排除、被动抗性/穿透标签） |
| `overcapped_scaling` | 溢出抗性阈值缩放（手套 ICR per 8% overcapped Cold Resistance，cap 200%） |

不满足 `require_nodes`（Binary node 9、Red Giant node 4）时编译拒绝；规则命中不到当前行时
产生显式诊断，不允许静默忽略。

## 5. 旧链路边界

- 生产路径不 import 旧 BH 计算器（`damage`/`ignite`/`cooldown`/`simulate`）：
  `test_unified_bh_migration` 用 import 守卫在独立子进程验证；
- 旧 legacy baseline 结果（作者本地，不随公开仓库分发）只作 D12 语义/数值对照，不作为新 handler 或唯一 oracle；
- VC/FR 已签收 run 与冻结数据只读；`plan-v2` 仍拒绝 dot/ailments（BH 切片改用 plan-v3）。

## 6. 当前正式 run

- 复验 P1 补修后的交付 run（作者本地 `reports/<case>/<run-id>/`，不随公开仓库分发；历史 run 保留为对照）：
  plan `5b7747a587308c09b8cfb194f8d9b54ccb109c8fecc6c516bcd77115f52d953d`，result-v5，
  203 contributions / 609 application records / 203 条摘要，12 manifest 文件，
  validation 64 checks（46 required 全 PASS），coverage partial/2；
- 与 legacy baseline 逐浮点一致：single_cast 10662227.4697164（body 1144337.76856214 +
  ignite 9517889.70115428）；eager horizon_dps 2919759.11554595 / 35 casts / max_concurrent 3；
  patient 1337984.91923145 / 16 casts / max_concurrent 1；5/30/120s 窗口 body/ignite/DPS/tail 一致。


## 7. 复验 P1 补修（2026-11-11）

独立复验 4 项 P1 已修复，正式 run 已按补修后代码重新交付（作者本地，目录形态 `reports/<case>/<run-id>/`）：

1. **组件级应用账本**：移除空 `applications`；按 contribution × {fire, cold, Ignite} 生成 609 条记录，
   状态/原因/角色齐全，`contribution_application_summary`、`contributions[].application`、
   `effect_ledger`、报告来源状态与 coverage 投影全部读取该账本；测试断言每条 contribution 有
   applied/excluded 解释且摘要可复算。
2. **顶层投影 primary 化**：scenario/states/target/components/contributions/cooldown/cast/single_cast/
   loops[0]/evaluations[0] 均按 `primary=true` 选择；反例覆盖“patient 排第一、eager primary、
   两评估不同 resistance”时顶层必须与 eager 一致；聚合/主场景身份仍按签收 FR 规则。
3. **damage_at_start=false 入口拒绝**：结构/执行期/定义绑定三处拒绝并同步 schema/契约；
   负例断言 `PlanValidationError`。本切片只支持 spawn 首跳。
4. **结果门禁复算**：`validate_result_v5` 从 casts/components/Ignite 批次复算 loops totals、
   windows、尾伤、max_concurrent、evaluations 摘要与 single_cast；`validate_stage(write=False)`
   同样拒绝篡改（如 `loops[0].totals.horizon_dps=1.0` 命中 `result.loop_totals`）。
