# 逆向研究工具链（REA）使用说明

本文件说明在**研究/逆向**阶段可选的 REA 工具链：它能做什么、如何安装、常用命令、与本项目的关系和限制。
**REA 不随本仓库分发**，普通导入/计算也不需要它；没有安装不影响任何计算结果。

> 说明：本文是使用方法说明，不是 REA 官方文档的翻译。命令与能力以你安装的版本 `rea --help` / `rea doctor` 输出为准。

## 1. REA 是什么

REA（Reverse-Engineering Agents，[morluto/rea](https://github.com/morluto/rea)，npm 包 `rea-agents`，MIT 许可）是一个
面向 agent 的逆向工程 CLI + MCP 工具：统一封装二进制、归档、.NET/IL2CPP 元数据、SQLite、网页/Electron、网络抓包等
静态侦察能力，并在安装了外部引擎（Ghidra / IDA / Hopper 等）时提供反编译、交叉引用与函数分析。

它与 Ghidra/IDA 不是替代关系：REA 负责“证据链 + 多格式侦察 + 可被 agent 调用的结构化输出”，
反编译能力仍由外部引擎提供。

本项目在开发阶段用它分析 Last Epoch 的 Unity IL2CPP 二进制、游戏资源与页面数据；相关结论整理在
`docs/evidence/reverse-engineering/`。这些结论是**静态证据**，不等于游戏内实测确认。

## 2. 不随仓库分发的部分

以下内容**不在公开仓库/发布包内**，需要时自行安装（均不属于普通计算依赖）：

| 组件 | 说明 | 缺失影响 |
|---|---|---|
| Node.js（REA 引擎要求的具体版本见其 `engines` 字段） | REA CLI/MCP 的运行时 | 无法运行 REA；不影响计算 |
| `rea-agents`（npm 全局或本地前缀安装） | REA 本体 | 同上 |
| Ghidra（如 12.x）+ JDK 21 | 反编译/函数分析引擎 | 只能用 REA 内置静态侦察，不能反编译 |
| IDA / Hopper / 其它引擎 | 可选替代引擎 | 同上 |
| 游戏安装目录、`GameAssembly.dll`、`global-metadata.dat`、资源包 | 分析对象 | 无法做游戏相关逆向；**不随仓库分发** |

推荐把便携运行时放在仓库内已被 gitignore 的目录（例如 `tools/node/`、`tools/jdk-21/`、`tools/ghidra_*/`），
避免污染系统 PATH；路径以本机实际安装位置为准，**不要把个人绝对路径写进仓库文档或配置**。

## 3. 安装（示例）

```powershell
# 1) 安装 Node（版本以 REA 的 engines 要求为准；用官方压缩包或包管理器）
#    解压到仓库内 gitignored 目录，例如 tools/node/，或用系统安装的 Node。
# 2) 安装 REA（按需选择全局或本地前缀）
npm install -g rea-agents
# 3) 验证
rea --version
rea doctor

# 可选：安装 Ghidra + JDK 21 后，设置环境变量（示例，按本机路径）
$env:JAVA_HOME = '<path-to-jdk-21>'
$env:GHIDRA_INSTALL_DIR = '<path-to-ghidra>'
rea doctor --provider ghidra
```

升级：`rea update`（会校验发布完整性）。如使用自建便携 Node，请确保升级命令使用同一个解释器环境。

## 4. 三种调用方式

### A. MCP（接入 agent 客户端）

REA 可以作为 MCP server 被支持 MCP 的客户端（DSH、Codex 等）调用。一般流程：

```powershell
rea setup --client <client-name> --skill=false --yes
# 重启客户端后，会出现 mcp__rea__* 工具；卸载用 rea uninstall --client <client-name>
```

具体 client 名称、配置路径与备份行为以 `rea setup --help` 为准。不要把批处理 shim 当作 MCP 的 `command`；
应直接指向 node 可执行文件与 `rea.mjs mcp`。

### B. CLI（任何 shell 可用）

```powershell
rea --help                 # 命令总览
rea doctor                 # 环境体检（node/host/引擎）
rea providers              # 分析引擎配置
rea capabilities           # 当前可用能力矩阵
```

建议对结构化输出显式限流，避免淹没上下文：

```powershell
rea inspect-desktop-application "<game-install>" --format json --filter-output normalized_result.candidates
rea <command> --format toon --token-limit 2000
rea <command> --schema     # 查看参数 schema
```

### C. 直接调用 Python/Node 工具

部分轻量分析可以用 Python（标准库或研究用第三方库）直接完成，不必装 REA；REA 的价值在于统一证据与多格式支持。

## 5. 与 Last Epoch 相关的常见分析目标

Last Epoch 是 Unity **IL2CPP** 游戏（`GameAssembly.dll` + `global-metadata.dat` + Unity 资源包）。
常见目标与建议入口：

| 目标 | 建议工具/命令 |
|---|---|
| 安装目录/运行库侦察 | `rea inspect-desktop-application <game-install>`、`rea inspect-artifact` |
| IL2CPP 元数据（类型/方法/字段/字符串） | `rea inspect-il2cpp-metadata <global-metadata.dat>`（静态读取，不执行游戏代码） |
| PE 结构（导入/资源/证书/TLS 回调/加载配置） | `rea inspect-pe-imports`、`inspect-pe-resources`、`inspect-pe-certificates`、`inspect-pe-load-config` |
| 原生函数反编译/交叉引用（需 Ghidra 等引擎） | `rea open-binary` / `rea decompile` / `rea xrefs` / `rea search-procedures` |
| 游戏资源/MonoBehaviour 字段（UnityPy） | 研究工具读取 `StreamingAssets/LEAssetBundles/*.bundle`（只读） |
| 页面数据/网络行为 | `rea inspect-web-page`、`inspect-web-network-capture`、`capture-browser-scenario` 等 |

注意：

- 只读分析，不修改游戏安装目录、不向游戏写入文件。
- 大二进制（`GameAssembly.dll` 约 97 MB）的反编译应按“一次性小时级”规划；默认启动预算与堆可能不足，需要时调整引擎启动参数。
- 元数据/函数结论是**静态证据**；游戏内实际行为仍需实测/日志核对（见 `docs/evidence/` 与 `mechanisms.md`）。

## 6. 限制与注意事项

- Windows 上 Ghidra 工程复用能力有限，通常是“一次分析一个大文件”；重复分析成本高。
- 未安装反编译引擎时，只能使用 REA 内置静态侦察能力。
- REA 及其依赖的安装、许可与更新由使用者自行负责；本仓库不附带其二进制、配置或个人路径。
- 工具输出是证据来源之一，不是游戏真值；不要把“反编译看到的常量”直接当作已确认机制，需结合上下文与实测。
- 分析产物中的路径、用户名、主机信息在写进 `docs/evidence/` 前应清理为本机无关的占位符。

## 7. 许可与致谢

REA 为 MIT 许可的第三方工具，**未捆绑**在本仓库中。第三方组件与参考项目的完整声明见
仓库根 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。
