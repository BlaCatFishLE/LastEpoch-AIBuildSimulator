# 第三方声明与许可（THIRD_PARTY_NOTICES）

本文件说明本仓库（LastEpoch-BuildSimulator）包含的第三方内容、参考项目、数据来源与再分发边界。
项目本身以 [MIT License](LICENSE) 发布，**仅覆盖本项目原创代码与文档**；第三方内容、游戏数据与商标分别适用其自身权利与限制。

## 1. 随仓库分发的第三方内容

### 1.1 szefo94/last-epoch-save-editor 数据表（MIT）

以下数据表来自开源项目 [szefo94/last-epoch-save-editor](https://github.com/szefo94/last-epoch-save-editor)（commit `793ea58`），以 MIT 许可随本仓库分发（vendored）：

- `tools/save_import/scripts/source-data/szefo94/bases.json`
- `tools/save_import/scripts/source-data/szefo94/uniques.json`
- `tools/save_import/scripts/source-data/szefo94/affixes.json`

这些表用于物品基底/词缀/暗金的名称与描述文本，以及存档解码的交叉印证。其许可全文与版权行如下：

```text
MIT License

Copyright (c) 2026 szefo94

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### 1.2 生成物中的说明文本

`data/mappings/`、`data/affix-db/` 等生成物中可能包含来自上述 MIT 表或第三方站点的名称/描述文本；
生成物整体作为本项目的规范化结果分发，不改变其上游文本的权利归属。

## 2. 参考项目与致谢（未分发其代码或数据）

以下项目仅作为格式、字段与实现思路的**公开参考**或交叉核对对象，本仓库**不包含**其代码或数据：

- [IgorBabayan/le-save-editor](https://github.com/IgorBabayan/le-save-editor)：独立的 Last Epoch 存档编辑器/解析器实现。本项目在恢复物品字节布局（`ItemData` 序列化 v6）时参考其公开实现进行交叉验证，未复制其代码或数据。
- [Musholic/LastEpochPlanner](https://github.com/Musholic/LastEpochPlanner)、[uta666XYZ/LastEpochBuilding](https://github.com/uta666XYZ/LastEpochBuilding)：Last Epoch 构筑/规划相关公开项目。仅查阅公开说明以校准字段与覆盖范围，未拉取或复用其代码/数据；其技能与 Buff 支持范围不构成本项目的承诺上限。

如需使用上述项目，请遵循各自仓库的许可与声明。

## 3. 开发工具（未捆绑）

- **REA（Reverse-Engineering Agents）**：[morluto/rea](https://github.com/morluto/rea)，npm 包 `rea-agents`，MIT 许可。本项目在**开发与逆向研究阶段**使用其 CLI/MCP 能力分析游戏二进制与页面数据。
  REA 及其依赖（Node.js、Ghidra、JDK、IDA/Hopper 等）**不随本仓库分发**，不在普通导入/计算链路上；使用者需自行安装并遵守各自许可。
- **UnityPy** 等游戏资源读取库：仅逆向研究阶段按需使用，不随仓库捆绑，普通计算不需要。
- Python 运行面只使用 3.12 标准库；PowerShell 入口使用 Windows 自带 PowerShell 5.1+。

## 4. 数据来源与再分发边界

- 游戏数据集（`phase1-dataset-003`）的**规范化结果**由第三方粉丝站公开数据整理而来，主要来源：
  - [Last Epoch Tools](https://www.lastepochtools.com)
  - [Tunklab](https://lastepoch.tunklab.com)
  这些站点与 Eleventh Hour Games 无隶属关系；其数据/文本权利归各自权利人。
- 词缀库 `data/affix-db/v1/normalized/affix-db.sqlite` 是基于 Tunklab 自报 1.5.2 数据整理的**规范化结果**，随本仓库分发。
- 以下内容**不随仓库分发**，仅在作者本地保留或由使用者自行获取：
  - `data/affix-db/v1/source-snapshot/`（第三方站点原始抓取快照）；
  - `data/reference/`（第三方参考库）；
  - 游戏本体、`GameAssembly.dll`、`global-metadata.dat`、游戏资源包（`*.bundle` 等）；
  - 任何原始用户存档（`1CHARACTERSLOT_BETA_*`、`STASH_*`）——请使用你自己的离线存档副本；
  - 第三方站点的原始 JS/页面缓存（仅用于本地离线提取，不再分发）。
- 运行所需的大体积 dataset（`data.sqlite`，约 251 MB）通过 GitHub Release 附件分发，仓库正文不包含该文件；其 sha256 记录在 `data/current.json`。

## 5. 游戏与商标声明

Last Epoch 及相关名称、标识、游戏内容与数据版权归 **Eleventh Hour Games** 所有。
本项目是独立的社区工具，**与 Eleventh Hour Games 无隶属、赞助或背书关系**；
不包含、不修改、不重新分发游戏本体或未授权游戏资源。
游戏内数值与机制的权利归游戏权利人；本项目对游戏数据的整理与模拟仅用于个人研究与工具演示。

## 6. AI 生成声明

本项目的代码、数据整理、规则、文档与报告由 AI 协作生成或辅助生成，可能包含错误、过时信息或不完整结论。
所有内容按 **AS IS** 提供，不提供任何明示或暗示的担保；引用或依赖前请自行复核来源与假设。
详见 [README.md](README.md) 的“AI 生成声明”与“已知限制与免责声明”。

## 7. 许可范围

- 本项目原创代码与文档：MIT（见 [LICENSE](LICENSE)）。
- 第 1 节的第三方数据表：MIT（其版权行见上）。
- 第 2–3 节的参考项目与工具：各自许可，不由本项目授权。
- 第 4–5 节的游戏数据、站点数据与商标：归各自权利人，MIT 不覆盖。
