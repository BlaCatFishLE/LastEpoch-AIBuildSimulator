# tools/knowledge — 机制/技能知识的可重建索引与独立查询接口（P1）

日期：2026-11-07。状态：**P1 实现**。
依据：[workflow-interface §5.1 / §8 / §9](../../docs/contracts/workflow-interface.md)、
[workflow.md](../../docs/workflow.md)、[knowledge/README.md](../../knowledge/README.md)。

## 1. 定位

- **JSON 是唯一权威源**：`knowledge/mechanisms/*.json`、`knowledge/skills/*.json`（字段见
  `knowledge/schemas/*.json`）。本模块**只读**它们。
- `data/cache/knowledge.sqlite` 是可删除重建的查询索引，**不是知识源**。
- `knowledge/INDEX.generated.md` 由本模块生成，禁止手改；手工改动会在 `rebuild` 时被覆盖并告警。
- 查询接口可绕过编排独立调用：不依赖当前案例、聊天上下文或全局状态；索引/资产根通过显式参数传入。

## 2. 命令（正式入口 `.\scripts\le.ps1`）

```powershell
.\scripts\le.ps1 knowledge rebuild                  # 从 JSON 重建 sqlite + INDEX.generated.md
.\scripts\le.ps1 knowledge rebuild --check          # 只检查索引是否新鲜（过期返回 1）
.\scripts\le.ps1 knowledge search --query "Flame Reave"        # 短摘要（默认）
.\scripts\le.ps1 knowledge search --query "node:fr11mv:16"     # 带命名空间 ID → 精确匹配
.\scripts\le.ps1 knowledge search --query "ignite" --kind keyword
.\scripts\le.ps1 knowledge search --component ignite --tag fire # 只用过滤条件浏览
.\scripts\le.ps1 knowledge search --query "Flame Reave" --json  # 机器可读结果（验收用）
.\scripts\le.ps1 knowledge search --query-json query.json       # 完整 le-mechanism-query-v1
.\scripts\le.ps1 knowledge show --id fr-rhythm-of-fire           # 详情（规则/证据/实现/未解决项）
.\scripts\le.ps1 knowledge show --id fr-flame-reave --json       # 含完整权威 JSON（document 字段）
.\scripts\le.ps1 knowledge rebuild --no-fts                      # 强制 LIKE 回退（无 FTS5 行为）
```

退出码沿用仓库约定：`0` 成功 / `1` 失败（校验错误、条目不存在、`--check` 过期） / `2` 参数错误。

## 3. 独立调用（不经过 le.ps1）

```powershell
$env:PYTHONPATH = 'tools\knowledge\src'
& .\.venv\Scripts\python.exe -m le_knowledge search --query "node:fr11mv:16"
& .\.venv\Scripts\python.exe -m le_knowledge show --id fr-rhythm-of-fire
& .\.venv\Scripts\python.exe -m le_knowledge rebuild
```

```python
from le_knowledge import search, show, rebuild, index_state

result = search({"queries": [{"query_id": "q1", "term": "node:fr11mv:16", "kind": "node_id"}]})
detail = show("fr-rhythm-of-fire")
report = rebuild()                       # 可显式传 knowledge_dir=/index_path=/index_md_path=/root=
state = index_state()                    # {"status": "ok"|"stale"|"missing"|"corrupt"|"incompatible"}
```

## 4. 返回结构与“逐条匹配语义”（workflow-interface §5.1）

`search()` 返回 `le-mechanism-result-v1`：

| 字段 | 说明 |
|---|---|
| `entries[].mechanism_id` / `entry_kind` | 机制 ID；技能模板条目另带 `skill_id`、`ability_id` |
| `entries[].matched_queries[]` | 每条 `{query_id, term, kind, selector, match_quality, match_reason}`；`match_quality ∈ {exact, candidate}` |
| `entries[].applicability` | `{status ∈ {satisfied, unknown, not_applicable}, evaluated_on[], missing_conditions[]}` |
| `entries[].rule_refs` / `evidence_refs` / `implementation_refs` | 规则、证据、实现入口 |
| `entries[].evidence_state` / `implementation_state` / `unresolved[]` | 与 readiness 独立的状态 |
| `unmatched_queries[]` | `{query_id, term, kind, reason}`；reason ∈ `no_match` / `filtered_out` / `missing_namespace` / `namespace_mismatch` |
| `summary.match_quality` | **仅结果集摘要，不参与自动绑定**；自动绑定只接受“至少一条 exact 且 `applicability.status=satisfied`” |
| `index_digest` | 索引内容指纹（`sha256:...`，与内容源一致） |

批量查询中已知 ID 与未知 ID 混排时分别回报：已知 → `exact`，未知 → `unmatched_queries[].reason=no_match`，
不会因为部分命中而整体退化。候选（`candidate`）不会自动绑定。

**适用性判定**：查询可带可选 `context`（扩展字段，不属于契约必填项）：

```json
{"context": {"conditions": ["ignite_active"], "nodes": ["node:test:10"], "exhaustive": false}}
```

- 无条件要求 → `satisfied`（`evaluated_on=["always"]`）；
- 有要求但上下文不足 → `unknown`（不得与 `satisfied` 混同）；
- `exhaustive=true` 且缺要求 → `not_applicable`。

**选择器组合规则**（内容登记与 schema 的边界，见 [docs/mechanisms.md](../../docs/mechanisms.md)）：
schema 把语义命名空间放在 `kind`、把来源子命名空间放在 `namespace`，引擎在加载时组合为
`kind:<namespace>:<id>`；`ids` 已带契约命名空间（如 `node:fr11mv:16`）时原样保留：

| 登记 | 主选择器 | 兼容选择器（`kind:id`，便于按契约示例查询） |
|---|---|---|
| `{kind: node, namespace: fr11mv, ids: [16]}` | `node:fr11mv:16` | — |
| `{kind: ability, namespace: dataset, ids: [bh2]}` | `ability:dataset:bh2` | `ability:bh2` |
| `{kind: stat, namespace: le_calc, ids: [damage]}` | `stat:le_calc:damage` | `stat:damage` |
| `{kind: item, namespace: slot, ids: [weapon1]}` | `item:slot:weapon1` | `item:weapon1` |
| `{kind: mechanism, namespace: bh, ids: [x]}` | `mechanism:bh:x` | `mechanism:x` |

兼容选择器只增加可检索入口，主选择器与条目内容不变；`matched_queries[].selector` 会回报实际命中的
那一个。内容侧若要统一为契约示例形式（`ability:fr11mv` / `stat:fire_damage`），需 Lead 决定后由
登记方调整 JSON；引擎不修改权威源。

**查询次序**：命名空间精确 ID（`mechanism:` / `node:` / `ability:` / `item:` / `stat:` / …）→
组件/标签/所选节点/条件过滤 → 别名/stat_key/计算块/关键词（FTS5 可用则用，否则 LIKE；
中文走维护的 alias/标题）→ 无精确匹配给候选。

## 5. 索引与维护

- 表：`meta`（`index_format` / `source_digest` / `counts` / `fts` / `warnings`）、`files`、`entries`、
  `selectors`、`aliases`、`search_terms`、`mappings`；FTS5 可用时另有 `terms_fts`。
- 内容指纹 = 内容文件（机制/技能/记录 schema）的路径 + sha256；同一内容重复重建 digest 不变、
  Markdown 字节一致；内容变化/索引缺失/索引损坏/格式版本不符时查询前自动重建。
- 无 FTS5 环境：建索引时写入 `meta.fts=0` 并回退 `LIKE`；若索引带 FTS 表但当前环境不可用，
  查询也会自动回退（`summary.keyword_backend` 为 `like`）。
- 校验失败时 `rebuild`/查询报错并给出 `文件: 字段路径: 原因`，不写半成品索引。
- 输出中的路径一律仓库根相对；根外路径显示 `<external-path>`，不泄漏机器绝对路径。

## 6. 测试

```powershell
.\scripts\le.ps1 test --suite knowledge
```

等价底层命令（从仓库根）：

```powershell
$env:PYTHONPATH = 'tools\knowledge\src'
& .\.venv\Scripts\python.exe -m unittest discover -s tools/knowledge/tests -t tools/knowledge/tests
```

测试使用临时目录 + 小型 fixture（只复制 `knowledge/schemas/` 下的契约 schema，条目内容在临时目录
现造），**不依赖** `knowledge/mechanisms|skills` 的真实登记数量。覆盖：

- loader 最小 schema 校验（required/enum/type/附加字段，错误带文件与 JSON pointer）；
- 选择器归一化（`ids: ["fr11mv:16"]` 与 `ids: ["node:fr11mv:16"]` 两种写法）、技能模板派生选择器；
- 逐条匹配语义（`matched_queries` / `applicability` / `unmatched_queries` / `summary` / `index_digest`）；
- 批量已知/未知 ID 混排不退化；过滤与过滤排除（`filtered_out`）；
- 索引重建幂等、内容变化、缺失/损坏索引自动重建、空知识库重建、Markdown 生成与手工改动告警；
- 无 FTS5 回退（强制 `--no-fts` 与 FTS 表缺失两条路径）；
- `show` 未找到报错、交叉引用（技能↔机制）、CLI 退出码 0/1/2；
- `scripts/le_commands.py` 的 `knowledge` 子命令转发与测试套件登记。

## 7. 目录

```text
tools/knowledge/
  README.md
  src/le_knowledge/
    __init__.py      # 公开 API：search/show/rebuild/index_state/load_corpus
    api.py           # 独立接口编排、索引新鲜度与重建
    loader.py        # 扫描/校验/归一化 JSON 权威源、内容指纹
    schema.py        # 最小 JSON Schema 子集校验器（无第三方依赖）
    store.py         # SQLite 索引构建（原子替换）与只读读取/FTS 回退
    query.py         # 查询次序、逐条匹配语义、过滤、适用性
    index_md.py      # INDEX.generated.md 生成
    cli.py           # search/show/rebuild CLI（le.ps1 与 python -m le_knowledge 共用）
    paths.py         # 路径/契约常量与可移植路径显示
    __main__.py      # python -m le_knowledge
  tests/             # 临时 fixture 单测（le.ps1 test --suite knowledge）
```
