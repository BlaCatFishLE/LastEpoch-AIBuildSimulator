# Tools: `save_import` — 离线存档 → BD 读取

把 Last Epoch 离线存档（`1CHARACTERSLOT_BETA_*` / `STASH_*`，`EPOCH` + JSON 容器）解析为
可读的构筑（BD）：角色/职业/专精、专精技能树、法师被动天赋、装备/神像/祝福、编织者树，
并可选与 Letools planner 的 `build-response.json` 做逐项对比。

本目录是**只读解析器**：不修改存档、不写游戏目录、不联网。

## 快速开始

```powershell
$py = '<repo>\.venv\Scripts\python.exe'
Set-Location '<repo>'
$env:PYTHONPATH = 'tools\save_import\src'

# 1) 概览
& $py -m le_save_import inspect --save '<game-save-dir>\1CHARACTERSLOT_BETA_2'

# 2) 完整提取（写 bd.json / 表格 / 证据文件；可选 --planner 对比）
& $py -m le_save_import build `
    --save '<game-save-dir>\1CHARACTERSLOT_BETA_2' `
    --out reports\<case>\save-import `
    --planner <planner-build-response.json> `
    --stash '<game-save-dir>\STASH_0'

# 3) 生成本地 BD HTML（使用修正后的 bd.json 覆盖 BuildSpec 展示值，保留人工神像折射面板）
& $py tools\save_import\scripts\render_bd_html.py

# 4) 只解码物品字节
& $py -m le_save_import decode --save <save> --out decoded-items.json
```

`build` 产物（由 `--out` 指定，例如 `reports/<case>/save-import/`）：

| 文件 | 内容 |
|---|---|
| `bd.json` | `save-build-v1`：完整构筑（角色/技能树/被动/装备/神像/祝福/编织者/任务/诅咒与 caveat） |
| `bd-tables.zh-CN.md` | 自动生成的中文表格（技能节点、被动、装备、神像、祝福…） |
| `save-build-view.html` | 可选：本地 BD HTML（`scripts/render_bd_html.py` 生成，包含装备/技能/神像 + 人工折射面板） |
| `decoded-items.json` | 43 个 `savedItems` 的逐字段解码（物品字节布局 v6） |
| `save-extract.json` | 非物品部分的结构化抽取（身份/树/HUD/物品分类/任务/进度） |
| `comparison-vs-planner.json` | 可选：与 planner payload 的逐项对比（match/partial/mismatch + 计数） |
| `decoded-stash.json` | 可选：仓库可堆叠列表（材料/ woven echoes / keys） |

## 目录

```text
tools/save_import/
  src/le_save_import/
    items.py      ItemData v6 字节解码（+ encode_item 精确逆运算；游戏二进制布局 + 生成器语义双读法）
    extract.py    非物品事实抽取（EPOCH 容器、树、HUD、任务、进度）
    names.py      数字 id -> 名称/效果 resolver（13 张 mapping 表；含 letools planner id codec）
    stash.py      仓库可堆叠列表解码（materials / woven echoes / keys）
    pipeline.py   save -> resolved build（save-build-v1）+ planner 对比 + 输出
    affix_ranges.py 读取 affix-db，按 baseType.affixEffectModifier 计算每位置有效 roll 范围
    cli.py        inspect / build / decode / stash
  data/mappings/  由 scripts/build_mappings.py 生成 + 来源 manifest（phase-1 dataset + MIT 表）
  scripts/        映射生成脚本 + `render_bd_html.py`（bd.json → 本地 BD HTML）
  tests/           21 个 unittest + fixtures（本机存档副本 + planner payload）
  README.md
```

## 数据来源与证据

- **存档格式**：`EPOCH`(5 字节 ASCII) + UTF-8 JSON（Mongo 风格文档）。逐字段布局、容器枚举、
  特殊词缀位、任务 `state=0` 序列化器行为等见
  [docs/save-import.zh-CN.md](../../docs/save-import.zh-CN.md)。
- **物品字节布局**：由本机游戏（Unity 6000 / IL2CPP 1.5.3）的 Il2CppDumper `dump.cs` 字段偏移 +
  Cpp2IL ISIL 反汇编 `ItemData.setValuesFromSerialisation`/`RebuildID` 恢复；
  并与两个 MIT 开源解析器交叉验证：
  [IgorBabayan/le-save-editor @6a5bb46](https://github.com/IgorBabayan/le-save-editor) 与
  [szefo94/last-epoch-save-editor @793ea58](https://github.com/szefo94/last-epoch-save-editor)。
- **名称/效果表**：项目 phase-1 dataset（`phase1-dataset-003`，游戏 1.5.2）+ szefo94 MIT
  数据表（bases/uniques/affixes）生成（`scripts/build_mappings.py`，见 `data/mappings/mapping-manifest.json`）。
- **词缀范围**：`data/affix-db/v1`（tunklab 1.5.2 规范化 DB，含每个 baseType 的
  `affixEffectModifier` 和游戏侧显示范围）。`build` 默认读取它，避免把全局 tier 范围误当成
  装备位置范围（例如 2H 剑火穿 35–40%、Body Armor 火抗 92–112%）。
- **生成器工具参考**：本机 `<game-install>\Editor\lastepochsupertools_v1.3.1`
  （用户提供的 save/build 工具，只读参考）。其 `planner/js/planner.js` 的
  `pvmBuildWireFromSave`/`pvmEncodeItem` 与本解码器相互印证；该工具写出的存档具有
  editor 特征（instance id 常量 0x7FFF、roll=255），解析时同时暴露"编辑器通道"与"游戏通道"。

## 限制 / 不做的事

- 只解析本工具覆盖的**离线**存档；在线（云存档/账号 API）不在范围。
- 不计算 DPS / 不执行游戏公式（那是 phase-3 `le_calc` 的范围）。
- **词缀 roll 范围按装备位置解析**：`build` 默认读取 `data/affix-db/v1/normalized/affix-db.sqlite`，
  用 `baseType.affixEffectModifier` 把 phase-1 的全局 tier 范围换算成该基底的**有效范围**。
  例：Fire Resistance T7 在 Body Armor 上是 92–112%，在 Helmet 上仍是 61–75%；
  Two-Handed Sword 的 `Fire Penetration` T7 是 35–40%（满 roll 40%），不是全局 19–22%。
  可用 `--affix-db <path>` 指定其他 DB；DB 不存在时退回旧全局范围，并在 `provenance.affix_db.status` 标记 unavailable。
- 存档为编辑器生成时，编辑器写入物品的 roll 字节为 `255`：**255 = 满 roll，按该位置有效范围上界（display_max）**；
  `roll∈[0,254]` 在有效范围 `[effective_min,effective_max]` 上线性映射。这是项目约定，phase-2 导出与 phase-3 计算都按上界使用。
- 不臆造：任何无法在 mapping 表中命名的 id 以 `status: unresolved` 返回。

## 测试

```powershell
Set-Location tools\save_import
& $py -m unittest discover -s tests -v   # 21 tests OK
```
