# LastEpoch-BuildSimulator

Last Epoch 离线构筑工具集：从本机离线存档导入构筑 → 匹配沉淀的机制知识 → 按统一 DPS 计划/执行/报告流程生成数值与离线 HTML 报告。Windows 平台，入口为 PowerShell 脚本，计算与数据层使用 Python 3.12 标准库。

> **AI 生成声明（重要）**：本项目由 AI 协作开发，主要面向 AI agent 使用，也按“人类可读、可复核”的标准整理文档。代码、规则、数值与文档都可能包含 AI 引入的错误或不完整结论。所有结果按 **AS IS** 提供，不构成游戏官方数据或权威结论；引用前请先核对结果中的来源、假设与覆盖状态。项目与 Eleventh Hour Games 无关。

## 当前支持范围（请先读）

支持的是**有限的计算切片**，不是“任意技能、完整战斗模拟”：

| 能力 | 状态 |
|---|---|
| 离线存档解析为 BD（clean-room、只读；`1CHARACTERSLOT_BETA_*`、`STASH_*`） | 支持（无外部工具依赖） |
| 存档 → 可计算报告（完整首接：BuildSpec/calculation-input/snapshot → run） | **需要可选外部工具**：Node + `lastepochsupertools v1.3.1`（不随仓库分发）；缺失时明确返回 `needs_input`，干净 clone 不能直接对任意存档出 DPS |
| planner payload（Last Epoch Tools 本地 JSON）→ BuildSpec → 计算 | 支持但为**进阶手工流程**（离线、不依赖 Node；按 `tools/build_import/README.md` 补齐案例字段） |
| Void Cleave（VC）、Flame Reave（FR）、Black Hole（BH）三个 execution-v1 计算切片，统一 plan-v4 / result-v6 | 支持（限定范围） |
| Ballista / Armed Construction 到期与销毁爆炸口径 | **partial**（玩家侧尚有未建模来源） |
| 词缀库、构筑知识检索、离线 HTML 报告、本机条件重算 | 支持 |
| 其它技能/天赋、完整循环与实战 DPS、组队 Buff、法力消耗、完整战斗生命周期 | **不支持** |
| 完整回归、所有机制实测确认 | **不承诺** |

- 结果会显式保留 `assumed` / `unresolved` / `coverage` 缺口；`partial` 只表示声明范围内已计算，不表示“完整可信”。
- 能力与限制的最新摘要见 [docs/current-status.md](docs/current-status.md)；计划/结果字段契约见 [docs/contracts/](docs/contracts/)。

## 环境与依赖

**运行普通导入与计算只需要：**

- Windows 10/11；
- PowerShell 5.1+（Windows 自带）；
- Python 3.12+（推荐 3.12，项目按 3.12 验证）；运行面**不需要**任何第三方 Python 包（标准库即可）；
- 约 300 MB 磁盘空间用于 dataset。

**数据（不随 git 正文分发，单独获取）：**

- 运行所需 dataset：`phase1-dataset-003`（目标游戏版本 1.5.2，`data.sqlite` 约 251 MB）。它作为 GitHub Release 附件发布，需按下方“获取 dataset”下载到 `data/releases/phase1-dataset-003/data.sqlite`；`data/current.json` 记录其 `file_sha256`（`afa79e9f…`）。
- 词缀库 `data/affix-db/v1/normalized/affix-db.sqlite` 随仓库分发。
- `data/reference/`、affix-db 的 `source-snapshot/`、游戏本体与原始来源快照均**不随仓库分发**。

**可选依赖（普通计算不需要，也不随仓库捆绑；其中 tool-native 决定能否“从自己的存档完整首接”）：**

- **tool-native 完整首接依赖（可选）**：Node.js + `lastepochsupertools v1.3.1` 工具目录，用于把新存档解码成 tool-native wire。两者均不随本仓库分发，需自行准备并通过 `--tool-home` / `LE_TOOL_NATIVE_HOME`、`LE_NODE`（或 PATH 中的 node）配置。缺失时相关命令明确返回 `needs_input`，不会静默换路线；没有它仍可做“骨架导入”，但不会自动得到可计算的 BuildSpec/计算输入。
- JDK 21 + Ghidra、REA（Reverse-Engineering Agents）、UnityPy 等：仅二进制/游戏资源逆向研究使用，见 [docs/rea-guide.zh-CN.md](docs/rea-guide.zh-CN.md)。
- Git：仅用于版本信息（`doctor` / 打包清单），不参与计算。

## 获取代码与 dataset

```powershell
git clone https://github.com/BlaCatFishLE/LastEpoch-AIBuildSimulator.git
cd <REPO>
```

下载 dataset（推荐，自动校验 sha256 并原子落盘）：

```powershell
.\scripts\fetch_dataset.ps1 -Repo BlaCatFishLE/LastEpoch-AIBuildSimulator
# 可选参数：-Tag v0.4.1（默认）、-Url <直链覆盖>、-OutFile <目标路径>、-Force（已存在且哈希正确时仍重下）
```

仓库地址：<https://github.com/BlaCatFishLE/LastEpoch-AIBuildSimulator>。

手动下载（无法运行脚本或 Release 尚未就绪时）：从本项目 Releases 页面下载 `data.sqlite`，放到
`data/releases/phase1-dataset-003/data.sqlite`，并核对 SHA-256：

```powershell
(Get-FileHash -Algorithm SHA256 data\releases\phase1-dataset-003\data.sqlite).Hash
# 期望：AFA79E9FDA0023B2ED09F4CBE8E07DA436E0C74C03F34F80772DA471C0ADB8AB
```

## 快速开始

项目有两条输入路径（都需要先完成环境与 dataset 准备）：

- **存档路径**：clean-room 解析存档 → BD；若要生成可计算报告，需要“完整首接”（依赖 Node + `lastepochsupertools`，见可选依赖）。
- **planner 路径**（不依赖 Node，进阶手工流程）：用户提供 Last Epoch Tools planner payload 的本地 JSON → `le_build import --file ... --share-id ... --offline` → `le_build export` → 补齐案例字段 → prepare/run；命令见 `tools/build_import/README.md`。

```powershell
# 1) 创建虚拟环境（无需 pip install；le.ps1 会自动优先使用 .venv\Scripts\python.exe）
py -3.12 -m venv .venv
# 若没有 py launcher，可用：python -m venv .venv

# 2) 下载 dataset（或按上一节手动下载）
.\scripts\fetch_dataset.ps1 -Repo BlaCatFishLE/LastEpoch-AIBuildSimulator

# 3) 环境自检：解释器、模块导入、dataset 与必要数据
.\scripts\le.ps1 doctor

# 4a) 完整首接（需要上面的 Node + lastepochsupertools 工具目录；当前仅显式 BH 模型路线）
.\scripts\le.ps1 import --case examples\<case>\case.json --save '<存档副本>' --main-skill bh2 --model black-hole --tool-home '<lastepochsupertools目录>'

# 4b) 其它技能：先做研究接入，再在 prepare 指定主技能（能否产出 DPS 取决于当前支持范围与知识）
.\scripts\le.ps1 import --case examples\<case>\case.json --save '<存档副本>' --model research --tool-home '<lastepochsupertools目录>'
.\scripts\le.ps1 prepare --case examples\<case>\case.json --main-skill <技能ID>

# 4c) 没有外部工具：骨架导入（clean-room 解码；只生成 bd.json 与案例骨架，不会自动产出可计算输入）
.\scripts\le.ps1 import --case examples\<case>\case.json --save '<存档副本>'

# 5) 准备与计算（仅对已完整首接的案例）
.\scripts\le.ps1 prepare --case examples\<case>\case.json --main-skill <技能ID>
.\scripts\le.ps1 run     --case examples\<case>\case.json

# 6) 校验或启动本机重算服务，然后打开 run 目录下的 HTML
.\scripts\le.ps1 validate --case examples\<case>\case.json
.\scripts\le.ps1 serve    --case examples\<case>\case.json --port 8765
```

- 关于“从自己的存档算出 DPS”：需要先完成**完整首接**，而它当前依赖 Node.js + `lastepochsupertools`（不随仓库分发）。只用本仓库 + dataset 时，可以运行 `doctor`、知识检索、测试与已生成产物的校验/服务，但**不能**从零把一份新存档自动变成可计算的 DPS；不要把它当作开箱即用的游戏模拟器。
- 没有真实存档、想先确认当前支持范围可运行：`.\scripts\le.ps1 test --suite phase3` 会使用仓库内的合成最小输入（`tools/phase3/tests/fixtures/step5/` 的 plan-v4 hit/DoT/charge 夹具）；这些是测试夹具，不是可直接 run 的真实案例或 DPS 演示。
- `run` 默认输出到 `reports\<case-id>\<run-id>\`，其中包含结果 JSON、来源/规则账本、运行清单和离线单文件 HTML。HTML 可双击离线打开；条件重算需要 `serve` 在回环地址运行。
- 新案例目录可用 `examples/_template/` 作为空模板；推荐的路径仍是让 `import` 生成案例骨架（`import` 拒绝覆盖已存在的路径）。`examples/` 下除模板外都是用户本地产物，已 gitignore。
- 若主技能不在当前支持范围，`import`/`prepare` 会停在 `research` / `needs_research` 状态：只完成数据接入与准备，不伪造 DPS。支持范围见 [docs/current-status.md](docs/current-status.md)。
- 其它常用命令：`.\scripts\le.ps1 test --suite <name>`（按套件运行测试，公开版部分依赖用户本地数据的测试会 SKIP）、`.\scripts\le.ps1 package`（按显式清单生成发布包）、`.\scripts\le.ps1 doctor --help`。完整命令清单见 [docs/workflow.md](docs/workflow.md)。

## 知识检索

机制/技能知识以 `knowledge/` 下的 JSON 为权威源；SQLite 索引是生成物，可删除重建。

```powershell
.\scripts\le.ps1 knowledge rebuild                     # 重建索引
.\scripts\le.ps1 knowledge search --query "Flame Reave" # 摘要检索
.\scripts\le.ps1 knowledge search --query "node:fr11mv:16"
.\scripts\le.ps1 knowledge show --id fr-node-reflash    # 查看单条详情
```

知识条目的证据引用可能指向作者本地归档（`archive://` 前缀）；`docs/evidence/` 中是随仓库分发的可复用证据文本。

## 常见问题

- **`doctor` 报 dataset 缺失**：按“获取代码与 dataset”一节下载并校验 sha256，或运行 `fetch_dataset.ps1`。
- **`doctor` 找不到 Python**：先创建 `.venv`（`py -3.12 -m venv .venv`），或设置环境变量 `LE_PYTHON` 指向 Python 3.12+。
- **`import` 返回 `needs_input`（完整首接）**：缺少 Node.js 或 `lastepochsupertools` 工具目录。用 `--tool-home` / `LE_TOOL_NATIVE_HOME` 指定工具目录、`LE_NODE`（或 PATH 中的 node）指定 Node；两者都不随仓库分发。没有这些工具时只能做骨架导入，不能直接 run 出 DPS。
- **没有存档但有 planner 数据**：用 planner payload 的本地 JSON 走 `le_build import --file ... --share-id ... --offline`（见 `tools/build_import/README.md`），再补齐案例字段。
- **存档读不出来**：仅支持本机**离线**存档（`1CHARACTERSLOT_BETA_*` / `STASH_*`）。云存档、旧版本非 v6 物品格式不在范围内；先确认存档副本路径正确、未被游戏占用。
- **支持哪些技能**：目前只有 VC/FR/BH 三个 execution-v1 切片，Ballista 为 partial。其它技能可以完成导入/研究注册，但没有可用的 DPS 结论。
- **结果和游戏面板/实战不一致**：这是限定范围的模拟结果，不是实战 DPS；先看报告的假设、来源与覆盖状态，再看 `coverage` 缺口。不要把它当作游戏实测。
- **网页重算失败**：离线双击打开只能看已生成结果；条件重算需要 `.\scripts\le.ps1 serve --case ...` 保持运行，且端口与报告一致。
- **`test --suite` 有 SKIP**：公开版不附带作者本地案例/报告；依赖这些数据的测试会显式跳过，这是预期行为。

## 已知限制与免责声明

- **开箱程度**：干净 clone + dataset 可运行 `doctor`、知识检索、测试、校验/服务已有产物；但要从任意用户存档生成 DPS，需要用户自备可选的 Node + `lastepochsupertools`。这不是“免安装的游戏模拟器”。
- **计算范围**：仅 VC/FR/BH execution-v1 切片 + Ballista partial；不是任意技能、完整战斗模拟或实战 DPS。
- **结果缺口**：`status` / `issues` / `coverage` 中的 assumed/unresolved/partial 缺口随结果保留；未列出的机制不等于已确认。
- **版本边界**：数据层是游戏 1.5.2 的规范化数据集，部分机制逆向证据来自 1.5.3；文档分别标注版本，不承诺跨版本等价。
- **平台**：Windows 10/11 + PowerShell 5.1+ + Python 3.12+；其它平台未验证。
- 项目不是游戏官方工具，不修改游戏文件；导入过程只读取你自己的存档副本。
- 结果可能包含 AI 生成或推断的错误。使用前请自行复核；作者不对据此作出的游戏决策负责。
- Last Epoch 名称、游戏数据与相关商标归 Eleventh Hour Games 所有；本仓库不分发游戏本体、原始存档或未授权游戏资源。

## License

- 原创代码与文档以 **MIT License** 发布，全文见 [LICENSE](LICENSE)。
- 第三方来源、随仓库分发的 MIT 数据表、参考项目致谢及“游戏数据不随仓库分发”等声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。MIT 仅覆盖本项目原创部分，不覆盖第三方内容或游戏数据。

## 目录结构

```text
scripts/          统一入口 le.ps1、命令实现、打包/校验、dataset 下载脚本
config/           通用配置模板（project.json 等）；本机配置与密钥不入库
data/             词缀库、映射、data/current.json 与本地 dataset（大文件不入库）
knowledge/        机制/技能知识 JSON 权威源与生成索引
tools/            实现模块：save_import / build_import / affix_db / phase3（计算引擎）/ knowledge / ingest / extract-game
docs/             现行文档、契约、证据（见 docs/README.md）
examples/         用户本地案例目录；仓库只保留 _template 模板
reports/          用户本地运行产物（gitignored）
dist/             打包输出（gitignored）
```

## 文档

- [AGENTS.md](AGENTS.md)：AI agent 工作入口（日常执行顺序、知识收尾、HTML 质量契约要点、不可变对象）。
- [docs/README.md](docs/README.md)：文档索引（现行文档、契约、证据、归档约定）。
- [docs/current-status.md](docs/current-status.md)：能力、支持矩阵、限制与最近验证状态。
- [docs/workflow.md](docs/workflow.md)：从导入到报告的操作流程与新机制接入规则。
- [docs/architecture.md](docs/architecture.md)：模块职责、数据流与格式契约。
- [docs/version-control.md](docs/version-control.md)：跟踪范围、不可变数据、发布流程。

## 验证状态

**公开发布验证（2026-11-13，tag v0.4.1）**

- **仓库与 Release**：<https://github.com/BlaCatFishLE/LastEpoch-AIBuildSimulator>；dataset 通过 Release 附件 `data.sqlite` 分发（251.2 MB，sha256 `afa79e9fda0023b2ed09f4cbe8e07da436e0c74c03f34f80772da471c0adb8ab`）。`scripts/fetch_dataset.ps1` 默认从本 tag 的 Release 下载并校验（附件发布后复核）。
- **v0.4.1 同步范围**：在 v0.4.0 快照（2026-11-13 05:23 +08:00）基础上，同步 2026-11-13 的修复批次：Ballista Armed Construction 修正轮（伤害通道类型隔离、来源绑定/结果门禁、编译期暴击折叠）、共享 damage channel 闭包、装备页完整词条与 `unique_description` 投影、报告 UI/契约/知识索引更新。同步边界截至 2026-11-13 12:53 (+08:00)；之后的上游工作另行发布。
- **验证环境**：Windows 10（10.0.19045）、PowerShell 7.6.10（脚本声明 5.1+）、Python 3.12.14。
- **已执行**：
  - 结构/隐私/死链/JSON 检查：PASS（0 本机路径、0 断开链接、0 无效 JSON、0 个 >100 MB 的跟踪文件）；
  - `le.ps1 doctor`：PASS（dataset 指针 + 文件 sha256 + SQLite + 解析路由）；
  - `knowledge rebuild`：OK（127 机制 / 130 条目，`source_digest=sha256:07ac7a3f…`）；`knowledge search` 抽查命中；
  - 合成最小输入 `fixtures/min-case/input-snapshot.json`：`le_calc check` 通过；`le_calc run` 生成 `result.json` 与 `black-hole-damage.html`；
  - 测试套件（含 Release dataset）：`save_import` 24/0、`knowledge` 91/0、`affix_db` 71/skip 23、`build_import` 35/skip 15、`phase3` **628/skip 117**、`ingest` 188/skip 38，全部 PASS（skip 为依赖作者本地案例/报告的模块，显式跳过）。
- **快照边界**：v0.4.1 为 2026-11-13 12:53 (+08:00) 的同步结果；仍不是完整战斗模拟，Ballista 支持范围按 partial 声明。版本审计见 `docs/evidence/extracted/version-audit.json` 的 `release_snapshot.incremental_syncs`。
- **未验证项（如实列出）**：真实用户存档的完整首接（需自备可选 Node + `lastepochsupertools v1.3.1`）、planner payload 手工流程的端到端操作、原生 Windows PowerShell 5.1 实机、完整 `test --suite all` 与完整网页验收矩阵；v0.4.1 Release 附件下载在发布后复核。能力边界见 [docs/current-status.md](docs/current-status.md) §4。
