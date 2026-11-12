# tools/build_import — 第二阶段 BD 导入器

把 Last Epoch Tools 的分享构筑转换为**可离线读取、可追溯到固定目录数据集**的 BuildSpec v1，
并输出导入报告与计算缺口报告。计划与验收标准见
导入契约见 [`docs/contracts/save-onboarding.md`](../../docs/contracts/save-onboarding.md)，通用操作流程见
[`docs/workflow.md`](../../docs/workflow.md)。

网络访问只发生在 `src/le_build/sources/letools.py`；`catalog/resolve/builder/rolls/validate/report/store`
全部可离线运行。

## 快速开始

```powershell
$py = '<repo>\.venv\Scripts\python.exe'   # 见仓库根 README 的 .venv 创建步骤
Set-Location '<repo>'   # 仓库根目录
$env:PYTHONPATH = 'tools\build_import\src'

# 真实链接导入（使用 data/current.json 选定的数据集）
& $py -m le_build import --url https://www.lastepochtools.com/planner/o3ZRm9my --dataset phase1-dataset-002

# 已保存原始响应的离线重放（同内容 → 同一 revision，幂等）
& $py -m le_build import --file <planner-build-response.json> --share-id <share-id> --offline

# 查询 / 追溯 / 离线校验 / 单文件 HTML 可视化
& $py -m le_build show     --build letools:o3ZRm9my
& $py -m le_build explain  --build letools:o3ZRm9my --path equipment.hands
& $py -m le_build validate --build letools:o3ZRm9my --offline --json
& $py -m le_build view     --build letools:<share-id> --out reports\<case>\build-view.html
```

> `letools:toolnative-save`（本地离线存档导入）的 view 产物额外包含「神像折射栏位（人工勾选）」面板：
> 勾选处于紫色折射栏位的神像后，按 Idol Altar 的 prefix/suffix effect 实时放大其前后缀，并显示
> 未封顶火抗；选择可导出为 JSON。自动判断依赖存档坐标 + 网格数据，当前不可靠，故保留人工确认。

退出码：`0` 成功；`1` 失败（fetch/schema/版本不符/存储错误）；`2` 参数错误；
`3` 已发布但存在未解析项或校验告警（partial，不得报告为完整成功）。

## 目录

```text
src/le_build/
  cli.py                 命令行入口（import/show/explain/validate/view）
  paths.py               repo 根、构筑目录布局、规范化 JSON 哈希
  catalog.py             只读目录数据集访问层（指纹校验 + source_id_map 命名空间查询）
  sources/letools.py     分享 URL 校验、HTTP 获取/归档、离线重放、版本识别
  codecs/letools_ids.py  LE Tools A/U/I 编码（marker + LZ-String base64 变体）
  resolve.py             来源引用 → catalog 实体（resolved/unresolved/ambiguous，不猜）
  builder.py             BuildDraft → BuildSpec v1（结构、来源路径、问题记录）
  rolls.py               版本化数值恢复（rrl7o/rrlZm 规则、缺省 roll 策略）
  validate.py            离线校验、分类覆盖率、explain、schema 子集校验
  report.py              import-report / calculation-gap-report（JSON + 中文 Markdown）
  store.py               不可变 revision 发布、幂等、原子 current 指针、失败不动旧版本
  viewer.py              单文件离线 HTML 可视化（`le_build view`）
schemas/build-spec-v1.json  BuildSpec v1 结构定义
tests/                   单元/集成/负向回归（unittest，标准库）
```

## 数据布局

```text
data/builds/letools/<shareId>/
  raw/<response-sha256>.json          原样响应
  fetches/<attempt-id>.json           抓取尝试元数据（时间、状态、哈希）
  revisions/<revision-id>/
    build.json                        BuildSpec v1
    import-report.json / .zh-CN.md
    calculation-gap-report.json / .zh-CN.md
  current.json                        构筑 current 指针（原子替换）
```

同一内容（不含抓取时间/路径等易变元数据）重复导入得到同一 revision；内容变化生成新 revision。
发布先写临时目录、校验通过后原子改名，再替换 current 指针；失败不会破坏上一次成功版本。

## 测试

```powershell
Set-Location 'tools\build_import'
$env:PYTHONPATH = 'src'
& $py -m unittest discover -s tests -v
```

第一阶段（目录数据库）测试独立于本包：`tools/ingest`。
