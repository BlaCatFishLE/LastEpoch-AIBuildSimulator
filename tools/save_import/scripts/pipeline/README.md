# 存档导入正式链路脚本（tools/save_import/scripts/pipeline）

本目录是从 `.scratch/save-import/*.py` 提升出来的**正式运行链路**，由
`scripts/le.ps1 reproduce` 调用。旧脚本只保留为历史参考，不再是可编辑的正式入口。

四个步骤按顺序执行（每一步只读写显式参数，可从任意当前目录运行）：

| 脚本 | 步骤 | 输入 | 输出 |
|---|---|---|---|
| （`le_build export`） | 固定 BuildSpec revision → 计算输入 | 构筑 revision `build.json` | `calculation-input-v1` JSON（146 条 mod） |
| `run_affix_db_pipeline.py` | affix-db 每基底范围重算 + 折射神像 Altar 前后缀放大 | 上一步的计算输入、`--case` 指向的 affix DB、`corrections.json` 的 `refraction` 块 | 重算后的计算输入 + `save-build-affix-db-recompute-v1` 报告 |
| `apply_corrections.py` | 人工补值（任务属性、胸甲火抗、神像 corrupted 全抗） | 上一步输出、`corrections.json` | 修正后的计算输入 + `save-build-corrections-v1` 来源记录 |
| `make_snapshot.py` | 静态属性快照 | 修正后的计算输入、案例 `snapshot_expectations` | `input-snapshot.json`（`le_calc run --snapshot` 直接消费） |
| `gen_bd_html.py` | 玩家可读 BD HTML 视图 | 案例 `bd.json` + BuildSpec revision | `bd-view.html` |

`_common.py` 提供仓库根定位（`__file__` 向上查找 `config/project.json` + `data/current.json`，
可用 `LE_REPO_ROOT` 覆盖）、案例加载、确定性 JSON 写入与 sha256 工具。

## 设计约束

1. **无绝对路径**：全部从项目根解析；不依赖 shell 当前目录；不读取未声明的外部路径。
2. **无样例常量**：折射神像索引、Altar 倍率、人工补值全部来自
   `examples/<case>/inputs/corrections.json`；代码里只保留通用词缀类别规则。
3. **确定性**：不写墙钟时间；JSON 使用固定缩进与插入顺序，重复运行字节一致。
4. **1.5.3 游戏侧取整**：affix-db 显示值沿用游戏 half-even（banker's）取整，不得改成其他舍入方式。
5. **不改写既有产物**：输出必须由调用方指定；不覆盖案例输入；重跑使用新 run 目录。
6. **校验不静默弱化**：`make_snapshot.py` 先按案例声明计数校验文件，再临时固定
   `le_calc.inputs.EXPECTED_*`（打印固定值，`finally` 恢复）。该运行时改模块常量是已知限制，
   详见本文件末尾的已知限制说明。

## 单独调用示例

```powershell
& .\.venv\Scripts\python.exe tools\save_import\scripts\pipeline\run_affix_db_pipeline.py `
    --case examples\<case>\case.json `
    --calc-input <calculation-input.json> --out <calculation-input.affix-db.json> --report <recompute.json>

& .\.venv\Scripts\python.exe tools\save_import\scripts\pipeline\apply_corrections.py `
    --case examples\<case>\case.json --input <calculation-input.affix-db.json> --out <corrected.json>

& .\.venv\Scripts\python.exe tools\save_import\scripts\pipeline\make_snapshot.py `
    --case examples\<case>\case.json --calc-input <corrected.json> --out <snapshot.json>

& .\.venv\Scripts\python.exe tools\save_import\scripts\pipeline\gen_bd_html.py `
    --case examples\<case>\case.json --out <bd-view.html>
```
