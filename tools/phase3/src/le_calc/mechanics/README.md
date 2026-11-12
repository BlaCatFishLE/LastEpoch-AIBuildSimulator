# `le_calc.mechanics` — 共享计算块适配器（P2 适配 + P3 生产切换）

本目录是实施方案 §6.2「先用适配器统一调用与输出，再在回归一致前提下合并」的落点。
规则/数值的权威源仍是 `tools/phase3/rules/` 与 `examples/`；本包**不复制案例输入、
不读规则文件、不读当前案例**，所有 snapshot/ruleset/scenario 由调用方显式传入。

P3 已完成生产切换：`target_side`（含 Ignite 分支）、`tag_match`、`increased_aggregate`、
`more_groups`、`crit_expected`（仅 FR）、`frequency`（数学等价部分）。每一步都以
“两案例 `reproduce` 项数不变（BH 82 / FR 40）+ 真实 run 产物逐叶数值不变 + `test --suite phase3`
全绿”为闸门；未通过即回退该步。

## 模块与入口 / 接入状态

| 模块 | 职责 | 主要入口 | P3 生产状态 |
|---|---|---|---|
| `target_side.py` | `actual = model × (1-eff_res) × (1-monster_dr)`，穿透模式/负抗策略/DR 来源/抗性来源参数化 | `compute_target_multiplier`、`resolve_monster_dr`、`bh_target_side`、`resolve_hit_target`、`hit_component_target` | **已接入**（body + FR 组件 + BH Ignite） |
| `tag_match.py` | 标签适用性统一签名 + 未知/忽略标签策略 | `tags_match`、`TagPolicy`、`bh_tag_policy`、`hit_tag_policy`、`classify_difference` | **已接入**（BH/FR 两套 matcher） |
| `increased_aggregate.py` | 加算池 `Σvalues (+属性加数)`、`factor=1+total` | `aggregate`、`aggregate_rows` | **已接入**（BH 本体/Ignite、FR inc、FR 攻速 inc） |
| `more_groups.py` | more 分组乘积；`group` 必须显式来自机制/规则字段 | `aggregate`、`product`、`rows_from_*`、`MoreGroup.row_indices` | **已接入**（BH/FR 伤害 more、FR 攻速 more） |
| `crit_expected.py` | `avg = (1-c)+c·m`，带模型 capability（BH 无暴击） | `expected_crit`、`avg_crit`、`crit_pools` | **已接入 FR**；BH capability=false，不进入伤害链 |
| `frequency.py` | 冷却/攻速→施放速率与窗口骨架（事件流语义不合并） | `effective_cooldown`、`cast_time`、`final_attack_rate`、`cast_policy`、`window_skeleton`、`cooldown_limited_rate`、`dps` | **已接入数学等价部分** |
| `ledger.py` | contribution→行投影薄封装，不改变行内容/顺序 | `project_source_ledger`、`project_hit_source_ledger` | 仅适配器（报告层合并留 P3R） |
| `registry.py` | 适配器声明、集成状态、模型能力（唯一说明入口） | `describe`、`get`、`integration_report`、`MODEL_CAPABILITIES` | — |

## 目标侧策略矩阵（两个模型的口径差异，不强行统一）

| 参数 | Black Hole（v4） | Flame Reave hit（v2） |
|---|---|---|
| `penetration_mode` | `fold_into_resistance`（`effective = res - pen`） | `separate_multiplier`（`(1+pen) × (1-res)`） |
| `negative_resistance_clamp` | 由 ruleset `negative_resistance_allowed` 决定（`allow` / `clamp_at_zero`） | `allow`（无钳制） |
| `dr_source` | `scenario_override`（`report.build_result` 注入 `describe(level)`；recompute 可覆盖） | `table_by_level`（按 `monster_level` 查同一张 1..100 表；忽略 scenario 显式 DR） |
| `resistance_source` | body=`scenario_target`；Ignite=`ruleset_base`（历史口径，P3 起显式） | `scenario_target`（ruleset.target + scenario.target 合并后的 `fire_resistance`） |
| 穿透适用分流 | `target_side.penetration_applies_to`（body/ignite） | 单组件，全部适用 |

数值关系（`tests/test_mechanics_target_side.py` 固化）：

* `res=0` 或 `pen=0` 时两种穿透口径**浮点逐位相等**；
* `res≠0 且 pen>0` 时差交叉项 `res·pen·(1-dr)`（示例 `res=0.4, pen=0.35, dr=0.87`：
  fold `0.1235` vs separate `0.1053`），属**预期差异，不合并**；
* `clamp_at_zero` 只在有效抗性为负且 ruleset 不允许负抗时生效；
* DR 表 1..100 与 `monster_dr.describe`/`dr_for_level` 逐位一致；
* `resistance_source=ruleset_base` 只用于 BH Ignite；生产场景两来源恰好相等，
  但 scenario 抗性覆盖时二者分离（测试显式断言差异）。

## 生产接入点（P3 切换清单）

| 文件/调用点 | 切换内容 |
|---|---|
| `damage.py::_target_multiplier` | → `target_side.bh_target_side`（fold + scenario DR + body scope；P2） |
| `damage.py::tags_match` | → `tag_match.tags_match(..., bh_tag_policy())`；组件闭包 = types∪categories（仅真实组件 types/categories 不相交） |
| `damage.py::_collect_increased` | → `increased_aggregate.aggregate(..., attribute_increase=...)` |
| `damage.py::group_more_rows` | → `more_groups.aggregate`（用 `row_indices` 重建既有行投影） |
| `hit_damage.py::resolve_target` | → `target_side.resolve_hit_target`（P2） |
| `hit_damage.py::compute_single_hit` 组件目标乘区 | → `target_side.hit_component_target`（P2） |
| `hit_damage.py::tags_apply` | → `tag_match.tags_match(..., hit_tag_policy())` |
| `hit_damage.py::compute_single_hit` inc 池 | → `increased_aggregate.aggregate` |
| `hit_damage.py::_more_groups` | → `more_groups.aggregate`（保留既有 window payload 形状/顺序） |
| `hit_damage.py::_crit_section` 公式 | → `crit_expected.expected_crit(FR capability)`；池提取仍在本函数 |
| `ignite.py::_ignite_scaling` inc 池 | → `increased_aggregate.aggregate` |
| `ignite.py::_ignite_target` | → `target_side.bh_target_side(component_scope="ignite", resistance_source="ruleset_base")` |
| `cooldown.py::compute_cooldown` | → `frequency.effective_cooldown` |
| `cooldown.py::compute_cast_time` | → `frequency.cast_time`（正 base_rate 分支；无 base 的 fallback 保留本地） |
| `hit_simulate.py::_attack_speed_pools` | → `increased_aggregate` + `more_groups` |
| `hit_simulate.py::simulate` final_aps/interval/冷却 | → `frequency.final_attack_rate` / `attack_interval` / `cooldown_limited_rate` |
| `hit_simulate.py::_policy_block` | → `frequency.cast_policy` + `damage_weights_factor` + `window_skeleton` |

### P3 逐步骤数值证据（本轮实测 gate）

基线：作者本地 P3 起始 run（`<run-dir>`）（P3 开始前新 run，
两案例 reproduce PASS 82/82、40/40）。每一步后新建 run 与基线逐叶比较（int/float 精确相等，
bool 精确相等；字符串只允许 generated_at/run_id/run 目录 locator/步骤产物 hash）。

| 步骤 | 切换 | BH run | FR run | int-float 叶（BH result / BH conditional / FR result） | 差异 | phase3 |
|---|---|---|---|---|---|---|
| S1 | tag_match | `<run-dir>/p3-s1-bh/` | `<run-dir-fr>/p3-s1-fr/` | 15,568 / 85 / 1,095 | 0 / 0 / 0 | PASS 557 |
| S2 | increased_aggregate | `<run-dir>/p3-s2-bh/` | `<run-dir-fr>/p3-s2-fr/` | 15,568 / 85 / 1,095 | 0 / 0 / 0 | PASS 560 |
| S3 | more_groups | `<run-dir>/p3-s3-bh/` | `<run-dir-fr>/p3-s3-fr/` | 15,568 / 85 / 1,095 | 0 / 0 / 0 | PASS 631* |
| S4 | crit_expected（FR） | `<run-dir>/p3-s4-bh/` | `<run-dir-fr>/p3-s4-fr/` | 15,568 / 85 / 1,095 | 0 / 0 / 0 | PASS 632* |
| S5 | frequency | `<run-dir>/p3-s5-bh/` | `<run-dir-fr>/p3-s5-fr/` | 15,568 / 85 / 1,095 | 0 / 0 / 0 | PASS 637* |
| S6 | target_side Ignite | `<run-dir>/p3-s6-bh/` | `<run-dir-fr>/p3-s6-fr/` | 15,568 / 85 / 1,095 | 0 / 0 / 0 | PASS 638* |

\* S3 起 phase3 套件总数包含 kh-bh 并行新增的 workflow 测试（归属非本任务），本地 mechanics
测试单独统计：S1 66 → S2 69 → S3 73 → S4 74 → S5 79 → S6 80；最终 79（BH 窗口 `dps` 辅助未接入，
删除其辅助测试，保持“已接入才断言生产路径”的测试纪律）。

最终复核（全部切换完成 + `simulate.py` 未改动 + README 定稿后）：`<run-dir>/p3-final-bh/`、
`<run-dir-fr>/p3-final-fr/`，对基线仍为 15,568 / 85 / 1,095 叶、**0 差异**，
reproduce PASS **82/82**、**40/40**。

两案例 reproduce 每一步均 PASS 且项数不变：BH **82/82**、FR **40/40**，全部 check
相对误差显示 0.000e+00（容差 1e-9）。

## 等价性测试（P2 适配器 + P3 切换双参考）

```powershell
# 从仓库根执行
$env:PYTHONPATH='tools\phase3\src'
& .\.venv\Scripts\python.exe -m unittest discover -s tools\phase3\tests -p 'test_mechanics_*.py'
```

| 测试文件 | 覆盖 |
|---|---|
| `test_mechanics_fixtures.py` | 两案例真实输入 fixture（BH 场景按 `report.build_result` 目标块构造） |
| `test_mechanics_target_side.py` | 冻结 legacy 公式 vs 适配器与生产调用点（真实值 + res/pen/dr 网格）、fold/separate 严格相等与交叉项、DR 表 1..100、Ignite `resistance_source`、golden 生产值 |
| `test_mechanics_tag_match.py` | 真实 tag 集 + AT 合成集 vs 冻结 matcher 与生产 matcher；metadata/unknown 差异类；组件定义差异记录 |
| `test_mechanics_operators.py` | increased/more/crit 生产调用点 vs 冻结实现逐位一致；group 显式来源语义；row_indices |
| `test_mechanics_frequency_ledger.py` | BH 冷却/施法时间、FR 攻速/窗口骨架生产值 vs 冻结公式、账本投影逐行一致、注册表完整性 |

## 保持独立（不合并）的项目与理由

| 项 | 为什么保持独立 |
|---|---|
| `ledger` 的**生产调用点** | 行 schema 属报告层；`hit_report`/`ledger` 两套投影字段不同，合并留 P3R（报告层），且本阶段不改变行内容 |
| BH charge 事件流 / 充能重放（`simulate.run_loop` 调度） | charge/CDR/动作流语义与 FR 不同；任务明确“语义不合并”，只切窗口 dps 除法 |
| BH 逐事件窗口聚合（body tick / Ignite 切片 → window_metrics，含 dps 除法） | 事件驱动语义，没有等价的纯公式骨架；保持既有实现（`simulate.py` 不在 P3 切换范围） |
| FR `_crit_section` 内部 chance/multiplier 的 more 组乘法 | 既有实现按“组首次出现顺序”相乘，共享适配器按键名排序；当前案例该类行数为 0（两序均为 1.0），但未来数据可能出现最后一位浮点差，故保留本地顺序 |
| FR `effective_cooldown` 的守卫语义 | FR 以 `cooldown<=0` 为无约束守卫，BH 以 ICR 分母 `<=0` 为守卫；`frequency.cooldown_limited_rate` 显式保留 FR 语义，不并入 BH 分支 |
| `crit` 默认开启 / BH 暴击 | 会改 BH 数值（BH 无 crit）；capability=false 只登记，不进入伤害链 |
| `res≠0 且 pen>0` 的穿透口径统一 | 会引入 `res·pen` 交叉项，改 FR 数值；参数化保留，不统一 |
| `resistance_source` 强制统一 | body 与 BH Ignite 的历史抗性来源不同（scenario vs ruleset base）；P3 只做显式参数化，不改变任一侧语义 |

另：本包不新增依赖（仅标准库 + `le_calc.monster_dr`）；不修改 rules/examples/expected/docs/knowledge；
不改历史 run/产物。
