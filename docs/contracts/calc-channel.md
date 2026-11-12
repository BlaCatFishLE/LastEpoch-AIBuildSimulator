# 跨技能 Buff 输入通道（条件重算）实现契约 v1（2026-11-06）

> 本契约于 2026-11-07 从临时工作区提升为正式文档；作者本地的旧路径与迁移记录不随公开仓库分发。
> 本文中的 `<archive>` 指作者本地归档根（不随公开仓库分发；引用形式见 `docs/evidence/` 的 `archive://` 约定）。

> 目标：让条件 Buff 页选择的机制真正进入 Black Hole 伤害计算链。**只新增“条件重算”能力**，
> 不修改既有 DPS 基线结果；旧报告生成路径（无场景/未选择）= 现有数值不变。
>
> 执行者：计算通道实现方 + 维护者集成。维护者负责 report.py / cli.py / 页面 / 文档。
> 参考契约：`docs/contracts/conditional-interface.md`（条件层）；作者本地交接记录不随公开仓库分发。

## 1. 文件范围（仅这些）

- 新建 `tools/phase3/src/le_calc/conditional/recompute.py`（+ 必要时在 `conditional/__init__.py` 导出）
- 新建 `tools/phase3/tests/test_conditional_recompute.py`
- `<archive>/calc-channel/**`（作者本地归档开发记录，不随公开仓库分发）

**不要**修改 report.py / cli.py / present.py / shell.py / report_ui / registry JSON / 官方 reports 产物。
如需 report.py 接口变更，写在汇报里由维护者/集成方处理。

## 2. 公开 API

```python
# le_calc/conditional/recompute.py
def recompute_scenario(
    snapshot,                      # model.Snapshot（不会修改）
    ruleset,                       # Ruleset
    *,
    scenario: dict,                 # 条件场景（conditional-scenario-v1）
    registry,                       # conditional.registry.Registry
    catalog: dict | None = None,    # build_catalog 输出（用于 effect metadata；可空）
    evaluation: dict | None = None, # 条件求值结果（可用 evaluate_scenario 现算）
    monster_level: int | None = None,  # 目标 DR；None => ruleset 默认（100）
    include_auto: bool = True,      # 是否把 auto_active 提案也计入（默认 true）
) -> dict
```

返回 `conditional-recompute-v1`：

```jsonc
{
  "schema": "bh-conditional-recompute-v1",
  "status": "ok",                      // ok | skipped | error
  "ruleset": {"id": "...", "sha256": "..."},
  "monster_level": 100,
  "baseline": {                        // 与现有 DPS 基线同口径，无条件下加成
    "single": {"single_cast_total": ..., "body_total": ..., "ignite_total": ...,
               "ignite_tail_damage": ..., "expected_total_stacks_applied": ...,
               "observation_end_s": ..., "cast_time_s": ...},
    "loops": [{"policy_id": "eager", "windows": [{"window_s": 120, "dps": ..., "total_damage": ...}],
               "cast_count": ..., "cycle_s": ...}, ...]
  },
  "scenario": { ...same shape... },
  "delta": {
    "single_cast_total": 123.4, "single_cast_total_pct": 0.0123,
    "body_total": ..., "ignite_total": ...,
    "loops": [{"policy_id": "eager", "window_s": 120, "dps": ..., "dps_pct": ...}, ...]
  },
  "condition_application": {
    "applied": [{"effect_id","modifier_id","stat_key","operation","value","unit","target",
                 "damage_tags","branches","condition","status","assumption_note"}],
    "skipped": [{"effect_id","reason"}],
    "target_conditions": {"target_is_boss_or_rare": true, "target_within_center_1m": true,
                          "both_stars_cover": true},
    "proposals_used": ["prop:..."],
    "auto_included": true
  },
  "warnings": ["..."],
  "assumptions": ["..."],
  "scenario_fingerprint": "...",       // 用 conditional.scenario.scenario_fingerprint
  "dps_recomputed": true
}
```

要求：
- **确定性**：同输入同输出（无时间戳/绝对路径）。
- 不修改 `snapshot` / `ruleset` / `scenario` 入参（deepcopy 后改）。
- 失败降级：缺少提案、未知 stat_key 等 → `status="skipped"` 或 warnings，不抛异常（除编程错误外）。
- 不写文件（由集成维护者负责）。

## 3. 计算方式

1. **基线**：用原始 snapshot + ruleset.scenario（按 monster_level 覆盖目标 DR，规则同 report.build_result：
   通过 `le_calc.monster_dr`/目标侧乘区？简化：直接用 ruleset 场景 + monster_level 生成与 report 相同的目标 DR；
   可调用 `report._dps_by_monster_level` 或复制既有逻辑；目标是“与 result.json 同口径”）。
   调用 `simulate.run_single_cast(snapshot, ruleset, scenario)` / `simulate.run_loop(...)` 得到 single/loops；
   单次 totals 从 `single["totals"]`；loop windows/totals 从 `loop["windows"]/["totals"]`。
2. **场景 snapshot**：`copy.deepcopy(snapshot)` 后追加 `model.Contribution`（source_kind=`conditional_buff`，
   included=True，condition_status=`satisfied`，evidence 记录来源 effect/modifier/registry）；
   contribution_id 稳定：`conditional:<effect_id>:<modifier_id>`。
3. **提案来源**（优先）：`evaluation["proposed_contributions"]`；若 `evaluation` 为 None，则用
   `evaluate_scenario(scenario, registry, catalog or {})` 现算。
4. **提案→Contribution 映射**（stat_key/operation/tags/target/group）：
   - 直接复用提案字段：`stat_key`、`operation`、`value`、`unit`、`target`、`damage_tags`、`branches`、`condition`。
   - **分支裁剪（重要）**：引擎按 `damage_tags` 判定作用分支，提案 tags 必须能表达 branches；若
     branches 只含 `body.fire`/`body.cold` 不含 `ignite` 而 tags 为空，则 tags 设为 `["Spell"]`
     （本体带 Spell、Ignite 不带）；仅 `ignite` 分支则 tags 设 `["Ailment"]`（或 ["DoT","Ailment"]）；
     `body+ignite` 保持空/原 tags。若无法表达 → warning + skip。
   - `more` 提案：`model.Contribution(... operation="more", group=f"conditional:{modifier_id}")`；
     `increased/added` 按原 operation。
   - `ailment_duration` 的 target 必须是 `ignite`（否则 warning/skip）。
   - `cooldown_recovery_speed` 用 `STAT_COOLDOWN_RECOVERY` 常量。
   - `ailment_chance_conversion`/`flag`/`conversion` 等未支持 stat_key → skip（Conflagrate、Apex）。
   - `requires == "needs_action_or_time_simulation"`：
     * 单次释放：只有 CastState 提案（Crescendoing/Preparation，场景 flags 已满足）可计入；
     * 循环：跳过并写入 skipped（需要逐施法相位模拟）。
   - `requires == "mechanism_unresolved"` → 跳过（Frozen Steel/Apex 等）。
   - dedupe：`dedupe_contribution_ids` 非空 → skip（避免与既有来源重复）。
   - `already_in_baseline` 不应出现在 proposals；若出现 → skip + warning。
   - `include_auto=False` 时丢弃 `status=="auto_active"` 来源的提案。
5. **目标条件 override**：把条件场景中的用户状态映射到 `scenario["target_conditions"]`：
   - `target.boss_or_rare` → `target_is_boss_or_rare`
   - `target.within_center_1m` → `target_within_center_1m`
   - `target.both_stars_cover` → `both_stars_cover`（引擎不消费 → warning，只在 assumptions 记录）
   必须在 `run_single_cast/run_loop` 之前生效（ruleset tree more 会按 scenario 条件过滤）。
6. **monster_level**：与 report.build_result 一致（默认取 ruleset target_side.monster_dr.default_level；
   输出目标 DR 系数并让 KPI 结算口径一致；可直接复用 `le_calc.monster_dr.describe`）。
7. 结果中的 baseline 应等于旧 `result.json` 的 single/loops（同一 monster_level 与 scenario），
   允许浮点误差 1e-9；测试用 reason 断言。

## 4. 各效果的支持矩阵（必须按注册表 modifiers 执行）

| effect_id | stat_key / operation | tags | target | 条件 |
|---|---|---|---|---|
| meteor.craterborn | penetration added 0.75；cast_speed increased 0.15 | Fire / [] | fire / null | toggle+state |
| flame_ward.infusion | damage increased 2.5 | Fire | null | toggle+state |
| frost_wall.chilled_touch | damage more 0.14 | [] | null | toggle + frost_wall_active + target.chilled |
| enchant_weapon.eternal_flames.passive | ailment_duration increased 0.25 | [] | ignite | auto（include_auto） |
| enchant_weapon.eternal_flames.active | ailment_duration increased 0.25 | [] | ignite | toggle+state |
| ring.crescendoing_rage | damage more 0.5 | [] | null | single cast flag；loop skip |
| idol.throne_of_ambition | damage more 0.02*stacks Fire；0.02*stacks Cold | Fire / Cold | null | stacks>0 |
| passive.preparation | damage increased 0.8 | Elemental,Spell | null | single cast flag；loop skip |
| idol_altar.layout_cdr | cooldown_recovery_speed increased 0.10 | [] | null | toggle/state（assumed） |
| passive.frozen_steel | skip（mechanism_unresolved） | | | |
| enchant_weapon.conflagrate | skip（action） | | | |
| equipment.apex_glyph_conversion | skip（mechanism_unresolved） | | | |
| main_skill.supermassive / weakened_reality | 不注入（已由 target_conditions override 生效） | | | |
| idol.fire_damage_mana_threshold* | 不注入（快照已含/自动） | | | |

其余提案按第 3 节通用规则处理；遇到未知/无法映射的写入 skipped + warning。

## 5. 测试（`test_conditional_recompute.py`）

1. **基线等价**：`include_auto=False` + 默认场景（无选择）→ baseline == scenario totals（与旧 result.json 同口径）。
2. **单效果方向**（用真实 snapshot/registry）：
   - Craterborn selected → body 与 ignite 都上升（火穿）；cast_time 下降。
   - Infusion selected → body/ignite 上升，cold 不变。
   - Eternal Flames active → ignite total/尾 上升（duration）。
   - Ambition 20 stacks → body fire 上升；cold 上升。
   - Chilled Touch（target.chilled + frost wall active）→ body 上升；ignite 是否随 tags [] 上升——按实现固定并断言。
   - Crescendoing 单次 flag → single 上升，loop 不因它上升（skipped）。
   - Preparation 单次 flag → body 上升、ignite 不变（tags Spell）。
   - layout CDR → cast/loop dps 变化（或至少 cooldown 行被使用）。
3. **不支持**：Frozen Steel/Conflagrate/Apex 选中 → skipped 且 DPS 不变。
4. **目标条件 override**：center=false → Weakened Reality 的 more 不再计入（body 下降）；boss=false → Supermassive 不计入；
   both_stars=false → warning（引擎不可建模）。
5. **无重复**：同一场景求值两次结果一致；`dedupe_contribution_ids` 非空提案被 skip；
   对 baseline snapshot 深比较不变。
6. **降级**：registry 缺失/无法 evaluate → status skipped/error 带原因，不抛异常。
7. **确定性**：两次调用 JSON（sort_keys）一致。
8. 运行：`$env:PYTHONPATH='tools\phase3\src'; & .\.venv\Scripts\python.exe -m unittest tools.phase3.tests.test_conditional_recompute -v`
   并确保 `discover -s tools/phase3/tests` 全绿（测试数量以当前代码为准）。

## 6. 汇报

完成后向维护者汇报：文件、API、支持矩阵实现情况、测试命令/数量、与旧 result 的基线等价验证、已知假设/未支持项，以及
需要集成方完成的部分（attach_conditional 调用、CLI、页面、artifact）。若遇到 report.py 必须改的点，先与维护者沟通。
