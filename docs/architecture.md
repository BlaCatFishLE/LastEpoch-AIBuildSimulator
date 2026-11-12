# 架构与格式契约（architecture）

本文件说明：模块各自负责什么、数据怎么流、主要格式的字段与**绑定规则**、以及哪些对象一旦发布就不能改写。
字段级权威定义在各契约中（[docs/contracts/](contracts/)）；机制证据与假设见 [mechanisms.md](mechanisms.md)；操作流程见 [workflow.md](workflow.md)。

> 说明：本文件描述公开版的通用架构。作者本地历史案例、历史 run 与归档材料不随仓库分发；文中路径均为仓库根相对路径。

## 1. 总览

```text
原始输入                          中间格式                         计算                              产物
──────────────────────────────────────────────────────────────────────────────────────────────────────
离线存档 (EPOCH + JSON) ─┐
planner payload ─────────┼─→ BuildSpec v1 ──→ calculation-input-v1 ──┐
本地人工输入/覆盖 ───────┘     (build-spec-v1)   (calculation-input-v1) │
词缀库 (affix-db) ──────────────────────────────────────────────────┼─→ input-snapshot ─┐
                                                                     │   (静态属性快照)    │
规则包 + 条件注册表 + 条件场景 ──→ 条件求值/重算通道 ────────────────┘                    │
                                                                                          │
                                      le_calc 计划编译/执行 ────────────────────────────────┴─→ result (v6)
                                                                                                ├─→ 玩家 HTML（离线单文件）
                                                                                                ├─→ source-ledger / rule-ledger / 条件工件
                                                                                                └─→ run-manifest（指纹 + 一致性）
```

关键纪律：**同一份冻结输入 → 同一份结果**；报告只读结果 JSON，前端不重算伤害规则；已发布的 dataset / revision 不就地改写；每一步都能从案例目录里的输入重新生成。

## 2. 模块职责

| 模块 | 职责 | 入口 |
|---|---|---|
| [`tools/save_import/`](../tools/save_import/README.md) | 离线存档解码（clean-room、只读）：`ItemData` v6 字节布局、容器枚举、名称/效果 resolver；输出 `save-build-v1` | `python -m le_save_import` |
| [`tools/build_import/`](../tools/build_import/README.md) | planner payload / 存档结果 → BuildSpec v1；导出 calculation-input-v1；BD 可视化 | `python -m le_build` |
| [`tools/affix_db/`](../tools/affix_db/) | 词缀 × 基底 × 位置 roll 范围数据库；`affix_base_ranges` 是唯一权威范围表 | `python tools/affix_db/run.py` |
| [`tools/phase3/`](../tools/phase3/README.md) | **计算引擎**：输入快照、计划编译/执行、条件效果、结果门禁、报告 UI、本机重算服务 | `python -m le_calc` |
| [`tools/knowledge/`](../tools/knowledge/README.md) | 知识索引构建与查询（`knowledge rebuild/search/show` 的实现） | `python -m le_knowledge` |
| [`tools/ingest/`](../tools/ingest/) | 数据提取与规范化（LE Tools / Tunklab → SQLite dataset）；研究/重建用，普通计算不需要 | `python -m le_data.cli` |
| [`tools/extract-game/`](../tools/extract-game/README.md) | 本地游戏资源只读检查（UnityPy，按需）；研究工具，不参与常规计算 | 脚本 |
| [`scripts/le.ps1`](../scripts/le.ps1) | 统一操作入口：doctor/import/prepare/run/serve/validate/test/knowledge/package | `.\scripts\le.ps1 <cmd>` |
| [`scripts/le_commands.py`](../scripts/le_commands.py) | CLI 实现：参数规范化、命令分派、退出码 | 由 `le.ps1` 调用 |
| [`scripts/le_pipeline/`](../scripts/le_pipeline/README.md) | 输入路线、外部依赖发现（tool-native harness）、phase-3 顺序层与 onboarding | 被 CLI / 引擎调用 |
| `scripts/fetch_dataset.ps1` | 从 GitHub Release 下载 dataset 并按 `data/current.json` 校验 sha256 | 独立 PowerShell 脚本 |
| `scripts/package.py` / `check_package.py` | 按显式清单打包发布 / 解压后校验 | 发布流程 |
| `config/` | 项目配置模板（`project.json`）与覆盖率分母（`coverage-scope.json`）；本机配置不入库 | — |
| `knowledge/` | 机制/技能/定义 JSON 权威源 + 可重建 SQLite 索引 | `knowledge rebuild` |
| `examples/_template/` | 空案例模板（唯一入库的 examples 内容） | 复制后补齐或由 `import` 生成 |
| `reports/` | 每次运行的独立产物（用户本地目录，gitignored） | `reports/<case>/<run-id>/` |
| `data/` | dataset 指针、词缀库、映射、发布物（大文件不入库） | `data/current.json` |

`tools/phase3/src/le_calc/` 内部分层：`model.py`（冻结数据契约）、`catalog.py`/`inputs.py`/`stats.py`/`ledger.py`（输入适配 + 静态快照 + 来源账）、
计划编译/执行（v1–v4 版本化计划、`plan_v4` 数据驱动执行与 result-v6 门禁）、`report.py` + `report_ui/`（展示层）、
`conditional/`（条件效果发现/场景/求值/重算）、`serve.py`（本机回环服务）、
`workflow/`（阶段执行/StateStore/阶段缓存/提交/原子写）、`mechanics/`（共享算子）。
文件所有权与写入范围见 [`tools/phase3/README.md`](../tools/phase3/README.md)。

## 3. 数据流

1. **数据层**：`discover → fetch/cache → parse → normalize → validate → publish`
   （原始来源快照 → staging → `data/releases/<dataset>/` + `data/current.json`）。
   普通用户只需要下载已发布 dataset，不运行数据层。详见 [data-dictionary.zh-CN.md](data-dictionary.zh-CN.md)、[architecture-decisions.zh-CN.md](architecture-decisions.zh-CN.md)、[source-inventory.zh-CN.md](source-inventory.zh-CN.md)。
2. **构筑层**：planner payload 或离线存档 → BuildSpec v1 revision（不可变）→
   `calculation-input-v1`（扁平 mod 列表）。本地缺值通过 `build-local-inputs-v1` 输入并以**新 revision** 生效。
   注意：存档 → BuildSpec 的**完整首接**需要可选外部 tool-native（Node + `lastepochsupertools`，不随仓库分发）；
   无该依赖时只能做 clean-room 骨架导入。planner payload 路线离线、不依赖 Node，但需手工补齐案例字段。
3. **计算层**：`calculation-input-v1` + dataset + affix-db → 静态属性快照 `input-snapshot` →
   计划编译（`compile_plan`）→ 执行（`execute_plan`）→ 结果门禁 → 报告 HTML。
4. **条件层**：注册表 + 当前构筑 → 候选/检查/场景 `conditional-scenario-v1` → 条件求值 →
   条件重算产物（生成场景重算，**不改写**基线结果的 DPS）。
5. **本地服务**：`le.ps1 serve` 在本机回环地址启动，报告页“应用条件并重算”才触发重算；报告本身不联网。

## 4. BuildSpec v1（`build-spec-v1`）

- Schema：`tools/build_import/schemas/build-spec-v1.json`。
- 生产者：`tools/build_import`（planner）与 `tools/save_import` 的管线上游；消费者：`le_build export`、报告可视化。
- 顶层字段：`build_spec_version, parser_version, mapping_version, meta, catalog, scope, character,
  passive_tree, specializations, action_bar, equipment, idols, blessings, weaver, context, progression,
  readable_summary, extensions, issues, statuses, coverage, value_resolution_notes`。
- 关键规则：
  - `meta` 记录构筑身份与本地输入指纹（`local_inputs`：文件 sha256、条目、逐条 applied）。
  - 每个词条/节点带 `value_source`（`source` / `local_input` / `local_value_override`）与 `input_id`；
    玩家视图对本地输入行显示“手动”。
  - 未提供数值不填 0：缺失 roll 用 `unresolved_inputs` 显式列出，并给可执行命令模板。
  - revision 由内容哈希决定；同内容重放 → `state=exists`，不新建 revision。

## 5. 本地人工输入（`build-local-inputs-v1`）

- Schema：`tools/build_import/schemas/build-local-inputs-v1.json`。
- 顶层：`version, build_id, updated_at, entries`。
- 每条输入按**变量**（而非 roll 槽）定位；一个 roll 槽可能驱动多个显示变量，必须逐变量给值。
- 该文件是“玩家可见但来源未提供”的数值的唯一合法来源；写入后必须 `sync` 成新 revision 才进入计算。

## 6. calculation-input-v1

- Schema：`tools/build_import/schemas/calculation-input-v1.json`。
- 生产者：`python -m le_build export`；消费者：计算引擎的输入层。
- 顶层字段：`schema_version, generated_at, build_id, revision_id, revision_seq, content_sha256, source,
  game_version, dataset_id, scope, character, skills, passive_tree, items, mods, excluded_non_dps,
  excluded_non_dps_properties, excluded_non_dps_mod_ids, unresolved_inputs, notes`。
- `mods[]` 每条有稳定 `mod_id`、`stat`（stat_id/name/unit/resolved）、`property/tags/special_tag/modifier_type`、
  `value_numeric/display_value/value_status/value_source`、roll（raw/roll_id/index/status）、
  定义范围（min/max/direction）、`hidden`、`tier/channel`、`readable_text`。
- 非 DPS 属性（掉落率等）从计算输入中排除并记入 `excluded_non_dps_*`，BuildSpec/原始来源仍保留。
- 同一 revision 反复导出字节一致（`generated_at` 默认取 revision 的 `published_at`）。

## 7. 计算输入快照（input-snapshot）

- 生产者：计算引擎的输入适配层；命令 `le_calc snapshot`（或 `run` 时自动生成）。
- 顶层：`build_meta, character, attributes, resistances, derived, issues, excluded_sources, contributions`。
- `build_meta` 必须冻结全部上游绑定：build/dataset/calc_input 的 id 与 sha256、指针状态与 revision 选择。
- `contributions[]` 是单条属性来源记录：`stat_key, operation(added|increased|more|base|flag),
  damage_tags, condition_code/condition_status, included/exclusion_reason, value_source,
  status(confirmed|derived|assumed|unresolved)`。
- `resistances[name] = {total, cap, capped, overcapped}`；快照是**已判定条件之后**的静态结果，引擎只消费 `Contribution` 与标量摘要。
- `revision_selection.current_pointer_drift=true` 表示快照固定时 `data/current.json` 已漂移 → 视为输入错误，需显式选 revision。

## 8. 条件场景（`conditional-scenario-v1`）

- 契约：[contracts/conditional-interface.md](contracts/conditional-interface.md)；schema/工件由案例与注册表声明。
- 顶层：`schema, preset_id, mode(fixed_conditions), baseline_profile, binding, effects, states, target,
  single_cast, loop`。
- `binding` 是**版本绑定**（缺一不可）：`build_id, revision_id, build_content_sha256, calc_input_sha256,
  dataset_id, ruleset_id, ruleset_sha256, registry_id, registry_sha256`。
- `effects`：逐效果 `enabled/stacks`；`states`：共享状态（目标、玩家 Buff 假设等）；`target`：目标类型/抗性/等级。
- `baseline_profile` 决定基线口径（例如是否包含常驻自动效果）；**不同基线不能混用或相减当作增益**。
- 保存/恢复 = 原样导出/导入该 JSON；绑定不匹配、未知效果、层数越界必须拒绝（fail-closed）。

## 9. 计算计划与结果

- 现行计划/结果：`calculation-plan-v4` / `result-v6`，契约见 [contracts/calculation-plan-v4.md](contracts/calculation-plan-v4.md)。
  历史版本 v1–v3（result-v3–v5）仅作兼容与历史边界，字段见对应契约。
- 计划是纯 JSON：记录组件、effect、来源/所有权、schedule、applications/charge、execution 与 `plan_sha256`/`plan_id`；
  执行器只消费计划/场景/静态 handler registry，不读历史案例常量。
- 结果的顶层（v6）：`schema, run, input, scope, scenario, action_policy, snapshot, source_ledger, rule_ledger,
  modifier_graph, single_cast, loops, issues, excluded_sources, conditional` 等；`status=assumption` 必须原样显示。
- `run-manifest.json` 记录每个产物文件的 SHA-256 与一致性检查（如分量之和等于总量、窗口顺序等）。
- 报告层：`report_ui/` 把结果转成展示 view 并生成单文件 HTML；HTML 只读结果 JSON，前端只做展示/插值/坐标计算，
  **不重新实现伤害规则**；产物离线、无外部请求。

## 10. 绑定、基线与不可变规则

| 规则 | 说明 |
|---|---|
| 一处维护 | 业务源码、规则、脚本、案例只有一个维护位置（本仓库）；发布包只是选择与复制。 |
| 不可变发布 | `data/releases/<dataset>/`、`data/builds/**/revisions/<rev-id>/` 发布后不就地改写；变更 = 新 dataset / 新 revision。 |
| 指纹传递 | `build content sha256 → calc_input sha256 → snapshot build_meta → result.input → run-manifest.files`，任一层不一致即视为非法组合。 |
| 基线口径 | 结果中的 baseline/场景命名由案例显式声明；不同基线不得混用或相减当作增益。 |
| 非语义字段 | `generated_at`、`run_id`、`out_dir`、`source.path` 等不参与内容比对；其余结构/数值按声明容差比较。 |
| 缺失值 | 未知 ≠ 0 ≠ 空串。数据层用 `field_evidence.evidence_status`，构筑层用 `unresolved_inputs`，计算层用 `status`/`issues`。 |
| 无 monkey patch | 运行时不得改写计算常量或伪造结果；样例断言属于验收，不属于引擎。 |
| 路径 | 所有可执行路径相对**案例配置或项目根目录**解析，不依赖当前工作目录；配置里不写机器个人绝对路径。 |

## 11. 案例配置与项目配置

### 11.1 案例配置（`le-case-v1`，`examples/<case>/case.json`）

案例配置是“可执行流程只读本文件”的契约：引擎/脚本不在代码里保留某个案例的常量；换 BD = 新建案例目录 + 新 `case.json`。
主要字段组：

| 字段组 | 作用 |
|---|---|
| `schema / case_id / title / description / created` | 案例身份与说明 |
| `game_version / monster_level` | 目标游戏版本与默认怪物等级 |
| `dataset` + `dataset_expected{dataset_id, logical_content_sha256, file_sha256, target_game_version}` | dataset 指针 + 期望指纹（防止用到别的 dataset） |
| `build{...}` | 构筑身份与绑定：`build_id, build_dir, revision_id, revision_seq, content_sha256, build_spec, build_spec_sha256` |
| `affix_db` | 词缀库路径 |
| `ruleset{id,path,sha256}`、`conditional_registry{id,path,sha256}` | 规则/注册表版本绑定 |
| `inputs{bd, calculation_input, corrections, idol_refraction_selection, snapshot, scenario}` | 案例输入路径 |
| `source{character_save, stash, planner_build_response, save_extract, decoded_items}` | 原始来源 |
| `input_sha256{...}` | 每个输入的指纹；不一致即视为输入被改过 |
| `snapshot_expectations{...}` | **案例级结构断言**（不是通用规则；换 BD 必须重新声明） |
| `output{root, run_id_template, canonical_run_id}` | 输出根、run-id 模板、规范 run id |
| `expected` | 预期摘要路径（可为空；公开版不提供作者案例的期望值） |
| `model / main_skill / calculation` | 兼容/路由声明；以当前路由契约与知识适用性为准 |

`examples/_template/case.json` 是空模板（路径为 `examples/<your-case>/...` 占位、数值字段留空）；推荐由 `import` 生成实际骨架。

### 11.2 项目配置（`config/project.json`，`le-project-config-v1`）

- `python.candidates`（`.venv/Scripts/python.exe` → `python`）、`env_override: LE_PYTHON`、`min_version: 3.12`；
- `git`：只用于版本信息（doctor / package-manifest），`required_for_runs: false`，`LE_GIT` 可覆盖；
- `dataset.pointer`、`affix_db`、`ruleset`、`conditional_registry`、`report_root`、`run_id_template`；
- `external_tools`（可选，默认 `null`）：`node` 与 `tool_native` 仅 tool-native 新存档路线需要；
  未配置时依赖检查为 `undetermined`、`prepare` 返回 `needs_input`，**不静默改用语义不同的解析路线**；
  这些外部运行时不在公开包内；
- `overrides`（环境变量，进程内生效、不写回）：`LE_REPO_ROOT`、`LE_PYTHON`、`LE_GIT`、`LE_CASE`、`LE_OUT_ROOT`；
- `outputs{run_manifest, package_dir, package_excluded}`：打包清单名、输出目录与排除项；
- 路径解析：所有相对路径相对**项目根目录**（由 `scripts/le.ps1` 的 `$PSScriptRoot\..` 推导），不依赖 CWD、不写机器绝对路径。

### 11.3 预期摘要（可选）

如案例提供 expected-summary，则记录数值基线、容差与 `checks[]`（文件 + JSON pointer + 期望值）；
公开版不为作者案例分发 expected 摘要，用户自行建立的案例可以按同一 schema 记录自己的基线。
约定：**摘要里的值必须来自真实运行产物，不得用“重新生成的预期值”覆盖实测差异**；差异要如实报告。

## 12. 扩展点（支持下一份 BD 时改哪里）

| 需求 | 改动位置 | 不要动 |
|---|---|---|
| 新案例（同主技能） | 新增 `examples/<case>/` + `case.json`（建议从 `_template` 或 `import` 开始） | 通用规则、其它案例输入 |
| 新主技能的计算 | 共享定义/知识条目 + 规则包 + 引擎能力（按需最小实现） | 既有 run 产物、已发布 dataset |
| 新条件效果 | 条件注册表 + `conditional/` 映射 + 场景 | 旧 DPS 基线数值 |
| 新数据来源/版本 | `config/sources.example.json` + `tools/ingest` 适配器 + 新 dataset | 已有 dataset 发布物 |
| 打包/发布清单 | `scripts/package.py`、`scripts/check_package.py` | 不进包的本地配置/缓存 |

新增技能/机制的判定顺序与缺口登记要求见 [workflow.md](workflow.md)“新机制接入与最小语义验证”与 [mechanisms.md](mechanisms.md)。
