# 可执行技能定义 v3（`definitions_v3`）契约

状态：S2/S3 VC 切片 + S4 FR 多命中原生定义扩展现行；BH 扩展在后续切片实现。日期：2026-11-11。
位置：
- 共享定义：`knowledge/definitions/void-cleave-fire-main-v3.json`、`knowledge/definitions/fr-flame-reave-v3.json`
- schema：`tools/phase3/schemas/skill-template-v3.json`
- 场景：案例侧 `examples/<case>/inputs/scenario-v3.json`；多评估时按定义声明，一评估一场景文件（如 `scenario-<evaluation_id>-v3.json`）
- 绑定器/IR：`tools/phase3/src/le_calc/definitions_v3.py`、`plan_model.py`

## 1. 三层分离

```text
共享定义（definition：技能语义/规则/selector/证据，无场景默认值、无当前 roll/节点总值/实例 ID）
  + 当前 FrozenContext（calculation-input/BuildSpec/dataset/catalog）
  + 场景（scenario：控件默认值/target/ monster_level/resistance）
  → bind_template（真实输入解析 selector，必需/可选/多实例语义）
  → ResolvedModel（独立中间表示：definition + controls + scenario + instance）
  → compile_from_resolved_model（共享计划装配器）
```

- v3 生产路径不读取旧 `dps-profile-v2.json`；profile-v2 适配器也先归一为同一 `ResolvedModel`，不把 v3 输出伪装成可序列化 profile。
- `ResolvedModel.assembler_view()` 仅存在于内存，供共享装配器使用；不写 profile schema 文件，不做 profile validate。
- template 不携带 `monster_level`/`resistance`/控件默认值；这些只在 scenario 文件。
- template 的 `definition.evidence` 是数组；schema 明确顶层/definition/selector 允许字段。

## 2. 输入 override

v3 入口与 profile 入口使用同一交接规则：

- `calculation_input`/`build_spec` override 必须同时提供合法 `*_sha256`；缺失/空串/非法格式拒绝；
- 实际文件 sha 必须与 artifact_ref 一致；calculation-input 的 build/revision/content 身份必须与 BuildSpec 一致；
- 合法 override 真正进入 `prepare_context` 与 plan input_refs，不会静默忽略；
- 不存在路径、错误 SHA、跨构筑身份均明确拒绝。

## 3. selector 语义

- selector 使用稳定身份/序号：`item_definition_entity_id` 或 `item_base_entity_id`、`expected_source`、
  `expected_property/special_tag/tags`、`expected_unique_mod_index`、`expected_item_mod_index`、
  `expected_description_index`；**不使用当前 roll 数值文本**。
- `selection=one`（默认）：命中恰好 1 个；0 个时按调用方 required 处理；>1 为单实例歧义拒绝。
- `selection=many`：合法多个实例分别生成来源与所有权；不允许在绑定阶段合并/覆盖。
- 可选 effect（`required=false`）来源缺失：形成 not_applicable，不抛绑定异常、不伪造实例。
  必需 effect 来源缺失：明确拒绝/阻断，不静默补来源。
- selector 命中 0/1/多实例都区分“来源缺失/合法多实例/单实例歧义”，不再统一“必须唯一”。

## 4. schema 与错误

`validate_template` 在绑定前执行结构校验：

- 顶层/definition 未知字段拒绝；`schema`、`binding_version`、`scenario_schema` 固定；
- `definition.evidence`/`limitations`/`effects` 等类型必须正确；
- selector 未知字段拒绝，禁止 `item_instance_id`/`mod_id` 混入 template；
- scenario 控件键必须与模板 controls 完全一致；缺/多键拒绝；
- 非法 template/scenario → 编译 rejected/blocked，不进入计划。

## 5. VC 计划/结果版本

VC 组件基数仍为 1 hit + 1 dot、`fixed_repeat`，与 plan-v1/result-v3 语义兼容：

- plan input_refs 增加 `template`、`scenario`，不含 `profile`；
- `profile_sha256=None`；来源身份由 template/scenario/input_refs 表达；
- 不提前实现 plan-v2/result-v4；FR/BH 需要多 hit、0..N dot、有限调度/充能时在 S4/S5 版本化。

## 6. 当前证据

- 正式 run：作者本地运行产物（`reports/<case>/<run-id>/`，不随公开仓库分发），plan `46ba62b4f39665d4d4f966f3d90ac90f0475a27b3c4e74d8f400d3206626f418`；
- 与 workflow-6：数值、coverage（partial/7）、contributions、applications、effect_ledger、issues 完全一致；
  差异仅 plan/execution 指纹、`profile_sha256=None`、输入引用来源；
- 定向测试：`test_unified_skill_migration` 13 项（含 override、可选/必需、多实例、schema 负例）。

## 7. S4 FR 扩展：plan-v2 定义与多场景绑定

FR 定义（`fr-flame-reave-v3.json`）在 v3 契约内新增以下**显式声明**；VC 未声明这些字段，行为不变：

1. `definition.plan_version`：`le-calculation-plan-v1`（缺省）或 `le-calculation-plan-v2`；
   plan-v2 只允许由显式声明启用，产物走 `docs/contracts/calculation-plan-v2.md`。
2. `definition.evaluations[]`：`{evaluation_id, label, metrics, primary}`；plan-v2 定义必须声明且恰好一个
   `primary=true`。每个评估在案例侧绑定独立 scene 文件（见 §8）。
3. `skill.components`：plan-v2 允许只声明 hit 组件（0..N hit + 0..N dot，总数 ≥ 1）；
   hit 组件的 `damage_type` 显式限制组件类型；多命中计数由 `rules.hit_events` 产生。
4. 新增规则处理器（结构在加载期校验，未知字段/枚举拒绝）：
   - `rules.hit_events`：`base_hits` + `return_sources[]`（tree/node/require_points/returns）+ 额外命中伤害倍率或显式权重；
   - `rules.rotation`：具名频率策略、主策略、窗口语义、`rhythm_cycle`（节点消费加成从真实输入绑定）；
   - `rules.item_effectiveness`：装备效果来源（selector）→ 目标槽位所有来源乘数；显示稀有度前提
     （`require_display_rarity`/`tier0_min`/`excluded_base_type_ids`）由当前词缀层级判定；
   - `rules.added_damage_conversion`：标签闭包 added 行池 → divisor × 节点值 → 输出 added 行；
   - `rules.row_normalizations`：显式旧基线口径归一（含 match 稳定锚点与 set 值/条件/原因；
     锚点未命中当前来源即编译拒绝）。
5. `definition.skill.base_attack_rate`：plan-v2 必填，dataset `extensions.as × speedMultiplier`；
   `action_s` 保留 dataset castTime 供目录一致性校验，不再承担攻击频率基数。

## 8. 多场景来源（FR）

`case.calculation.source` 在 `kind=definitions_v3` 下支持两种互斥形态：

```json
{"kind":"definitions_v3","template":"knowledge/definitions/<x>-v3.json",
 "scenario":"examples/<case>/inputs/scenario-v3.json"}
```

```json
{"kind":"definitions_v3","template":"knowledge/definitions/fr-flame-reave-v3.json",
 "scenarios":[{"evaluation_id":"single_hit","path":".../scenario-single-hit-v3.json"},
              {"evaluation_id":"dps","path":".../scenario-dps-v3.json"}]}
```

- `scenarios` 与模板 `definition.evaluations` 的 id 集合必须一致（多/缺均拒绝）；多场景仅 plan-v2 可用；
- 每个场景文件仍是 `le-skill-scenario-v3`，并与模板 controls 精确同键；`primary` 评估决定计划
  controls 默认值与计划 `scenario` 语义；
- `prepare_context`/路由把每个场景文件作为 `role="scenario"` 的 `input_refs`；`ResolvedModel.evaluations`
  保存每个评估的 `scenario_path` 与 `scenario_sha256`（= canonical `{states,target}` 哈希）；
- 生产绑定仍不读取旧 `dps-profile-v2.json`，不 import `hit_inputs`/`hit_damage`/`hit_simulate`。

## 9. FR 当前证据

- 正式 run（复验 P1 补修后）：作者本地运行产物（`reports/<case>/<run-id>/`，不随公开仓库分发），plan `le-calculation-plan-v2`
  （`a7c1b51af75da5245b8568d24152af486d4caa511d691c326cfe5934fbcf7c4e`），result `le-dps-result-v4`；
  历史 run 保留只读；
- 与 legacy FR 基线（作者本地冻结产物，不随公开仓库分发）：
  single_hit / DPS / 每次施放伤害 / 每秒施放 / 每秒命中 / 策略对照全部在 1e-9 内一致；
  差异仅计划/结果 schema、coverage 显式缺口（partial/18）、输入来源表示；
- 多评估执行身份按 evaluation_id 聚合全部评估场景哈希（`scenario_sha256`），主场景哈希另存
  `primary_scenario_sha256`；report-binding/manifest/validate 同步；
- 定向测试：`tools/phase3/tests/test_unified_fr_migration.py`（D7 合成多命中、D12 legacy 对照、
  真实绑定/实例变化/稀有度前提/命名评估独立性/非主评估场景身份变化/应用组件引用/契约负例/
  正式 run 产物）。

## 10. BH 当前证据（S5，plan-v3）

- 共享定义：`knowledge/definitions/black-hole-v3.json`（`plan_version=le-calculation-plan-v3`）；
  两个具名场景（案例侧 `examples/<case>/inputs/scenario-{eager,patient}-v3.json`，states/target 相同，
  差异在 evaluation 的 `action_policy`）；
- 正式 run（复验 P1 补修后）：作者本地运行产物（`reports/<case>/<run-id>/`，不随公开仓库分发），plan
  `5b7747a587308c09b8cfb194f8d9b54ccb109c8fecc6c516bcd77115f52d953d`，result `le-dps-result-v5`；
  203 contributions / 609 application records / 203 条摘要；历史 run 保留只读；
- 与 legacy BH baseline 逐浮点一致：single_cast 10662227.4697164、eager horizon_dps
  2919759.11554595 / 35 casts / max_concurrent 3、patient 1337984.91923145 / 16 / 1、
  5/30/120s 窗口 body/ignite/DPS/tail 一致；差异仅 schema/计划指纹/coverage 缺口（partial/2）；
- 多评估执行身份仍按 evaluation_id 聚合场景哈希；主场景哈希另存；顶层投影按 primary 评估；
  应用账本按 contribution × {fire,cold,Ignite} 生成并驱动摘要/报告/coverage；
  结果门禁改用 `result.loops`/`single_cast`/应用账本/尾伤复算等 v5 检查（46 required）；
- 定向测试：`tools/phase3/tests/test_unified_bh_migration.py`（D8 手算、D9 调度/并发、D12
  legacy 对照、无旧计算器 import、plan-v3 负例、正式 run 产物）。

## 11. canonical 案例入口与迁移文件（S6，2026-11-11）

案例使用 definitions_v3 canonical 时，`case.json` 的 `calculation.source.kind=definitions_v3`：

| 项 | 约定 |
|---|---|
| 案例配置 | `examples/<case>/case.json`（公开版不提供作者案例；用户按 `examples/_template/` 或 `import` 生成） |
| v3 定义 | `knowledge/definitions/<template>.json`（`definition.plan_version` 决定 plan/result 版本） |
| canonical run | 案例 `output.canonical_run_id`（作者本地产物不随公开仓库分发） |
| plan/result 版本 | plan-v1/result-v3、plan-v2/result-v4、plan-v3/result-v5 仅作历史/兼容边界；现行输出为 plan-v4/result-v6 |
| 迁移文件 | 如保留历史 profile/hit/legacy case（`case.profile-v2.json` 等），仅作只读迁移证据，不参与默认解析 |

`case.definitions-v3.json` 等迁移别名只作证据。默认入口必须解析到 `unified:definitions_v3`，不得静默回退旧 profile/hit/legacy 模型；模板 `migration` 字段登记 canonical_case / migration_case / canonical_run_id / entry_policy。验证与未迁移案例清单属于作者本地实施记录，不随公开仓库分发。

## 12. 候选目录、能力声明与适用性（第五步 G1/G2，2026-11-11）

- `knowledge/definitions/index.json`（`le-definition-catalog-v1`）是共享定义候选的派生目录：
  登记 template_id/path/schema/binding_version/ability_id/plan_version/revision/sha256/
  capabilities/applicability。模板正文仍是计算权威，目录不参与计算配置，也不是第二套知识仓库。
- 模板可选声明 `capabilities` 与 `applicability`；目录元数据与模板正文合并后由
  `definitions_v3.validate_template` 校验。未知能力、未知适用性事实/字段拒绝。
- 能力白名单按 plan 版本注册在 `definitions_v3.SUPPORTED_CAPABILITIES`；候选发现对不受支持的能力
  返回 `capability_gap`，绑定同样拒绝未知必需能力。
- 适用性事实限定为 ability_id/class/mastery/damage_tags/build.revision_id/dataset.dataset_id；
  非当前实例 roll/节点总倍率/旧 item_instance_id 不得作为匹配键。类别/标签 definite 不匹配
  返回 not_applicable，缺事实返回 undetermined。
- 新存档 import（G2）在 candidate 为 applicable_single 时才生成中性默认场景并显式写
  `case.calculation.source.kind=definitions_v3`；场景假设写入文件，不从历史案例复制。
  文档入口见 `docs/contracts/save-onboarding.md`。
