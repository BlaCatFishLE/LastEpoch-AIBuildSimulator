# Flame Reave hit 模型证据（flame-reave 案例）

> **公开版说明（2026-11-13）**：本文件是作者本地研究记录，保留当时记录的路径与数值作为历史依据；文中 `archive://examples/…`、`archive://reports/…`、`archive://docs/history/…` 与已从公开版移除的历史规则包均为作者本地归档（统一以 `archive://` 前缀标记），不随公开仓库分发。可复用结论与其来源定位见 [docs/evidence/extracted/](../extracted/README.zh-CN.md)。

本目录是 hit 模型案例（`archive://examples/flame-reave/`，主技能 Flame Reave / `fr11mv`）的机制证据与实测记录。
权威数据仍是规则包与运行产物；这些文档只做索引与解释。

| 文件 | 内容 | 状态 |
|---|---|---|
| [`FR-MECHANICS.zh-CN.md`](FR-MECHANICS.zh-CN.md) | R1–R8 机制研究：命中次数/双持/攻速/Enchant Weapon/Frenzy/Blade Weaving/Firebrand/Rhythm/ignite 来源/mana/伤害类型，逐条 locator + 未解决项 | 研究事实（`tools/phase3/rules/fr-hit-facts-v1.json`，45 facts） |
| [`BUFF-SENSITIVITY.zh-CN.md`](BUFF-SENSITIVITY.zh-CN.md) | 条件 Buff 单状态切换的定量敏感性（本轮实际运行同一引擎函数） | 实测（2026-11-07，Lead） |

相关入口：

- 规则包：`archive://tools/phase3/rules/ruleset-fr-hit-stage1-v1.json`（id `fr-hit-stage1-v1`）
- 研究事实：`tools/phase3/rules/fr-hit-facts-v1.json`
- 规范 run：`archive://reports/flame-reave/save-build-fr-hit-stage1/`（`result.json` / `rule-ledger.json` / `buff-screening.json` / 离线 HTML）
- 复现证据：`archive://reports/flame-reave/lead-reproduce-7/`（`le.ps1 reproduce`，25 项 PASS）
- 案例说明：`archive://examples/flame-reave/README.md`；支持范围/已知问题：`docs/current-status.md` §3.2/§5

> 结果状态是 `assumption`：规则包仍有 assumed/unresolved 项（双持合成口径、技能等级缩放、部分条件效果），
> 不得把本目录的数值/假设写成已确认机制。
