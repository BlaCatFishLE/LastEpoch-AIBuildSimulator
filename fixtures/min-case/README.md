# fixtures/min-case — 合成最小验证输入（不依赖真实构筑）

本目录提供一个**合成**（synthetic）输入快照，用于在干净 clone、没有真实存档 /
构筑 / dataset 的情况下验证 phase-3 计算引擎与离线 HTML 报告链路仍然可用。

- `input-snapshot.json`：由测试夹具 `tools/phase3/tests/support.py` 的
  `make_snapshot()` 生成（数值为测试用占位，**不是**任何真实构筑结果，也不代表
  游戏机制结论）；`build_meta.build_id` 已改为 `letools:min-case`。
- 不含任何真实存档、构筑 revision、历史 run 或 dataset 数据库；它不是可运行案例
  （`case.json`），不能用于 `le.ps1 run --case`。带案例的完整流程需要用户自己的
  存档 / planner payload（见 `examples/_template/README.md`）。

## 演示一次计算 + 离线 HTML（无需 dataset / 真实构筑）

在仓库根目录执行：

```powershell
$env:PYTHONPATH = 'tools\phase3\src'
# 1) 结构自检（允许 synthetic 计算输入缺失的 warning/issue）
& .\.venv\Scripts\python.exe -m le_calc check --snapshot fixtures\min-case\input-snapshot.json

# 2) 运行一次 Black Hole 引擎 + 生成离线报告（输出写本地产物目录 reports/，已 gitignore）
& .\.venv\Scripts\python.exe -m le_calc run `
    --snapshot fixtures\min-case\input-snapshot.json `
    --out reports\min-case-demo --run-id min-case-demo
```

预期产物（`reports/min-case-demo/`）：

| 文件 | 说明 |
|---|---|
| `result.json` | `black-hole-stage1-result-v1` 结果（合成数值） |
| `black-hole-damage.html` | 离线单文件 HTML 报告（共享渲染器） |
| `source-ledger.json` / `rule-ledger.json` | 来源账 / 规则账 |
| `conditional-*.json` | 条件 Buff 评估与重算工件（synthetic 计算输入缺失时记录为显式 issue） |
| `run-manifest.json` | 运行清单 |

说明：

- 因为快照没有真实 `calculation-input` 文件，条件重算会记录
  `计算输入缺失或不存在：synthetic` 的显式 issue，这是预期行为，不是链路失败。
- 这条路径验证的是“引擎 + HTML 渲染”，不是 `le.ps1 run --case` 的完整报告白名单；
  `report-capabilities.json` 的生成由 `tools/phase3/tests/test_report_capabilities.py`
  的最小回归覆盖（仅引用实际存在的契约文件）。

## 没有真实资产时的替代验证命令

```powershell
.\scripts\le.ps1 doctor
.\scripts\le.ps1 test --suite phase3
.\scripts\le.ps1 test --suite build_import
.\scripts\le.ps1 test --suite save_import
.\scripts\le.ps1 test --suite affix_db
.\scripts\le.ps1 test --suite knowledge
```

公开版不含作者本地冻结案例/报告/构筑，相关验收测试模块会以
`unittest.SkipTest` 明确跳过（见 `tools/phase3/tests/_author_assets.py`）。
