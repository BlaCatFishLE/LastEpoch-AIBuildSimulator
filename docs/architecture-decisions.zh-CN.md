# 架构决策记录（第一阶段）

日期：2026-11-01。范围：Last Epoch 数据提取与规范化数据库（不含 BD 导入与伤害引擎）。

## ADR-001 主流程使用 Python 标准库 + SQLite

**决策**：提取、解析、规范化、校验、报告、发布均用 Python 3.12 实现，Web 数据链路不引入第三方依赖；数据合同为 SQLite + JSONL + Markdown，与语言无关。

**理由**：项目约定数据合同与运行语言无关、整个项目只维护一套规范化逻辑；统一使用 Python 3.12（标准库）并避免依赖 Windows 商店别名解释器。标准库足以完成 HTTP、JSON、SQLite、哈希；避免为提取流程引入需要长期锁定的包。本地游戏资源阶段按需在项目内 venv 安装 UnityPy（ADR-006），但该研究依赖不随仓库分发。

**后果**：`le-data` 以 `python -m le_data.cli` 或安装后的 console script 运行；不依赖网络安装。

## ADR-002 禁止执行下载的 JavaScript

**决策**：两站数据都嵌在压缩 JS 包中（LE Tools 的 `window.*={...}`、Tunklab 的 Turbopack 数据模块）。项目内置 `le_data/jsdata.py`，只解析字面量子集：对象、数组、字符串、数字、布尔、`null`、`!0/!1`、`void 0`，以及经白名单确认的纯常量变换 `.split("x")`、`.concat([...])`、`.trim()`。其余函数调用、标识符、模板字符串、展开、计算键一律抛 `UnsupportedJsConstruct` 并记录 issue。

**理由**：项目约定禁止执行下载的整段脚本。`.split()` 在这两个站点用于编码字符串列表（`"a b c".split(" ")`），是纯数据变换，可安全解码并记录在解析器版本中。

**后果**：站点改版若引入新的常量变换，解析明确失败并留下定位，而不是静默产出空数据。解析器不解析真实业务逻辑代码，因此不构成“运行站点代码”。

## ADR-003 三层分离：discover → fetch/cache → parse → normalize → validate → publish

**决策**：每个来源适配器实现 `discover` / `fetch` / `parse` 三个方法，产出的 `SourceRecord`（完整原始 JSON + 定位 + parse_status）落 JSONL；规范化从 JSONL 读取，不再访问网络或原始 JS。

**理由**：项目约定要求网络、解析、入库分离，离线可重建、可重复运行、失败可恢复；也让两个来源的数据形状调查可以并行进行而不互相污染。

**后果**：`parse --offline` 与 `normalize --offline` 真正无网络；原始响应缓存（`data/raw/<source>/<snapshot>/responses/`）是重解析唯一输入。

## ADR-004 一次 dataset 对应固定输入；run 与 dataset 分离

**决策**：`run_id` 记录一次执行与所用快照；`dataset_id` 记录一组固定快照 + 解析/映射版本；manifest 记录二者及每个来源的版本证据。网站版本与本地游戏版本不一致时不默认兼容，结论写入 `dataset_meta.compatibility_conclusion`。

**理由**：项目约定要求降低历史数据混算风险、区分抓取时间/来源声称版本/本地 build/确认兼容版本。

**后果**：`diff` 只比较逻辑内容指纹；发布物可从原始快照独立重建。

## ADR-005 内部身份与跨源合并

**决策**：内部 `entity_id = <kind>:<source>:<scope>:<source_id>`。跨源合并只经 `data/mappings/crosswalk.json` 显式登记（canonical entity + members + 方法 + 证据）。未登记的同名实体不合并；两个来源各自保留 record、ID 映射与字段证据。

**理由**：项目约定禁止用名称或数字 ID 直接判等；相同数字 ID 在不同作用域未必同一实体。显式 crosswalk 让每次合并可审计、可回滚，也避免错误合并污染后续计算。

**后果**：覆盖率报告分别展示两站数量与规范化去重数量；crosswalk 为空时去重数为 0，这是事实而非缺陷。

## ADR-006 本地游戏资源仅作为缺口补充，只读

**决策**：游戏目录只读；仅在网页来源明确缺字段时按只读研究流程用 UnityPy（项目内 venv，锁定版本）读取具体容器。GameAssembly.dll + metadata 表明 IL2CPP 路线，Il2CppDumper/Cpp2IL 等类型恢复工具只在必要时作为下一步建议，不在本阶段无条件安装。

**理由**：项目约定要求小范围验证、不修改游戏文件、不把工具输出写进游戏目录、不扩展成无边界逆向。

**后果**：报告需注明工具版本、容器路径、哈希、path id 与兼容性结论；无法解出的字段以 issue 形式保留。

## ADR-007 缺失值语义

**决策**：未知值一律 `NULL`，并在 `field_evidence` 记录 `known/absent/not_applicable/unparsed/conflict/inferred`。禁止用 0、空串、空数组代替未知。`source_records.parse_status`、`raw_resources.fetch_status`、`dataset_meta.status` 是互不混用的独立枚举。

**理由**：项目缺失值约定：后续阶段必须能区分“来源不给”与“我们还没解析”。

## ADR-008 发布协议

**决策**：normalize 产出 staging 库 → validate 写报告并在 `dataset_meta` 置 valid/partial → publish 校验门槛、复制到 `data/releases/<dataset>/`、写 manifest、最后原子替换 `data/current.json`。partial 不再替换已有 current（除非显式 `--force-current`）。WAL checkpoint 后再复制，保证发布文件可独立打开。

**理由**：项目发布协议要求失败保留旧版、原子指针、manifest 可独立搬运校验。

## ADR-009 报告口径

**决策**：所有数量由当前数据库/运行日志机器生成，不手写；覆盖率分母来自 `config/coverage-scope.json`，来源声称总数与索引实际数不一致时解释口径；无法确定分母写“未知”，不输出 100%。覆盖率只描述数据层级（原文/结构化/关系已解析），不描述计算正确率。

**理由**：项目 READY 判定要求（数量机器生成、分母可解释、未知不输出 100%）。
