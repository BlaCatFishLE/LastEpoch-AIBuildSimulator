# Flame Reave hit 模型 — 条件 Buff 敏感性（本轮实测，Lead）

> **公开版说明（2026-11-13）**：本文件是作者本地研究记录，保留当时记录的路径与数值作为历史依据；文中 `archive://examples/…`、`archive://reports/…`、`archive://docs/history/…` 与已从公开版移除的历史规则包均为作者本地归档（统一以 `archive://` 前缀标记），不随公开仓库分发。可复用结论与其来源定位见 [docs/evidence/extracted/](../extracted/README.zh-CN.md)。

日期：2026-11-07。口径：`archive://examples/flame-reave/snapshot/input-snapshot.hit.json` + `archive://tools/phase3/rules/ruleset-fr-hit-stage1-v1.json` + `archive://examples/flame-reave/scenario/conditional-scenario.json`；目标 Boss/稀有、怪物等级 100（DR 87%）、火抗 0；单次命中含 3 击权重 [1.0,0.5,0.5]、Rhythm of Fire 循环平均 ×1.325；冷却受限 DPS。数值是**固定条件估算**，不是实战 DPS。

| 场景 | 单次命中（含 DR） | 相对基线 | DPS(5s) | 相对基线 | more 乘区 | crit chance |
|---|---:|---:|---:|---:|---:|---:|
| baseline（build_base；EW 常驻 + Army high-health + Rhythm 平均） | 28,418.83 | +0.0% | 53,218.99 | +0.0% | ×6.9296 | 21.06% |
| legacy_single_skill（无 EW/Army 条件） | 10,625.16 | -62.6% | 19,897.39 | -62.6% | ×3.1005 | 21.06% |
| 目标 ignited（Scorching Tide +60% more，Chaos Flare crit×1.2） | 49,482.98 | +74.1% | 92,665.12 | +74.1% | ×11.0873 | 25.27% |
| Frenzy active（+45.6% inc AS；冷却约束下 DPS 不变） | 29,980.03 | +5.5% | 56,142.61 | +5.5% | ×6.9296 | 21.06% |
| Ambition 20 层（+40% more Fire） | 39,786.36 | +40.0% | 74,506.58 | +40.0% | ×9.7014 | 21.06% |
| Firebrand 消耗 9 层（Incineration more + crit） | 189,141.18 | +565.5% | 354,198.39 | +565.5% | ×16.2845 | 84.24% |
| Blade Weaving 消耗 8 层（+120% more Elemental） | 62,521.42 | +120.0% | 117,081.78 | +120.0% | ×15.2450 | 21.06% |
| Rhythm of Fire 消耗单次（+130% more，单次命中口径） | 65,363.30 | +130.0% | 122,403.68 | +130.0% | ×15.9380 | 21.06% |
| sustained burst（ignited + Frenzy + Ambition20 + Firebrand9 + BladeWeaving8） | 1,130,958.96 | +3879.6% | 2,117,909.14 | +3879.6% | ×80.2498 | 100.00% |

说明：
- 用户裁决（2026-11-07）：双持 added 直接相加、攻速基础公式正确、无类型 added 自适应→Fire；这些只改状态/口径，不改变本表数值（基线仍为 build_base）。
- 基线 = `build_base` profile：Enchant Weapon 近战常驻（assumed）、Army of Skin 对高血量目标 49% more（assumed）、Rhythm of Fire 纯 FR 循环平均 ×1.325（assumed）。
- 每一行都是**单独切换一个状态**（最后一行是组合），不是相互叠加后的精确总量；冷却/攻速/资源/其它技能补层未做时间模拟。
- `Frenzy active` 只提高攻击速度：Reflash 冷却（有效 1.415s）是主约束，所以 DPS 不变；若改用 attack-speed-limited 口径才会体现。
- 这些状态进入 `buff-screening.json` 的候选清单（modeled/unresolved/not_modeled）与 `next_probe`，不是已确认机制。

数据来源：本轮实际运行（`le_calc hit-run` 同一引擎函数），脚本见 `<author-archive>/flame-reave-recon/buff_sensitivity.py`；规范 run：`archive://reports/flame-reave/save-build-fr-hit-stage1/`。
