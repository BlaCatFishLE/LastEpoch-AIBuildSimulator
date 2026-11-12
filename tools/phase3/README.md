# 第三阶段第一小阶段：Black Hole DPS 计算器（v1 工程契约）

本目录是第三阶段第一小阶段的独立实现，**只读消费**第一阶段 dataset 与第二阶段构筑 revision，
不修改 `data/`、`tools/ingest/`、`tools/build_import/` 中的既有产物与代码。

- 输入构筑：由当前案例 `case.json` 的 `build.build_dir` / `build.revision_id` 显式指定
  （公开版不内置任何构筑 revision；`data/builds/` 是用户本地产物目录）
- 计算输入导出：由案例 `inputs.calculation_input` 指定；格式 `calculation-input-v1`
- 只读目录库：`data/releases/phase1-dataset-003/data.sqlite`（由 `scripts/fetch_dataset.ps1` 获取，不随仓库分发）
- 规则包：`tools/phase3/rules/`（随公开版分发）
- 作者本地的规划/参考证据已归档，不随公开版分发

## 1. 运行环境

```powershell
$py = '<repo>\.venv\Scripts\python.exe'   # Python 3.12；创建步骤见仓库根 README
$env:PYTHONPATH = 'tools\phase3\src'
& $py -m unittest discover -s tools\phase3\tests -v
```

当前测试基线（2026-11-07）：**386 tests OK**（含条件 Buff 后端、条件场景重算与玩家版页面验收）。
仅使用 Python 标准库。HTML 产物为单文件 UTF-8、无外部请求、可离线双击打开。

### 1.1 怪物等级减伤（Monster DR，v4）

- 数据：`tools/phase3/data/monster_dr.json`（来源 [tunklab /mobdr](https://lastepoch.tunklab.com/mobdr)，等级 1–100；等级 100 DR=87%）。
- 规则包：`tools/phase3/rules/ruleset-bh-stage1-v4.json`（v3 + P1 清理：补 6 条 rule、修过期 locator、统一 tick 口径；含 monster DR，默认等级 100）。
- CLI：`--monster-level 1..100`；`0` 或缺省（无规则默认时）= 关闭怪物 DR；v4 规则包缺省为 100。
- 结果：`result.json` 新增 `scenario.target.monster_level/monster_damage_reduction/monster_dr_factor` 与顶层 `monster_dr`（等级表 + `dps_by_level` 0/1–100 换算）。报告默认使用生成场景；顶部“目标”区可编辑怪物等级与目标火抗，点「应用」后全页数值按目标侧统一变换联动（UI v2，见 `作者本地历史记录（不随公开仓库分发）`）；全等级对比仍在「场景与敏感性」抽屉。
- 说明与正式 run：`作者本地历史记录（不随公开仓库分发）`。

## 2. 目录与文件所有权（写入范围）

| 路径 | 所有者 | 说明 |
|---|---|---|
| `tools/phase3/README.md`、`schemas/`、`fixtures/`、`rules/` | Lead | 契约、结果 schema、示例 fixture、规则包 |
| `tools/phase3/src/le_calc/__init__.py`、`model.py` | Lead | 共享数据契约 |
| `tools/phase3/src/le_calc/catalog.py`、`inputs.py`、`stats.py`、`ledger.py` | input-ledger | 输适配器 + 静态属性快照 + 来源账 |
| `tools/phase3/src/le_calc/damage.py`、`ignite.py`、`cooldown.py`、`simulate.py`、`report.py`、`cli.py`、`ruleset.py` | Lead | 伤害核、循环、结果组装、CLI、规则包加载 |
| `tools/phase3/src/le_calc/conditional/`（`registry/discover/scenario/evaluate/view/context/conditions/recompute`） | backend-conditional（条件 Buff 阶段）+ calc-channel（重算通道）+ Lead 集成 | 条件效果注册表加载、当前构筑来源检查、场景校验、条件求值、页面展示数据；v3.2 起 `recompute` 可在生成场景下独立重算（不改写旧 DPS 基线） |
| `tools/phase3/rules/conditional-effects-bh-stage1-v1.json` | registry-research（2026-11-06）+ Lead | 条件效果机制注册表（效果定义/共享状态/检查记录/预设/证据 locator） |
| `tools/phase3/src/le_calc/html.py` | Lead | 兼容入口：`render` / `write_html` / CLI，转发到 `report_ui` |
| `tools/phase3/src/le_calc/report_ui/`（`units/labels/sources/present/components/shell/assets`） | Lead | 展示数据层、文档骨架、样式与交互 |
| `tools/phase3/src/le_calc/report_ui/pages/` | Lead + page 实现者 | 六个页面模块（总览/伤害构成/点燃/时间轴/来源与校验/条件 Buff） |
| `tools/phase3/tests/test_{inputs,stats,ledger}.py` | input-ledger | 输入/快照测试 |
| `tools/phase3/tests/test_{damage,simulate,report}.py` | Lead | 引擎/结果测试 |
| `tools/phase3/tests/test_conditional.py` | backend-conditional + Lead | 条件注册表/发现/场景/求值/展示数据测试 |
| `tools/phase3/tests/test_html.py` | Lead | 展示层 + 离线单文件 HTML 测试 |
| `reports/<case>/<run-id>/` | 各自 | 本地运行产物（不入库） |

**不得**改写 `data/` 中的已发布数据与 `reports/<case>/<run-id>/` 历史运行产物；新结果一律写新 run-id。

## 3. 数据契约

### 3.1 `le_calc.model`（冻结）

- `Evidence(kind, detail, locator)`：`kind ∈ {game_data, user_rule, catalog, third_party_reference, public_doc, derivation}`
- `Contribution`：单条属性来源记录（一个词条/节点效果/基础值/派生值一行）
  - `stat_key`：归一化属性族（`damage`, `added_damage`, `ailment_duration`, `ailment_effect`,
    `ailment_chance`, `penetration`, `cast_speed`, `cooldown_recovery_speed`, `cooldown_reduction`,
    `skill_duration`, `mana`, `mana_regen`, `all_attributes`, `attribute_intelligence` …,
    `resistance_fire`, `resistance_cold`, …, `unsupported`）
  - `operation`：`added | increased | more | base | flag`
  - `damage_tags`：LE Tools AT 位掩码解码后的标签名列表（`Fire`、`Cold`、`Elemental`、`Spell`、`DoT`…），
    空列表表示无标签限定
  - `condition_code`/`condition_status`：条件码与判定（`always | satisfied | not_satisfied | unknown`）
  - `included`/`exclusion_reason`：是否纳入本次计算以及原因
  - `value_source`：`source | local_value_override`
  - `status`：`confirmed | derived | assumed | unresolved`
- `Snapshot`：`build_meta / character / attributes / resistances / contributions / issues`
  - `resistances[name] = {total, cap, capped, overcapped}`
  - 属性快照是**已解析、已判定条件**后的计算结果；引擎只消费 `Contribution` 与标量摘要。

### 3.2 规则包 `tools/phase3/rules/ruleset-bh-stage1-v1.json`（Lead 维护）

由 Lead 根据执行方案 + 用户 2026-11-04 确认的 7 条规则 + 游戏数据编写。结构见文件内 `schema_note`。
引擎读取规则包、来源账、快照后产出结果 JSON。

### 3.3 结果 JSON `result-v1`（HTML 唯一业务数据源）

Schema：`tools/phase3/schemas/result-v1.json`；示意 fixture：`tools/phase3/fixtures/sample-result-v1.json`
（**fixture 是合成数据，不是计算结果**）。

顶层键（至少）：

```text
schema, run, input, scope, scenario, action_policy, snapshot,
source_ledger, rule_ledger, modifier_graph,
single_cast {body, ignite, totals, event_trace, time_series},
loops [ {policy_id, action_policy, windows, casts, resource, event_trace, time_series, totals} ],
issues, excluded_sources
```

HTML 只读取该 JSON，不在前端重算伤害。

### 3.4 报告展示层 `le_calc.report_ui`（UI 改版 v1）

`result.json` 仍然是唯一业务数据源；`report_ui/present.py` 把它转换成页面可读的展示
`view`（单位、来源目录、乘区步骤、时间曲线、校验信息），`report_ui/shell.py` 负责单文件
文档骨架并内联 CSS/JS。前端只做展示、插值与坐标计算，不重新实现伤害规则。

```text
report_ui/
  units.py       数值单位与格式化（fraction / pp / factor / damage / seconds / stacks …）
  labels.py      中文标签表（状态、条件、来源类别 …）
  sources.py     source_ledger -> 玩家可读来源目录（名称/槽位/词缀/数值过程/证据）
  present.py     build_view(result) -> 展示数据层
  components.py  共享 HTML 片段（来源 chip、表格、徽章、公式链 …）
  shell.py       render(result) / write_html(result, path)：单文件 HTML 骨架
  assets/        style.css + app.js（渲染时内联，运行时无外部请求）
  pages/         六个页面：overview / composition / ignite / timeline / validation / conditions
```

- **报告基线（2026-11-05 起，v2 含动态目标场景）**：后续伤害报告统一以本 `report_ui` 版本为默认模板，
  不再回退旧版 `html.py` 渲染；决定、正式命令与动态场景约束见 `作者本地历史记录（不随公开仓库分发）`。
- 契约与页面约定：`作者本地历史记录（不随公开仓库分发）`
- 改版依据：`作者本地历史记录（不随公开仓库分发）`
- 验收记录：`作者本地历史记录（不随公开仓库分发）`
  （条件 Buff 页验收另见 `作者本地历史记录（不随公开仓库分发）`）
- 渲染：`python -m le_calc.html --result <result.json> --out <report.html>`；
  默认由 `le_calc run` 自动写出 `black-hole-damage.html`。
- 动态目标场景（v2）：顶部可编辑怪物等级/目标火抗，客户端只做目标侧统一变换
  （等级用引擎等级表、火抗用抗性乘性公式），绑定协议见契约文档 §3.4。
- 条件 Buff 页（2026-11-06，v3.2）：第六个页面，展示当前构筑的条件效果候选、全局条件、每效果的开关/层数/
  施法状态与检查记录；预设与场景在最前，条件效果区合并展示（不再重复共享状态/自动判定段）。
  页面可导出/导入场景 JSON；服务端在生成报告时按“生成场景”用跨技能 Buff 输入通道重算并显示
  “旧 DPS 基线 vs 生成场景重算”（`#cond-recompute`），**不改写旧 DPS 页数值**。v3.3 起可运行
  `le_calc serve`（仅 127.0.0.1）后点击页面“应用条件并重算 DPS”，整页替换为按场景重算的报告；
  导出/导入场景后预设下拉显示“已保存”。展示数据来自
  `result["conditional"]["view"]`，契约见 UI 契约文档 §3.5；实现与交接说明见
  `作者本地历史记录（不随公开仓库分发）`。

### 3.5 条件 Buff 层 `le_calc.conditional`（2026-11-06）

在单技能 DPS 结果之外，增加一层**条件效果发现、筛选与场景配置**，不改变伤害核：

```text
le_calc/conditional/
  registry.py   条件效果注册表加载/严格校验（effects/shared_states/computation_modules/presets/证据）
  discover.py   从当前构筑（计算输入 + 快照 + catalog）枚举来源、候选、检查记录与数据缺口
  scenario.py   默认场景 / 预设 / 保存 / 加载 / 校验（版本绑定、层数范围、未知效果）
  evaluate.py   资格与条件求值（selected/condition unmet/needs rules/…）、拟议 contribution 接口
  view.py       条件页展示数据 + 客户端求值规则（离线即时状态，不伪造 DPS）
  context.py    build_context(...)：以上流程的门面，挂到 result["conditional"]
  conditions.py 推导静态状态（自动判定项）
  recompute.py  跨技能 Buff 输入通道：场景提案 → model.Contribution → 独立重算（不改写旧 DPS 基线）
```

- 入口：`le_calc run`（自动生成，默认附带条件场景重算；`--no-recompute` 可跳过）、
  `le_calc conditions`（只输出条件工件；加 `--recompute` 时也写 `conditional-recompute.json`）与
  `le_calc serve`（本机回环一键重算服务，报告页按钮使用）。
- 产物：`conditional-effects.json`、`conditional-scenario.json`、`conditional-evaluation.json`、
  `conditional-recompute.json`（见 §6）。
- 口径：固定条件估算——选中的持续状态在观察期保持，不模拟获得/维持成本。v3.2 起，固定条件机制
  （Craterborn/Infusion/Chilled Touch/Eternal Flames/Ambition/Preparation 单次/Crescendoing 单次/布局 CDR）
  通过 `recompute.py` 注入引擎重算；旧 DPS 基线（result.json 主数值）保持不变，重算只写入
  `recompute` 块与独立工件。Apex/Frozen Steel/Conflagrate 等机制仍未建模。
- 数据参考：`data/reference/tunklab/ailments.sqlite`（147 ailments）+ `ability-glyph-of-dominion.json`
  （Glyph 能力参考），独立于 phase1 dataset，不进入 DPS 输入。
- Glyph 模式（v3.3）：Apex + 站立 Glyph 时 Heat Wave 点燃几率转 Fire Resistance Shred，ignite=0，
  目标火抗按 shred 降低（Boss 60% less→-20%，非 Boss -50%）并与穿透相加；Glyph 不在该构筑，
  站立状态为人工声明，覆盖率按 10 层持续假设。
- 设计/研究/验证文档：`作者本地历史记录（不随公开仓库分发）`、
  `作者本地历史记录（不随公开仓库分发）`、`作者本地历史记录（不随公开仓库分发）`。

## 4. v1 已确认规则（用户 2026-11-04）

| # | 规则 | 状态 |
|---|---|---|
| 1 | 结算周期不足一个 interval 的剩余伤害合并到最后一跳（总伤害 = 名义速率 × 实际持续时间） | user_rule / confirmed |
| 2 | 先执行 Red Giant 冷转火；Binary System 视作一个黑洞：总伤害翻倍，另一半为冰霜伤害；一个双星算 1 个黑洞（"只能创建 1 个"限制的是**单次施放**，多次施放的黑洞可并存且独立结算——2026-11-04 第二轮确认） | user_rule / confirmed |
| 3 | 同一节点多点相加；不同节点带 "more damage" 描述者为独立乘区 | user_rule / confirmed |
| 4 | Heat Wave：未封顶火抗 → **每秒一次**的点燃几率判定 | user_rule / confirmed |
| 5 | Black Hole 施加的 Ignite 继承 Black Hole 技能树上所有增伤节点，除非节点明确限定 hit 等与点燃无关 | user_rule / confirmed |
| 6 | duration 加算理解正确；点燃时长延长是总伤害乘区；冷却先做加减（固定秒数）、再应用冷却恢复速度；v1 忽略法力资源（无限法力） | user_rule / confirmed |
| 7 | 目标场景取最优：Boss 标签、0 抗性、双星全覆盖、中心 1 米条件满足 | user_rule / confirmed |

以上规则在规则账中标记 `user_rule`，不属于"游戏数据独立验证"，HTML 顶层需保持"限定来源的 v1 模型"表述。

### 4.1 2026-11-04 第二轮用户裁决（已并入 v1）

| # | 裁决 | 落地 |
|---|---|---|
| R3-1 | Elemental 标签等价于 Fire/Cold/Lightning → 进入 Ignite | `rule-ignite-elemental-tag` → confirmed（v1 原本已适用） |
| R3-2 | Crescendoing Rage 本阶段暂不考虑，放到后续阶段 | `rule-crescendoing-rage` → unresolved + deferred，v1 不计入 |
| R3-3 | Pyromancer +40% Ignite Chance 先按不计入（应与 hit 相关） | `rule-pyromancer-ignite-chance` → assumed，v1 不计入 |
| R3-4 | Heat Wave 判定时序细节正确 | `rule-ignite-heat-wave-timing` → confirmed |
| R3-5 | 本体跳数暂按 rate×duration，文档标记、后续逆向 | `rule-bh-tick-schedule` 保持 assumed + 待逆向注记 |
| R3-6 | **黑洞数量上限针对单次施放；多次施放可并存、各自独立结算** | 引擎循环改为允许多黑洞重叠；eager 与 patient 策略出现实质差异；`rule-black-hole-count-limit` / `rule-black-hole-recast` → confirmed |
| R3-7 | Binary System 的 +100% 语义不确定，记录待逆向 | `rule-bh-binary-more-interpretation` → unresolved；v1 仍按结构翻倍 |
| R3-8 | Intelligence 的 inc 先作用于 Black Hole，再传递给其 Ignite | `rule-ignite-attribute-scaling` → confirmed（v1 已计入） |

### 4.2 2026-11-04 数据修正：装备词条 "Fire Penetration with Ignite"（先修数据库与本地 BD 导入）

用户指出第 9 点其实是**装备导入错误**：Fiery Dragon Shoes 的真实词条是 **"Fire Penetration with Ignite"**（点燃专属火焰穿透），不是 "Increased Ignite Effect"。已先修数据管线：

| 层 | 修复 |
|---|---|
| phase-1 数据库 | 新增 `dataset 003`（`phase1-dataset-003`）：stats 增加 `special_tag_display` 显示映射（property 43 + specialTag 1 = "Fire Penetration with Ignite"，由单属性词缀 182/467/468 的显示名推导；另含 2/23/8/79 等 ailment 变体）；`data/current.json` 已切到 003；该 dataset 通过 validate（70 pass / 2 warn / 0 fail）。 |
| phase-2 本地 BD 导入 | catalog 使用数据集 003 的 specialTag 显示映射并输出到 BuildSpec/calc-export；重新导入本地 BD（`rev-c746411ccebee9c0`，seq 8，dataset 003，17 条本地输入全部应用）；calc-export 中该 mod 的 `stat.name`/`readable_text` 均为 "Fire Penetration with Ignite"，大小 524,731 B。 |
| phase-3 计算器 | 新增 `STAT_PENETRATION_WITH_IGNITE`（只作用于 Ignite 的火焰穿透），规则改为 `rule-fire-penetration-with-ignite`（confirmed）；35% 与 41% 火焰穿透合并 = 76%，目标 0 抗 → -76%（×1.76），**数值口径不变**（单次 31,219,074.24 / eager 120s 4,717,076.66 / patient 120s 3,967,354.80）。 |

修正后单次与循环数值与修正前**完全一致**（只是数据语义与展示修正）；阶段测试：phase-1 188 OK、phase-2 355 OK、phase-3 168 OK。

## 5. v1 仍为假设/未解决的项目（必须在结果中显式标注）

- 每跳事件的具体时间表（damageAtStart 的首跳、末跳合并的具体事件形态）；参考模型主张整数跳并丢弃余量，与用户规则 1 冲突，v1 以用户规则为准（本体伤害差约 +0.7%）；用户 2026-11-04 暂按现口径，后续逆向确认。
- Heat Wave 判定的 >100% 多层折算按线性期望处理（时序细节已由用户确认）。
- **Binary System 的 +100% 语义**（v1 按结构翻倍；若为额外独立 more：本体 +11.3%；若还继承 Ignite：单次总伤害约 +88.7%）。用户 2026-11-04 记录待逆向。
- 冷却恢复速度与冷却缩减的具体作用顺序细节（v1：`(基础 + 固定秒数) ÷ (1 + ICR)`，Recurrence 60% 归入 ICR 池；映射未确认，另一解读 3.64s）。
- 施法时间公式（v1 采用参考公式 `1/(as × speedMultiplier × (1 + cast speed))` = 0.5043s；旧口径 0.4438s 为另一分支）。
- Ignite 伤害的记账方式（v1 连续流积分 + 1 秒切片事件，切片之和 = 层总伤害）。
- Preparation 的下一次施法状态（v1 不默认生效）。
- 手套 overcap-cold 派生词条是否按整数 chunk 取整（v1 用连续比例，input snapshot 记为 assumption）。
- 任务属性点（+2 全属性）未纳入（影响约 +0.5% 总伤害）。
- 未实现：法力消耗与恢复、Chill/Blind 等非伤害状态、双星空间轨迹（采用全覆盖假设场景）、其他技能树效果、Crescendoing Rage（后续阶段）。
- 2026-11-06 起，**条件 Buff**（Craterborn / Infusion / Chilled Touch / Eternal Flames / Crescendoing Rage / Ambition /
  Frozen Steel / Preparation / 神像火伤门槛 / Apex Glyph 转换等）由 `le_calc.conditional` 单独发现、筛选与配置。
  v3.2 起，固定条件机制可由 `conditional/recompute.py` 在“生成场景”下独立重算（`conditional-recompute.json`），
  但 **result.json 的主 DPS 基线数值不变**；Frozen Steel / Conflagrate / Apex 等机制仍未建模；
  单次施法状态只进单次重算，循环口径仍缺动作相位。见 §3.5 与
  `作者本地历史记录（不随公开仓库分发）`。

## 6. 产物路径

```text
reports/phase-3/black-hole-stage1/<run-id>/
  run-manifest.json
  input-snapshot.json
  source-ledger.json
  rule-ledger.json
  result.json
  black-hole-damage.html
  conditional-effects.json      # 条件效果注册表指纹 + 主技能依赖 + 来源检查目录（2026-11-06 起）
  conditional-scenario.json     # 本轮条件场景（可保存/恢复/交给后续重算）
  conditional-evaluation.json   # 条件求值 + 拟议 contribution + 校验结果（旧 DPS 不重算）
  conditional-recompute.json    # 生成场景的跨技能 Buff 重算（v3.2；基线 vs 场景，旧 DPS 不变）
  README.zh-CN.md
```

run-id 形如 `20261104T193000-bh-stage1-v1`。新版本不覆盖旧目录。

正式 run 使用案例声明的规则包与场景（公开版默认规则包 `tools/phase3/rules/ruleset-bh-stage1-v4.json`）。
运行产物写入案例 `output.root` 下的新 run-id 目录。

条件 Buff 工件单独复核：

```powershell
$env:PYTHONPATH = 'tools\phase3\src'
# 只求值/导出（不跑伤害引擎）
& .\.venv\Scripts\python.exe -m le_calc conditions `
  --snapshot reports\<case>\<run-id>\input-snapshot.json `
  --ruleset tools\phase3\rules\ruleset-bh-stage1-v4.json `
  --out reports\<case>\<run-id>
# 附带跨技能 Buff 重算（写 conditional-recompute.json）
& .\.venv\Scripts\python.exe -m le_calc conditions `
  --snapshot reports\<case>\<run-id>\input-snapshot.json `
  --ruleset tools\phase3\rules\ruleset-bh-stage1-v4.json --recompute `
  --out reports\<case>\<run-id>
```

完整报告可选 `--scenario <scenario.json>` 生成条件场景重算；`--no-recompute` 可跳过重算以加速。

审查修复（2026-11-07）：条件场景新增 `target`（等级/火抗/boss/rare），`prepare_scenario`/`recompute_scenario`
支持 `baseline_profile`（build_base vs legacy_single_skill）、R7 fail-closed、R4 assumptions、R5 application_states、
R8 boss/rare 拆分；`compute_scenario_outputs(prepared)` 一次性产出 baseline/scenario 单次+循环，一键重算约 2.65s。
验收：330 tests OK；headless 112/112；详见 `作者本地历史记录（不随公开仓库分发）`。

一键重算（可选，浏览器整页更新）：
```powershell
$env:PYTHONPATH = 'tools\phase3\src'
& .\.venv\Scripts\python.exe -m le_calc serve `
  --snapshot reports\<case>\<run-id>\input-snapshot.json `
  --ruleset tools\phase3\rules\ruleset-bh-stage1-v4.json `
  --calc-input reports\<case>\<run-id>\calculation-input.json `
  --build reports\<case>\<run-id>\bd.json --monster-level 100 --port 8765
# 然后在离线报告的条件 Buff 页点击「应用条件并重算 DPS」
```

## Flame Reave hit 模型扩展（新案例 `flame-reave`）

hit 模型是现有 `le_calc` 的技能模型扩展（与 BH 的 `damage.py`/`ignite.py` 同层），不是第二套管线：

| 文件 | 职责 |
|---|---|
| `src/le_calc/hit_inputs.py` | `case.json` + calculation-input + dataset → hit 快照（`model.Snapshot`；`derived.model=hit` / `skill` / `weapon` / `hit_count` / `node_effects`） |
| `src/le_calc/hit_ruleset.py` | `le-ruleset-hit-v1` 加载与校验（rules/effects/attack_model/hit_events/rotation） |
| `src/le_calc/hit_damage.py` | 单次命中：`(base + added×scaling) × (1+Σinc) × Π(1+more) × avgCrit × (1+pen) × (1−DR)`，按标签过滤 |
| `src/le_calc/hit_simulate.py` | 攻速/APS、命中间隔、命中权重、冷却限制、5/30/120s DPS 窗口 |
| `src/le_calc/hit_buffs.py` | 条件 Buff 筛查候选（modeled / not_modeled / unresolved + evidence + next_probe） |
| `src/le_calc/hit_report.py` | `le-hit-result-v1` 汇总 + `buff-screening.json` + `run-manifest.json` + 离线 HTML |
| `rules/ruleset-fr-hit-stage1-v2.json` | 当前规则包（24 rules / 22 effects；v2 为 P1 文案清理 revision，数值与 v1 相同） |
| `rules/fr-hit-facts-v1.json` | 机制研究事实（45 facts，R1–R8 + OQ1–OQ10） |

入口：`le.ps1 run/validate --case examples/<case>/case.json`（`scripts/le_commands.py`
按 `case.model` 分派；BH 分支不变）。底层命令见 `le_calc hit-snapshot` / `hit-run`（`cli.py`）。
结果状态固定为 `assumption`；条件敏感性见 `作者本地证据（不随公开仓库分发）`。

