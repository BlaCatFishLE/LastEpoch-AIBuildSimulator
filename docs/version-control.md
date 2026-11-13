# 版本控制、发布与数据分发（version-control）

本文件规定：仓库跟踪什么、什么一发布就不能改、提交/标签怎么写、dataset 如何分发、发布包怎么生成与校验。
**所有 git 操作由仓库维护者执行**，本文件只写约定与可核对的事实。实际忽略规则以仓库根 `.gitignore` / `.gitattributes` 为准。

## 1. 基本约定

- 仓库根目录 = 本工程根目录；**一个仓库**管理业务模块、规则、脚本、文档、知识与必要数据，不按模块拆多个仓库。
- 默认分支 `main`。
- **公开发布使用新的 GitHub 仓库，单次初始提交，不包含旧私有仓库历史**；旧私有仓库留档，不迁移其历史提交。
- 提交身份使用维护者自己的 GitHub 配置；不伪造他人身份、不写本机用户名/邮箱到文件正文。
- 大文件不进 git 正文：dataset（约 251 MB）通过 **GitHub Release 附件** 分发；本机虚拟环境、外部运行时、用户产物不入库。
- 提交前先 `le.ps1 doctor` + 受影响测试通过（公开版允许依赖本地案例的测试 SKIP，但需如实记录）；提交后工作树应干净。

## 2. 跟踪与忽略策略

判定原则：**复现与运行必需、或需要版本审计** → 跟踪；**可再生产、用户本地、第三方原始快照** → 忽略。

| 跟踪（入库） | 忽略（不入库） |
|---|---|
| `tools/**` 业务源码、`schemas/`、`rules/`（现行版本）、`tests/`、`fixtures/` | `.venv/`、`venv/`、`__pycache__/`、`*.pyc`、`.pytest_cache/` 等缓存 |
| `scripts/`（统一入口、命令实现、打包/校验、`fetch_dataset.ps1`） | `.scratch/`（临时工作区）、`_verify/`、`dist/`、`*.zip`、`*.log`、`*.tmp` |
| `config/project.json`、`config/*.example.json`、`config/coverage-scope.json` | `config/local.json`、`config/*.local.json`、`config/sources.json`、`.env*` |
| `docs/**`（现行文档、契约、证据文本） | 作者本地的历史/审查/清点文档、截图与大文件归档（不随公开树提供） |
| `knowledge/**`（机制/技能 JSON 权威源、schema） | `data/cache/`（知识索引、阶段缓存、操作记录；可重建） |
| `data/affix-db/v1/normalized/`（规范化词缀库） | `data/affix-db/v1/source-snapshot/`（第三方原始快照；公开树不包含，本地重建时不要提交） |
| `data/mappings/`、`data/current.json`、`data/releases/<dataset>/manifest.json` | `data/releases/*/data.sqlite`（Release 附件）、`data/releases/*-001|002`（历史 dataset） |
| `examples/_template/`（空模板） | `examples/<case>/**` 的其它内容（用户本地案例） |
| 测试夹具与最小合成输入 | `reports/**`（用户本地运行产物） |
| `README.md`、`AGENTS.md`、`THIRD_PARTY_NOTICES.md`、`LICENSE`、`.gitignore`、`.gitattributes` | `data/builds/**`（构筑 revision 本地产物）、`data/raw/**`（只读存档/来源快照） |
| — | `tools/node/`、`tools/jdk-21/`、`tools/ghidra_*/` 等外部运行时/引擎 |

补充：

- `package-manifest.json` 只在发布输出目录生成（根目录同名文件被忽略），因此可以记录所属 commit 而不产生自引用哈希。
- `data/current.json` 是 dataset 指针，入库；`data/releases/<dataset>/data.sqlite` 不入库，靠 Release 附件 + 指针中的 `file_sha256` 校验。
- `examples/_template/` 是唯一入库的 examples 内容；真实案例、存档、build、report 一律不提交。
- 修改 `.gitignore` / `.gitattributes` 后必须重查“待提交清单 + 被忽略清单”：确认正式依赖没被漏掉、没有带入本机私密内容或缓存；该类修改单独提交并说明原因。

## 3. 不可变发布数据

以下内容一旦发布即**只读**，不得就地改写、重排字段或重新生成后覆盖：

| 不可变对象 | 位置 | 变更方式 |
|---|---|---|
| 发布 dataset | `data/releases/<dataset-id>/`（`data.sqlite` + `manifest.json`） | 新建 dataset-id，重跑 validate → publish；旧 dataset 保留 |
| 构筑 revision | `data/builds/<source>/<share-id>/revisions/<rev-id>/` | 新输入 → 新 revision（seq+1）；旧 revision 保留只读 |
| 当前 dataset 指针 | `data/current.json` | 只由 publish 原子替换；partial 不替换（除非显式 `--force-current`） |
| 运行产物 | `reports/**/<run-id>/` | 重新跑 = 新 run-id；旧 run 作为对照保留，不改写 |
| 逆向/实测证据 | `docs/evidence/**` | 只追加新证据文件；旧证据不重写数值 |
| 知识条目 | `knowledge/**` | 更新内容/状态需保留证据入口；不能为通过校验伪造 `validated` |

原则：已发布、按内容哈希被引用的对象**必须保留原字节**；位置可变但身份不可变。历史产物内部的旧路径引用不回改（那是当时的事实记录）。

## 4. `.gitattributes`：禁止换行转换

本工程给产物记指纹（dataset manifest、`run-manifest.json`、`rule-ledger.json` 等记录文件 SHA-256）。Git 任何 checkout/commit 换行转换都会让指纹在克隆后失效，因此 `.gitattributes` 的现行规则是：

| 规则 | 作用 |
|---|---|
| `* -text` | 全局关闭 EOL 归一化：所有文件按原始字节存/取（仍保留普通文本 diff） |
| `*.sqlite`、`*.db`、图片/PDF/压缩包/字体/`*.dll`/`*.exe`/`*.so`/`*.dylib` → `binary` | 不 diff/merge、不转换 |
| 存档夹具（`1CHARACTERSLOT_BETA_*`、`STASH_*`）→ `binary` | 存档是逐字节输入 |
| dataset/affix-db manifest、规则 JSON、schema JSON → `-text` | 哈希绑定的 JSON：按原样保存，但仍可作为文本 diff |
| **没有任何 `text`/`eol` 规则** | PowerShell、Python、JSON、Markdown 也保持字节精确 |

## 5. dataset 分发与恢复

- 运行所需 dataset 是 `phase1-dataset-003`（约 251 MB），作为 GitHub Release 附件 `data.sqlite` 分发（tag 与仓库地址见 README）。
- 下载脚本：`scripts/fetch_dataset.ps1 -Repo BlaCatFishLE/LastEpoch-AIBuildSimulator [-Tag v0.4.1] [-Url <直链>] [-OutFile <路径>]`。
  默认落盘 `data/releases/phase1-dataset-003/data.sqlite`，先写 `.part` 再校验 `data/current.json` 的 `file_sha256`（`afa79e9f…`），成功后原子改名；失败删除临时文件并打印期望/实际 sha256。
- 手动恢复：从 Releases 下载 `data.sqlite` → 放到 `data/releases/phase1-dataset-003/data.sqlite` → 用 `Get-FileHash -Algorithm SHA256` 核对 `data/current.json` 的 `file_sha256`。
- 恢复后跑 `.\scripts\le.ps1 doctor`；如需重建指针，只通过 publish 流程，不手改 `data/current.json`。
- 词缀库随仓库分发（`data/affix-db/v1/normalized/affix-db.sqlite`），无需单独下载。

## 6. 提交规范

- 一次提交做一件事：代码/规则改动与其测试、文档更新放在同一提交；纯文档整理单独提交。
- 建议格式（中文说明允许）：

```text
<type>(<scope>): <一句话目的>

<为什么改、影响哪些契约/数值、验证命令与结果>
```

  `type`：`feat` / `fix` / `data` / `docs` / `refactor` / `test` / `chore`；
  `scope`：`ingest` / `build_import` / `save_import` / `affix_db` / `phase3` / `knowledge` / `docs` / `scripts` / `examples`。
- 提交前必须：
  1. `.\scripts\le.ps1 doctor` 通过（缺少 dataset 时先按第 5 节恢复）；
  2. 受影响测试通过（`le.ps1 test --suite <name>`），失败/跳过如实写入提交说明；
  3. 核对关键数值/结构：同版本同场景结构等同；结论以结果内 `issues` / `coverage` 为准；
  4. `git status` 无意外未跟踪文件（尤其本机配置、缓存、个人路径）。
- **禁止**：改写已发布 dataset/revision/证据；提交 `.venv`、外部运行时、私密配置；用 `--force` 覆盖他人历史；把别人的未完成改动一起卷入。

## 7. tag 与发布流程

1. **创建新仓库**（不含旧历史），完成单次初始提交，确认 `git status` 干净、待提交清单与 `.gitignore` 一致。
2. 在干净 clone 上按 [README](../README.md)“快速开始”复核：创建 venv → `doctor` → `knowledge rebuild` + `search` → 受影响 `test --suite` → 模板/合成最小输入 `import → prepare → run → validate/serve` → 打开 HTML；把结果填入 README 的“验证状态”区块。
3. 打 annotated tag：`v0.4.1`（发布后不再悄悄修改该版本对应的提交）。
4. 创建 GitHub Release（tag `v0.4.1`），上传 dataset 附件：
   - 附件名 `data.sqlite`，内容为 `phase1-dataset-003/data.sqlite`；
   - 发布说明中给出 sha256（与 `data/current.json` 的 `file_sha256` 一致：`afa79e9f…`）与下载后放置路径；
   - 若 dataset 或仓库地址变化，同步更新 README 的示例命令与占位符。
5. 发布包（可选）：从该 tag 的干净工作树生成 `scripts/package.py` 输出（默认 `dist/`），再用 `scripts/check_package.py` 校验；`package-manifest.json` 记录 commit/tag、数据指纹与逐文件 SHA-256，且不把自己列进文件清单（避免自引用）。

## 8. 发布前检查清单

- [ ] 工作树干净，当前提交就是验收提交；tag `v0.4.1` 已打。
- [ ] `.gitignore` / `.gitattributes` 与第 2、4 节一致；待提交清单无用户产物、第三方原始快照、本机配置。
- [ ] 公开文件中**没有**个人机器绝对路径、用户名、邮箱、QQ、第三方版权代码或未授权游戏数据。
- [ ] `doctor`、`knowledge rebuild`、受影响 `test --suite` 通过（失败/跳过项已记录并说明原因）。
- [ ] 干净 clone 按 README 快速开始走通；HTML 可打开；条件重算（`serve`）按需验证。
- [ ] Release 已上传 `data.sqlite`，README/本文中的 sha256 与实际一致；下载后放置路径正确。
- [ ] dataset/词缀库的版本与再分发声明见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)；LICENSE 未被修改。
