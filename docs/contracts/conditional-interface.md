# 条件 Buff 配置阶段：接口契约（实现契约 v1，2026-11-06）

> 本契约于 2026-11-07 定为正式文档；早期的协作/Chat 交接材料与作者本地归档不随公开仓库分发。
> 本文中的 `<repo>` 指仓库根；`<archive>` 指作者本地归档根（不随公开仓库分发）。
>
> 所有 schema/字段/状态语义以本文件为准；发现不适用时先与维护者沟通，不要各自改契约。实现相关的历史基线文档不随公开仓库分发。

## 0. 项目环境

- 项目根：`<repo>`（仓库根；所有命令在仓库根目录执行）。
- Python：`.\.venv\Scripts\python.exe`；运行 phase3 代码前设 `$env:PYTHONPATH='tools\phase3\src'`。
- 控制台中文：`$env:PYTHONIOENCODING='utf-8'; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8`。
- 回归测试：`& .\.venv\Scripts\python.exe -m unittest discover -s tools\phase3\tests`；具体测试基线以当前代码与 CI 结果为准，本文不写死数量。
- 并行改动时：改文件前先读，不要删除/覆盖不属于自己写入范围的产物；不要动 `data/`、`tools/ingest/`、`tools/save_import/` 与其它模块的既有交付物。

## 1. 目标（建立时范围）

1. 从**当前案例的冻结构筑**（`examples/<case>/inputs/` 下的 bd.json / 计算输入、`examples/<case>/snapshot/` 下的快照，以及该案例规范 run 的结果）出发，交叉检查全部实际来源，形成 **候选清单 + 检查分母 + 数据缺口 + 已检查排除**。
2. 对候选做**相关性筛选**（直接/间接影响主技能本体/点燃/施法/CD/持续/目标状态），并区分 **相关性** 与 **实现支持** 两个维度。
3. 把相关效果建模成**可配置状态**（开关/层数/共享目标状态/单次施法状态/数值门槛/动作），提供 **场景 JSON**（可保存、恢复、校验、复核），以及给后续 DPS 重算的 **消费接口**（拟议 contribution / 需要的规则映射 / 缺口）。
4. 在 DPS 报告 `report_ui` 中新增条件 Buff 页：同一风格、离线单文件、无外部请求；页面允许操作状态并即时显示**选择/条件/支持**状态，但**不得伪造 DPS 变化**（不重算伤害，明确写“本页不重算 DPS”）。
5. 交付 JSON 工件 + 中文文档 + 测试。

**建立时明确不做**：真正的 DPS 重算/多技能时间模拟/新主技能/全游戏 Buff 数据库/本地计算服务。页面只输出场景与求值说明，供后续引擎消费；后续能力按现行契约扩展。

## 2. 并行写入范围（历史协作约定）

并行开发时按模块划分写入范围，只改自己负责的文件；先读后写，不覆盖他人产物。当前公开版的职责划分以 [AGENTS.md](../../AGENTS.md) 与各模块 README 为准：

- 规则/注册表：`tools/phase3/rules/*.json`；
- 条件后端：`tools/phase3/src/le_calc/conditional/**` 与对应测试；
- 条件页：`tools/phase3/src/le_calc/report_ui/pages/conditions.py` 与页面资源；
- 集成与报告：`report_ui/present.py`、`shell.py`、生成产物与交付文档。

- 研究可以读取 `data/`、`reports/`、`docs/`，但不要修改。
- 作者本地的归档研究材料不随公开仓库分发（见 `archive://` 约定）。

## 3. 数据与证据入口（研究用）

- 构筑 revision：`examples/<case>/inputs/bd.json`（raw save import）、`examples/<case>/inputs/calculation-input.affix-db.json`（计算输入，**主扫描对象**：items/skills/passive_tree/mods）、`examples/<case>/snapshot/input-snapshot.affix-db.json`。
- 现有结果：该案例规范 run 目录下的 `result.json`（含 snapshot/source_ledger/scenario/derived）、`source-ledger.json`、`run-manifest.json`（作者本地产物，不随公开仓库分发）。
- 规则包：`tools/phase3/rules/ruleset-bh-stage1-v4.json`（Black Hole 本体/点燃/穿透/CD/施法/目标侧的具体规则与规则账；`rules` 数组含 status）。
- 机制数据（只读）：`data/current.json` → `data/releases/phase1-dataset-003/data.sqlite`（约 250 MB catalog，**权威节点/被动/物品/buff 数值**）；装备词缀库 `data/affix-db/v1/normalized/affix-db.sqlite`。
  - catalog 入口：`le_calc.catalog.open_catalog()`；常用：`trees()`、`tree_by_source_id()`、`nodes_for_tree()`、`node_by_index(tree_entity_id, idx)`、`effects_for_owner(node_entity_id)`、`passive_bonus_payload()`、`buffs_named()` / `buff_by_entity_id()`、`skill_components()`、`entity()`。
  - 已确认：节点 description、effects 的 `raw_text`/`value_text`/`extensions.stat_name`、`buff_param_candidate`、`buff_mod_config`、`config_mod` 都可直接引用为证据。示例：Meteor 节点 25 Craterborn 的 effect 行含 `15%` 火穿、`15%` cast speed（noScaling）、`6` duration；`buff:letools:UIBuffs:14` duration 6000ms/max1。
  - 物品 mod 的 `readable_text`（计算输入）含唯一效果自然语言；`mods[].stat.name`/`special_tag`/`tags`/`modifier_type` 可用于解析。
- 旧方案候选线索：Craterborn、Flame Ward Infusion、Frost Wall Chilled Touch、Enchant Weapon Eternal Flames/Conflagrate、Crescendoing Rage、Throne of Ambition、Frozen Steel、Preparation、神像火伤 mana 门槛、Pyramidal Altar 布局 CDR、Apex of Thought Glyph 转换、药水类（当前无来源）。**这些只是线索，必须用本地数据/代码证据逐条核对，允许修正或补充。**
- 现有排除原因：`result.json` 的 `source_ledger`（240 条，含 `condition_code/condition_status/included/exclusion_reason/notes`）与 `excluded_sources`（14 条）已包含大量“已检查未纳入”理由，直接复用并交叉核对，不要重新发明。
- 逆向证据：`docs/evidence/reverse-engineering/`、`evidence/*.json`（如 Craterborn、Ambition 相关事实若在其中可引用；没有的证据不要编造 locator）。
- 快速侦察脚本（作者本地归档材料，不随公开仓库分发）：`<archive>\2026-11-07\scratch\root-files\recon_conditions.py`、`<archive>\2026-11-07\scratch\root-files\recon_items.py`，可复制/扩展。

## 4. 版本与指纹（写进所有产物的 `input`/`provenance`）

必须从文件实际读取并记录：
- build：`build_id=letools:toolnative-save`、`revision_id=rev-dbba378f6fe3d829`、`content_sha256`（来自 snapshot.build_meta.build）
- calc input：path + sha256（snapshot.build_meta.calc_input.sha256）
- dataset：`phase1-dataset-003` + dataset sha256（snapshot.build_meta.dataset）
- registry：self sha256（文件字节）
- ruleset：`bh-stage1-v4` + sha256
- game_version：`1.5.2`

---

## 5. Registry JSON schema：`tools/phase3/rules/conditional-effects-bh-stage1-v1.json`

由数据研究方编写；后端实现严格加载器（字段缺失/枚举非法要报可读错误）。除注明外全部字段必填。所有 id 用 lower_snake / dot 分隔稳定字符串。

```jsonc
{
  "schema": "conditional-effects-registry-v1",
  "registry_id": "bh-conditional-effects-v1",
  "game_version": "1.5.2",
  "dataset_id": "phase1-dataset-003",
  "created": "2026-11-06",
  "context": {
    "main_skill_id": "bh2",
    "main_skill_name": "Black Hole",
    "ruleset_id": "bh-stage1-v4",
    "modes": [
      {"id": "fixed_conditions", "label": "固定条件估算",
       "description": "选中的持续状态在观察期间保持；不模拟获得/维持这些状态的操作成本。"}
    ],
    "baseline_profiles": [
      {"id": "legacy_single_skill", "label": "旧版单技能基线", "description": "..."},
      {"id": "build_base", "label": "构筑基础状态", "description": "...", "default": true}
    ],
    "disclaimer": "当前设置描述计算时采用的条件；人工声明的持续状态不代表已经模拟获取和维持这些状态的完整操作过程。本页不重算 DPS。"
  },

  // 主技能计算依赖（第 3 步“主技能计算依赖说明”的结构化版本，同时供页面展示/文档引用）
  "computation_modules": [
    {
      "id": "body.fire",                      // 固定枚举：body.fire|body.cold|body.aggregate|ignite|ignite.application|cast|cooldown|duration|penetration.body|penetration.ignite|target.state|ailment
      "label": "火焰本体",
      "question": "哪些输入通过哪些规则影响火焰本体？",
      "consumes": ["increased_damage", "more_damage", "penetration", "cast_speed", "duration", "target_resistance", "monster_dr"],
      "calc_refs": ["le_calc.damage.compute_body", "le_calc.report.build_result"],
      "rule_ids": ["rule-bh-component", "..."],
      "notes": "五行公式：基础/点伤 → inc → more → 抗性穿透 → 怪物 DR → 每次结算 × 次数。",
      "evidence": [{"kind": "game_data", "detail": "...", "locator": "...", "snippet": "..."}]
    }
  ],

  // 共享状态（界面统一设置；跨效果共用同一份值）
  "shared_states": [
    {
      "id": "target.chilled",                 // 稳定 id
      "label": "目标处于冰缓",
      "kind": "bool",                         // bool | int
      "source": "user",                       // user | auto（auto 由引擎/派生属性决定，界面只读）
      "default": false,
      "bounds": null,                         // int 时 {"min":0,"max":20}
      "description": "只声明目标状态；不模拟冰缓来源、概率与覆盖时间。",
      "affects": ["frost_wall.chilled_touch", "passive.frozen_steel"],
      "evidence": [...]
    }
  ],

  // 候选效果（本轮已研究/已建模的效果）。注意：不是全游戏列表，只覆盖当前构筑中实际存在且进入配置区/已检查区的效果。
  "effects": [
    {
      "effect_id": "meteor.craterborn",
      "group_id": "meteor.craterborn",        // 同一 Buff 的多条效果用相同 group_id（页面可合并展示）
      "name": "Craterborn",
      "name_en": "Craterborn",
      "category": "other_skill",              // 页面筛选：other_skill|equipment|passive|main_skill|target|action
      "source": {
        "kind": "skill_node",                 // skill_node|passive_node|equipment|idol|blessing|idol_altar|main_skill_node
        "display": "Meteor · Craterborn · 5 点",
        "tree": "Meteor",
        "tree_entity_id": "tree:letools:LESkillTrees:me27",
        "node_id": 25,
        "node_name": "Craterborn",
        "slot": null,
        "item_instance_id": null,
        "item_display_name": null,
        "locator": "examples/<case>/inputs/calculation-input.affix-db.json#/skills/specializations/1/nodes/6",
        "evidence": [{"kind":"game_data","detail":"节点描述与 stat 行","locator":"data/releases/phase1-dataset-003/data.sqlite#node:letools:me27:25","snippet":"Casting Meteor grants the 'Craterborn' buff ..."}]
      },
      "match": {                              // 发现器如何从当前构筑证明“拥有该来源”
        "type": "skill_node",                 // skill_node|passive_node|equipment_mod|mod_id
        "tree": "Meteor",                     // skill/passive 用
        "node_id": 25,
        "min_points": 1,
        "instance_id": null,                  // equipment/idol 用，如 "equipment.ring2"
        "mod_id": null,                       // 精确到某条 mod 时用，如 "equipment.ring2#affix#sealed#0.0"
        "name_contains": null                 // 兜底文本匹配（仅在无可靠 id 时）
      },
      "values": [                             // 定义值；发现器按实际点数/roll 计算 actual
        {"key": "fire_penetration", "label": "火焰穿透", "per_point": 0.15, "constant": null,
         "unit": "fraction", "display": "+15%/点（5 点 = +75%）"},
        {"key": "cast_speed", "label": "施法速度", "per_point": null, "constant": 0.15,
         "unit": "fraction", "display": "+15%（不随点数缩放）"},
        {"key": "duration_s", "label": "持续时间", "per_point": null, "constant": 6.0,
         "unit": "seconds", "display": "6 秒"}
      ],
      "modifiers": [                          // 进入后续重算的拟议贡献；每条含作用分支与引擎落点
        {"modifier_id": "meteor.craterborn.fire_pen", "stat_key": "penetration", "stat_label": "火焰穿透",
         "operation": "added", "unit": "fraction", "target": "fire", "damage_tags": ["Fire"],
         "value_ref": "fire_penetration",
         "branches": ["body.fire", "ignite"],
         "engine_sink": "snapshot.contributions[stat_key=penetration,target=fire]",
         "dedupe_contribution_ids": [],
         "note": "作用于玩家火穿，本体与点燃都读取；点燃另有专属穿透但同属火焰穿透池。"},
        {"modifier_id": "meteor.craterborn.cast_speed", "stat_key": "cast_speed", "stat_label": "施法速度",
         "operation": "increased", "unit": "fraction", "target": null, "damage_tags": [],
         "value_ref": "cast_speed",
         "branches": ["cast", "cooldown", "loops"],
         "engine_sink": "snapshot.contributions[stat_key=cast_speed]",
         "dedupe_contribution_ids": [], "note": "改变施法时间与循环调度，不改变单次总伤害。"}
      ],
      "activation": {
        "kind": "persistent_buff",            // persistent_buff|passive|conditional_passive|stacks_buff|numeric_threshold|cast_state|action|baseline_included|layout
        "duration_s": 6.0, "max_stacks": 1,
        "refresh": "recast_refreshes",        // recast_refreshes|no_refresh|unknown
        "source_action": "cast:Meteor",
        "description": "施放 Meteor 后获得 Craterborn；不叠层。"
      },
      "control": {                            // 用户可操作部分
        "kind": "toggle",                     // toggle|stacks|cast_state|none
        "state_id": "player.craterborn_active",  // toggle 绑定的共享状态；stacks 用 stack_state_id
        "stack_state_id": null,
        "label": "假设 Craterborn 持续有效",
        "help": "固定条件估算：假设整个观察期维持该状态。",
        "default": false
      },
      "conditions": {                         // 生效条件（声明式；求值器只支持这些操作）
        "all": [{"state": "player.craterborn_active", "equals": true}],
        "any": [],
        "note": "需要先施放 Meteor；施法速度只在 Buff 有效期内影响施法时间。"
      },
      "relevance": {
        "status": "relevant",                 // relevant|scenario_dependent|unconfirmed|not_relevant_current_scenario
        "path": "施放 Meteor → 获得 6 秒 Craterborn → 玩家火穿 +75% → 火焰本体与点燃的抗性乘区提高；+15% 施法速度 → 施法时间缩短、循环窗口调度变化（单次总伤害不变）",
        "branches": ["body.fire", "ignite", "cast", "loops"],
        "scenario_prerequisites": ["Buff 需要在 Black Hole 施放时处于有效期；固定条件模式假设维持"],
        "baseline_relation": "new",           // new|replaces|already_in_baseline|action_not_supported
        "notes": "施法速度固定 15%，不能乘 5；重复施放不叠层。"
      },
      "support": {
        "level": "needs_rule_mapping",        // engine_supported|needs_rule_mapping|needs_action_or_time_simulation|mechanism_unresolved
        "engine_gap": "现有引擎只消费快照中已解析的装备/被动/技能节点贡献，没有跨技能 Buff 输入通道；施法速度按固定属性快照计算。",
        "next_probe": "把该 Buff 作为场景状态，生成 fire_penetration / cast_speed 修正贡献后重算。",
        "open_questions": ["点燃是否继承 Craterborn 的火穿（现有规则：点燃读取玩家火穿）"]
      },
      "research": {
        "status": "confirmed",                // confirmed|assumed|unresolved
        "confidence_note": "节点 stat 行与 buff 资产均给出 15%/15%/6s；不叠层由 buff maxStacks=1。",
        "open_questions": [{"question": "...", "status": "unconfirmed", "probe": "..."}]
      },
      "evidence": [{"kind":"game_data","detail":"...","locator":"...","snippet":"..."}]
    }
  ],

  // 已检查但未纳入配置区的来源/效果（“已检查但未纳入”入口的数据；按类别分组，条目较多）
  "checked_records": [
    {
      "record_id": "checked:skill:me27:3",
      "category": "other_skill",              // 同 effect.category + "scope"
      "source_label": "Meteor · Celestial Celerity · 3 点",
      "effect_summary": "+30% Meteor 施法速度（仅 Meteor）",
      "disposition": "excluded",              // candidate|excluded|baseline_included
      "reason": "作用对象是 Meteor 自身施法速度，不进入 Black Hole 施法路径。",
      "related_effect_id": null,
      "evidence": [...]
    }
  ],

  // 已检查范围的分母（发现器会与真实构筑计数交叉校验；不一致时 issue）
  "checked_scope_expectations": {
    "equipment_items": 11, "idols": 8, "blessings": 10, "idol_altar": 1,
    "passive_nodes": 19, "specializations": 5, "snapshot_contributions": 240,
    "excluded_sources": 14
  },

  // 预置场景（页面“预设”下拉；保存/恢复用；默认第一个）
  "presets": [
    {"preset_id": "build_base", "label": "构筑基础状态（默认）", "description": "只含构筑常驻；无临时 Buff、无目标异常、无预设层数。",
     "states": {}, "effects": {}, "single_cast": {}, "loop": {}}
  ]
}
```

### 5.1 registry 校验规则（loader 必须实现）

- 顶层必填键、`schema` 精确匹配、`effects[].effect_id` 唯一、`shared_states[].id` 唯一、`presets[].preset_id` 唯一。
- 枚举校验：category / source.kind / match.type / control.kind / activation.kind / relevance.status / baseline_relation / support.level / research.status / states.kind / states.source。
- `conditions` 中引用的 `state` 必须存在于 `shared_states`；`control.state_id`/`stack_state_id` 必须存在且 `source=="user"`。
- `modifiers[].branches` 必须是 `computation_modules` 的 id 子集；`value_ref` 必须存在于 `values`；`value` 计算：`per_point*points`（若有）优先，否则 `constant`；没有可解析值 → 校验错误。
- 每条 effect 必须有 ≥1 `evidence`，且 `relevance.path` 非空。
- 全部 effect 的 `modifier_id` 全局唯一。
- 解析后输出 `effect_actuals`：每个 value 的 `actual`（per_point × 实际点数 / constant / roll）、`unit`、`display`。

## 6. 发现（discovery）语义

`discover.py` 输入：`snapshot`（model.Snapshot）、`calc_input`（dict，可缺）、`build`（bd.json dict，可缺）、`catalog`（可缺）、`registry`。输出 **catalog** dict（见 §8.1）。

1. **检查对象（分母）从真实构筑枚举**：
   - items（29 实例 = 11 equipment + 8 idol + 10 blessing，含 idol_altar）：逐 mod 记录 147 条；按 registry/启发式判定 disposition。
   - skills specializations（5 棵树、每个已分配节点）：名称/点数/max_points；结合 catalog 节点 description/effects。
   - passive_tree nodes（19 个）。
   - snapshot contributions（240 行）：已有 included/condition_status/exclusion_reason。
   - snapshot excluded_sources（14 条）。
   - bd.json（若可用）：交叉核对 revision、specializations/passives 点数、物品实例；不一致写入 `data_gaps`/`issues`（不静默）。
2. **候选判定**（启发式，不许用“属性白名单”当唯一依据）：
   - 来源描述/stat 名/readable_text 命中条件线索（`while|when|if |per |per stack|against|on hit|chance to|buff|next |every \d|for \d+ seconds|active|duration|grants?` 等中英文线索，可在 registry 配置正则列表）；
   - catalog 有 `buff_param_candidate`/`buff_mod_config`/`config_mod`（授予 Buff）；
   - `condition_status != always` 的 contribution；
   - `noScalingPointThreshold`/`conditions`/`modConditions` 非空；
   - 转换/触发/消费/层数/flags（tags 含 Buff/Ailment/Curse/Trigger 等）。
3. **registry 精确匹配**：按 `match` 找到来源实例并计算 actual 值；`existing_contribution_ids` 由 `source_path`/`mod_id` 前缀/condition_code 匹配 snapshot contributions 得到；`baseline_relation` 校验：已计入的效果必须指向已有 contribution（`already_in_baseline`），否则 `new`。
4. **未匹配但命中线索** → candidate record（`relevance.status="unconfirmed"`、`support.level="mechanism_unresolved"`、`control.kind="none"`），页面进“待确认”。
5. **已检查排除**：没有命中线索且无伤害关联的（ward/护甲/移速/暴击/药水栏/召唤物等）→ `checked_records.disposition="excluded"`，理由优先复用快照 `exclusion_reason`，否则给简短机制理由。仓库/未装备物品、编织树等 scope 排除单独记录。
6. **数据缺口**：calc input 缺失/sha 不符、catalog 不可用、bd 不可用、节点 id 在 catalog 找不到、点数/roll 无法解析、condition 无法映射等 → `data_gaps[]`，并计入 `issues`（info/warning）。
7. 输出必须**确定性**（排序稳定；不含当前时间戳）。

## 7. 场景（scenario）schema：`conditional-scenario.json`

```jsonc
{
  "schema": "bh-conditional-scenario-v1",
  "mode": "fixed_conditions",
  "baseline_profile": "build_base",
  "preset_id": "custom",                  // 使用 registry preset 时为 preset_id，否则 custom
  "binding": {
    "build_id": "letools:toolnative-save",
    "revision_id": "rev-dbba378f6fe3d829",
    "build_content_sha256": "...",
    "calc_input_sha256": "...",
    "dataset_id": "phase1-dataset-003",
    "registry_id": "bh-conditional-effects-v1",
    "registry_sha256": "...",
    "ruleset_id": "bh-stage1-v4",
    "ruleset_sha256": "..."
  },
  "states": { "target.chilled": false, "target.frozen": false, "player.craterborn_active": false },
  "effects": {
    "meteor.craterborn": {"enabled": true},
    "idol.throne_of_ambition": {"enabled": true, "stacks": 10}
  },
  "single_cast": {"crescendoing_rage_applies": false, "preparation_ready": false},
  "loop": {"crescendoing_rage_enabled": false, "initial_eligible_cast_count": 0, "action_stream": "black_hole_only"}
}
```

- 默认（build_base）：所有 user states=false；所有 toggle=false；stacks=0；single_cast 两项=false；loop 两项=false/0。auto 状态不写入场景（由引擎派生）。
- 校验（返回 `errors[]`/`warnings[]`，每条 `{code,message,effect_id?,state_id?}`）：
  - 未知 effect/state、本构筑没有的来源、`enabled` 用于无控制效果、层数超 bounds、类型错误 → **error**；
  - binding 与当前输入不一致（旧预设）→ error `stale_preset`（提示需要复核，不静默套用）；
  - `scenario.schema` 不匹配 → error；
  - 选择但条件不满足只作为求值结果，不算校验错误；
  - unsupported/action 效果被 `enabled=true` → warning（记录但不假装支持）。
- 场景指纹：`sha256(canonical_json(scenario with sorted keys, excluding preset_id))`，供幂等/对照。
- `save_scenario` / `load_scenario`：UTF-8 JSON、sort_keys、稳定输出；加载后重校验。

## 8. 求值（evaluation）与消费接口

### 8.1 result["conditional"] 结构（由集成方写进 result.json；后端产出）

```jsonc
{
  "schema": "bh-conditional-context-v1",
  "status": "ready",                       // ready|degraded|unavailable
  "issues": [{"severity":"info","code":"...","message":"..."}],
  "registry": {"id": "...", "sha256": "...", "path": "tools/phase3/rules/conditional-effects-bh-stage1-v1.json", "schema": "..."},
  "input": {
    "build": {...}, "calc_input": {...}, "dataset": {...}, "fingerprints": {...},
    "files": [{"role":"calc_input","path":"...","sha256":"...","exists":true}]
  },
  "context": {                             // 供文档/页面“主技能依赖”展示
    "main_skill": {...}, "mode": {...}, "baseline_profile": {...},
    "computation_modules": [...], "shared_states": [...], "disclaimer": "..."
  },
  "catalog": {                             // §8.2
    "checked_scope": {...}, "candidates": [...], "effects": [...], "checked_records": [...], "data_gaps": [...]
  },
  "scenario": {...},                       // §7
  "evaluation": {                          // §8.3
    "summary": {...}, "effects": [...], "proposed_contributions": [...], "unsupported": [...],
    "validation": {"errors": [], "warnings": []},
    "scenario_fingerprint": "...", "baseline_fingerprint": "...",
    "changed_from_baseline": true, "dps_recomputed": false
  },
  "rules": {...},                          // §8.5 客户端求值规则
  "view": {...}                            // §9 页面展示数据（条件页只从 view.conditions 读）
}
```

- **失败降级**：registry 缺失/非法、calc input 缺失等不能让 `le_calc run` 崩溃；`status` 置 `degraded`/`unavailable`，`issues` 写原因，`view` 仍可渲染说明页。
- `result["conditional"]["view"]` 也直接挂到 `report_ui` 的 `view["conditions"]`（集成方）。**注意：view 必须 JSON 可序列化、确定性、无绝对本机路径**（可写相对路径）。

### 8.2 catalog 细化

```jsonc
"catalog": {
  "checked_scope": {
    "counts": {"items": 29, "item_mods": 147, "specializations": 5, "skill_nodes_allocated": 67,
                "passive_nodes": 19, "snapshot_contributions": 240, "excluded_sources": 14,
                "effect_instances": 18, "candidates": 26, "excluded_records": 300},
    "categories": [{"id":"equipment","label":"装备","examined":11,"mods":47,"candidates":3}],
    "source_instances": [
      {"instance_id":"equipment.ring2","label":"Unsated Rage（戒指 2）","category":"equipment","mods":10,
       "disposition":"candidate","reason":null,"candidate_effect_ids":["ring.crescendoing_rage"]}
    ],
    "excluded_sources": [{...snapshot excluded_sources...}]
  },
  "effects": [                              // 已注册效果实例（页面配置区主数据来源）
    {"effect_id":"meteor.craterborn","effect": <registry effect 精简> , "actuals": {...},
     "qualification": {"available": true, "points": 5, "max_points": 5, "reason": null},
     "existing_handling": [{"contribution_id":"...","included":true,"condition_status":"unknown","exclusion_reason":null}],
     "source_id": null}                     // 若可映射到 report_ui 的来源，给 contribution_id（集成方会过滤有效性）
  ],
  "candidates": [                           // 所有候选（含已注册+未注册）
    {"candidate_id":"cand:skill:me27:25","effect_id":"meteor.craterborn","source_label":"Meteor · Craterborn · 5 点",
     "category":"other_skill","relevance_status":"relevant","condition_cues":["buff","duration"],
     "effect_summary":"+75% 火焰穿透、+15% 施法速度、6 秒 Craterborn Buff",
     "support_level":"needs_rule_mapping","disposition":"registered"}
  ],
  "checked_records": [...],                 // 已检查未纳入/自动计入
  "data_gaps": [{"id":"gap:...","severity":"warning","area":"...","message":"...","probe":"..."}]
}
```

### 8.3 evaluation 细化

```jsonc
"evaluation": {
  "summary": {
    "checked_objects": 462, "candidates": 26, "registered": 18,
    "relevant": 12, "scenario_dependent": 4, "unconfirmed": 2, "not_relevant": 8,
    "configured": 4, "condition_met": 2, "condition_unmet": 1, "not_selected": ...,
    "auto_active": 2, "auto_inactive": 1, "auto_unknown": 1,
    "engine_supported": 0, "needs_rule_mapping": 12, "needs_time": 2, "mechanism_unresolved": 4
  },
  "effects": [
    {
      "effect_id": "meteor.craterborn",
      "selected": true, "active": false,
      "status": "selected_ready_needs_rules",   // §8.4 枚举
      "status_reason": "条件已满足，但现有引擎没有 Buff 输入通道；已生成拟议贡献。",
      "qualification": {...},
      "condition_results": [{"kind":"state","state_id":"player.craterborn_active","label":"...","expected":true,"actual":true,"met":true}],
      "modifier_results": [{"modifier_id":"...","branches":["body.fire","ignite"],"stat_key":"penetration","operation":"added",
                             "value":0.75,"unit":"fraction","engine_sink":"...","dedupe_contribution_ids":[]}],
      "auto_parts": [],                       // 自动生效部分（如 Eternal Flames 被动）: [{"label":"被动 +25% 点燃持续","value":0.25,"unit":"fraction","condition":"..."}]
      "blocked": {"support":"needs_rule_mapping","reason":"...","next_probe":"...","open_questions":[...]},
      "already_in_baseline": false
    }
  ],
  "proposed_contributions": [                // 后续重算可直接消费（本轮不应用、不影响 result 伤害）
    {"proposal_id":"prop:meteor.craterborn:fire_pen","effect_id":"meteor.craterborn","modifier_id":"...",
     "stat_key":"penetration","stat_name":"火焰穿透","operation":"added","value":0.75,"unit":"fraction",
     "target":"fire","damage_tags":["Fire"],"branches":["body.fire","ignite"],
     "condition":"player.craterborn_active","engine_sink":"...","requires":"needs_rule_mapping",
     "dedupe_contribution_ids":[],"status":"proposed","evidence":[...]}
  ],
  "unsupported": [{"effect_id":"...","support":"...","reason":"..."}],
  "validation": {"errors": [], "warnings": []},
  "scenario_fingerprint": "...", "baseline_fingerprint": "...",
  "changed_from_baseline": false, "dps_recomputed": false
}
```

### 8.4 求值状态枚举（Python 与客户端 JS 必须一致）

优先级自上而下（先匹配先返回）：

1. `not_available` — 来源资格不满足（未装备/未分配/点数不足）。来源不存在时不出现在当前构筑清单，但校验旧预设会报错。
2. `unsupported_action` — `activation.kind=="action"` 或 `support.level=="needs_action_or_time_simulation"` 且不是 toggle/stacks（动作机制不在固定条件模式支持）。**即使 selected 也返回该状态**（selected=true 时提示“已选择，但动作机制未支持”）。
3. `auto_active` / `auto_inactive` / `auto_unknown` — `control.kind=="none"`（自动判定）：条件全满足→auto_active；有明确 false→auto_inactive；含未知 auto 状态→auto_unknown。
4. `not_selected` — 未选择（toggle false / stacks=0 / cast_state false）。
5. `selected_condition_unmet` — 已选择但条件未满足（列出未满足项）。
6. `selected_ready_needs_rules` — 已选择、条件满足、`support.level=="needs_rule_mapping"`。
7. `selected_ready_needs_time` — 已选择、条件满足、`support.level=="needs_action_or_time_simulation"`（用于非 action 但需要时间/动作的）。
8. `selected_unconfirmed` — 已选择、条件满足、`support.level=="mechanism_unresolved"`。
9. `active_supported` — 已选择、条件满足、`support.level=="engine_supported"`（本轮预计无，保留）。
10. `already_in_baseline` — `relevance.baseline_relation=="already_in_baseline"` 优先于 3/4（自动显示“已计入基础”，附条件是否满足）。

状态中文标签：not_available=构筑无此来源 / unsupported_action=动作机制未支持 / auto_active=自动生效 / auto_inactive=自动未生效 / auto_unknown=自动判定待确认 / not_selected=未选择 / selected_condition_unmet=已选择·条件不足 / selected_ready_needs_rules=已选择·条件满足·缺规则映射 / selected_ready_needs_time=已选择·条件满足·需时间/动作模拟 / selected_unconfirmed=已选择·机制待确认 / active_supported=已选择·已接入计算 / already_in_baseline=已计入基础。

### 8.5 客户端求值规则 payload（`result["conditional"]["rules"]`）

页面在离线状态下用它即时更新“选择/条件/支持”状态与计数（不碰伤害数值）。结构由 backend 生成，**必须只含声明式数据**（无函数/表达式字符串）：

```jsonc
{
  "schema": "bh-conditional-rules-v1",
  "states": [{"id":"...","label":"...","kind":"bool","source":"user","default":false,"bounds":null},
             {"id":"...","label":"...","kind":"int","source":"user","default":0,"bounds":{"min":0,"max":20}}],
  "auto_states": [{"id":"auto.max_mana","label":"最大法力","kind":"number","value":219.9,"display":"219.9",
                   "comparisons":[{"state":"auto.max_mana_gte_300","label":"最大法力 ≥ 300","op":">=","ref":"auto.max_mana","threshold":300,"met":false}]}],
  "effects": [{"effect_id":"...","name":"...","category":"...","group_id":"...",
               "control":{"kind":"toggle","state_id":"..."} | {"kind":"stacks","stack_state_id":"...","bounds":{...}},
               "condition":{"all":[{"state":"...","equals":true}],"any":[]},
               "support":"needs_rule_mapping","baseline_relation":"new",
               "status_when_met":"selected_ready_needs_rules",
               "already_in_baseline":false,
               "summary":"..." }],
  "status_labels": {"not_selected":"未选择", ...},
  "presets": [...],
  "baseline_scenario": {...}
}
```

页面 JS：从内嵌 `view-data`（或 `result-data`）读取 rules + 初始 scenario；操作控件→更新本地 scenario→按 §8.4 优先级重算状态/计数→更新 `[data-cond-status-for]`、`[data-cond-count]`、`[data-cond-changed]`；导出按钮生成 scenario JSON 下载；导入按钮读取 JSON 并做基础校验（schema/binding/未知 id/范围）后应用；增加“当前配置与基线不同，本页不重算 DPS”提示。**不实现第二套伤害计算。**

## 9. 页面展示数据：`result["conditional"]["view"]`（UI 契约）

```jsonc
{
  "schema": "bh-conditional-view-v1",
  "header": {"title":"条件 Buff 配置", "question":"哪些条件效果可能改变 Black Hole？如何设置？",
             "mode_label":"固定条件估算", "baseline_label":"构筑基础状态",
             "disclaimer":"...", "dps_recomputed": false,
             "dps_note":"本页只生成场景与求值说明，不重算 DPS；DPS 页仍为生成基线。"},
  "counts": {"checked_objects":462, "candidates":26, "configured":4, "relevant":12, "unconfirmed":2,
             "auto_active":2, "auto_inactive":1, "needs_rules":12, "needs_time":2, "unsupported":3},
  "filters": {
    "categories":[{"id":"all","label":"全部"},{"id":"other_skill","label":"其他技能"},...],
    "relevance":[{"id":"all","label":"全部"},{"id":"relevant","label":"相关"},...],
    "support":[{"id":"all","label":"全部"},{"id":"engine_supported","label":"已接入"},...],
    "statuses":[{"id":"all","label":"全部"},...]
  },
  "states": [
    {"id":"target.chilled","label":"目标处于冰缓","kind":"bool","source":"user","value":false,
     "description":"...","affects_labels":["Frozen Steel","Chilled Touch"],"editable":true},
    {"id":"auto.max_mana","label":"最大法力","kind":"number","source":"auto","value":219.9,
     "display":"219.9","editable":false,"note":"来自当前输入派生；不满足 300 门槛"}
  ],
  "presets": [{"id":"build_base","label":"...","description":"...","is_default":true}],
  "effects": [
    {"effect_id":"meteor.craterborn","group_id":"...","name":"Craterborn","name_en":"Craterborn",
     "category":"other_skill","category_label":"其他技能",
     "source_label":"Meteor · Craterborn · 5 点",
     "source_detail":{"kind":"skill_node","tree":"Meteor","node_id":25,"points":5,"max_points":5,
                      "slot":null,"item":"...","locator":"..."},
     "summary":"+75% 火焰穿透 · +15% 施法速度 · 6 秒 Buff",
     "value_lines":[{"label":"火焰穿透","value_text":"+75%","detail":"每点 +15% × 5 点"}, ...],
     "condition_text":"需要 Craterborn 有效（施放 Meteor 后 6 秒）",
     "impact_path":"施放 Meteor → Craterborn 6s → 玩家火穿 +75% → 火焰本体/点燃抗性乘区；+15% 施法速度 → 施法时间/循环",
     "branches":["body.fire","ignite","cast","loops"],
     "relevance":{"status":"relevant","label":"相关","tones":"ok"},
     "support":{"level":"needs_rule_mapping","label":"需要规则映射","tone":"warn","gap":"...","next_probe":"..."},
     "control":{"kind":"toggle","state_id":"player.craterborn_active","label":"...","help":"..."},
     "status":{"initial":"not_selected","label":"未选择","tone":"muted","reason":"..."},
     "condition_checks":[{"label":"Craterborn 有效","state_id":"player.craterborn_active","expected":true,
                          "met":false,"source":"user"}],
     "activation_text":"施放 Meteor 后获得；6 秒；不叠层；重复施放刷新",
     "baseline_note":null,             // already_in_baseline 时说明
     "dedupe_note":"无重复计入；现有来源账未包含此项",
     "evidence":[{"kind":"game_data","title":"游戏数据","detail":"...","locator":"...","snippet":"..."}],
     "advanced":{"effect_id":"...","modifier_ids":[...],"engine_sink":[...],"research_status":"confirmed","open_questions":[...]}}
  ],
  "groups": [{"id":"meteor.craterborn","label":"Meteor · Craterborn","effect_ids":["meteor.craterborn"]}],
  "auto_notes": [{"id":"auto.max_mana_gte_300","label":"神像火伤翻倍（最大法力 ≥ 300）","met":false,
                  "explain":"当前最大法力 219.9 < 300；基础神像火伤已计入，翻倍部分未计入。"}],
  "checked": {
    "summary": [{"category":"equipment","label":"装备","examined":11,"candidates":3,"excluded":8,"baseline":0}],
    "records": [{"record_id":"...","category":"equipment","source_label":"...","effect_summary":"...",
                 "disposition":"excluded","disposition_label":"已检查未纳入","reason":"...","relevance":"not_relevant_current_scenario"}],
    "excluded_sources": [...],
    "data_gaps": [...]
  },
  "dependency": {"modules":[...], "note":"..."},
  "export": {"scenario_filename":"bh-conditional-scenario.json", "command_hint":"..."},
  "client_rules": {...}                // 即 §8.5
}
```

- 页面 **不得** 使用任何外部 URL；evidence locator 只作文本。
- 页面渲染必须用 `c.esc`/`units`/`c` 现有 helper；新增 CSS 类以 `cond-` 前缀。
- 页面读 `view.get("conditions") or {}`，字段缺失时安全降级（空状态页），不能抛异常。
- 页面必须覆盖：顶部说明/KPI、共享状态区、预设与导入导出、筛选工具栏、效果卡片区（含状态徽章、条件检查、影响路径、控件、mechanism/evidence `<details>`）、自动判定区、待确认/不支持/已检查未纳入入口（可用页内 tabs 或分组折叠）、依赖说明入口。

## 10. CLI 与产物（集成方使用，后端提供函数）

- `le_calc run` 新参数：`--conditional-registry PATH`、`--calc-input PATH`（默认取 snapshot.build_meta.calc_input.path，相对项目根解析）、`--build PATH`（可选 bd.json 交叉核对）、`--scenario PATH`（可选场景 JSON，逾期/非法时报错退出 2）、`--no-conditional`（跳过，默认生成）。
- `le_calc conditions` 新子命令：`--snapshot`、`--ruleset`、`--scenario PATH|preset:<preset_id>`、`--registry`、`--calc-input`、`--build`、`--out DIR`；输出仅条件工件 + 校验/求值 JSON（不需要 DPS 计算），退出码：0 ok / 2 校验错误或降级。
- `write_run` 额外写（仅当 `result.get("conditional")` 为 ready/degraded 时）：
  - `conditional-effects.json`：registry 指纹 + context + catalog（含检查分母/候选/排除/缺口）
  - `conditional-scenario.json`：本轮场景
  - `conditional-evaluation.json`：evaluation（含 validation/proposed_contributions）
  - 写入 run-manifest `files`；README 增加“条件 Buff 配置”文件与摘要一节。
- 官方复现命令在《baseline》文档中更新（加入 `--conditional-registry` 与新产物说明），但 DPS 数值口径不变。

## 11. 后端函数接口（后端 → 集成点）

新包 `le_calc/conditional/`，导出：

```python
# context.py
def build_context(
    snapshot,                       # model.Snapshot
    *, scenario: dict | None = None,          # result["scenario"]（缺省时用 ruleset 构造最小场景）
    ruleset=None,                              # Ruleset（可 None）
    ruleset_path: str | None = None,
    calc_input_path: str | Path | None = None, # 显式覆盖；None 时从 snapshot.build_meta 解析
    build_path: str | Path | None = None,      # bd.json，可选
    registry_path: str | Path | None = None,   # None => 默认 tools/phase3/rules/conditional-effects-bh-stage1-v1.json
    scenario_path: str | Path | None = None,   # 用户场景文件
    preset_id: str | None = None,              # 与 scenario_path 二选一
    repo_root: str | Path | None = None,       # 默认 Path.cwd()
) -> dict:                                     # 即 result["conditional"]，绝不抛异常（降级）
```

其它模块（backend 可自由拆分，但对外至少提供）：
- `load_registry(path) -> Registry`（含 sha256、校验、effects 索引）
- `default_scenario(registry, context) -> dict`
- `validate_scenario(scenario, registry, catalog) -> {"errors":[], "warnings":[]}`
- `evaluate_scenario(scenario, registry, catalog) -> evaluation dict`
- `build_view(context) -> view dict`
- `write_artifacts(context, out_dir) -> dict[str, path]`（供 `le_calc conditions`）
- `export_scenario(context, path)` / `load_scenario(path)`

**确定性**：同输入同场景重复调用输出完全一致（排序稳定、无时间戳/绝对路径）。

## 12. 测试要求（各自负责）

- backend：`tools/phase3/tests/test_conditional.py`（用真实 registry + 真实 snapshot/calc input：
  - registry 校验通过 & 指纹稳定；effects 数量与关键 effect 存在（Craterborn、Crescendoing、Ambition、Frozen Steel、Preparation、Eternal Flames、Chilled Touch、Infusion、idol 门槛、Supermassive、Weakened Reality）
  - discovery 覆盖分母（items 29/mods 147/specializations 5/passive 19/contributions 240 与真实数据一致；候选包含此前不在有效贡献列表的效果）
  - 相关性分类有依据（relevant/scenario_dependent/unconfirmed/not_relevant 均有记录；排除项有理由）
  - 场景默认值=build_base；保存/加载 roundtrip；连续求值幂等（无累加）
  - 校验：未知 effect、越界层数、旧 binding、error 信息可读
  - 求值状态：`not_selected`、`selected_condition_unmet`、`selected_ready_needs_rules`、`already_in_baseline`、`auto_inactive`（idol 门槛）等
  - proposed_contributions 不进入伤害：result 的伤害数值与不含 conditional 时一致（由维护者的集成测试或 backend 断言 context 不改 snapshot）
  - 降级：registry 路径不存在 → status degraded 且不抛异常
  - 确定性：同输入两次 build_context 的 JSON 相同
- ui：页面模块单测（自己放临时脚本即可，不写正式 unittest）；维护者会在 `test_html.py` 补页级验收。
- research：产出 JSON 必须能被 loader 校验（backend 完成后用 CLI/脚本校验，维护者最终把关）。

## 13. 交付/汇报

- 完成阶段任务后汇报：改了哪些文件、验证命令与结果、未决问题、需要维护者做的事。
- 不要碰别人的写入范围；不要覆盖他人生成的规范 run 产物。
- 历史协作交接、验收记录与交付文档属于作者本地材料，不随公开仓库分发。
- 页面截图/离线验证：用本地浏览器完成，截图与大文件不入库。
