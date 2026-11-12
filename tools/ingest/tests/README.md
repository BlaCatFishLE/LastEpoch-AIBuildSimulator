# 边界与失败模式测试（task-5）

工作指导书 §13 要求的失败模式验收：可重复运行的 `unittest` 测试 + 与真实数据分离的合成 fixture。
测试只读取/构造临时 SQLite 与临时目录，不写真实 `data/current.json`、不触碰任何来源缓存。

## 运行

```powershell
Set-Location -LiteralPath '<repo>'
# 全量测试（环境无 pytest，使用标准库 unittest）
.\.venv\Scripts\python.exe -m unittest discover -s tools\ingest\tests -t tools\ingest\tests -v

# 只跑本任务新增的 8 个文件（逐个 -p）
.\.venv\Scripts\python.exe -m unittest discover -s tools\ingest\tests -p 'test_boundaries_known_absent.py'
.\.venv\Scripts\python.exe -m unittest discover -s tools\ingest\tests -p 'test_units_evidence.py'
.\.venv\Scripts\python.exe -m unittest discover -s tools\ingest\tests -p 'test_unknowns_extensions.py'
.\.venv\Scripts\python.exe -m unittest discover -s tools\ingest\tests -p 'test_missing_references.py'
.\.venv\Scripts\python.exe -m unittest discover -s tools\ingest\tests -p 'test_fetch_cache_integrity.py'
.\.venv\Scripts\python.exe -m unittest discover -s tools\ingest\tests -p 'test_diff_deletion_boundaries.py'
.\.venv\Scripts\python.exe -m unittest discover -s tools\ingest\tests -p 'test_publish_failure_recovery.py'
.\.venv\Scripts\python.exe -m unittest discover -s tools\ingest\tests -p 'test_fingerprint_rebuild.py'

# 审查修复（P1-3/P1-4/P1-5/P2-3）的校验/发布门槛测试（共用 gate_support.py 合成 fixture）
.\.venv\Scripts\python.exe -m unittest discover -s tools\ingest\tests -p 'test_validation_gates.py'
.\.venv\Scripts\python.exe -m unittest discover -s tools\ingest\tests -p 'test_version_gate.py'
.\.venv\Scripts\python.exe -m unittest discover -s tools\ingest\tests -p 'test_publish_immutability.py'
```

本任务文件在 2026-11-01 的实际结果：**54 tests，全部通过，2 expected failures**（见“已知实现缺陷”）。
`expectedFailure` 是刻意保留的：它们分别对应下面两个已上报的实现 bug，修复后 unittest 会报告
“unexpected success”，届时删除对应装饰器即可。

审查修复新增的 3 个文件（`test_validation_gates.py` 16 个、`test_version_gate.py` 19 个、
`test_publish_immutability.py` 14 个，共用合成 fixture `gate_support.py`）共 **49 tests**，
不写真实数据；其中 3 个用只读的发布库 `data/releases/phase1-dataset-001/data.sqlite` 作为审查
基线（文件不存在时 skip），证明旧的 “44 pass / 0 warn / 0 fail” 不再复现。
2026-11-01 全量套件当时为 **174 tests 全部通过**（含其他任务的用例，数字随套件增长会变）。

## 覆盖矩阵

| workguide §13 检查 | 测试文件 | 关键断言 |
|---|---|---|
| 缺失与 0（0 / false / null / 空描述 / 不存在字段） | `test_boundaries_known_absent.py` | 缺失 `maxPoints` → 列为 NULL 且不出现 0；显式 0 → `known` + raw/csv；`description:""`/null → `absent` 证据；`positive:false` → debuff 且 raw `false` 保留；ailment `maxInstances:0` → `unparsed`（UI 0 的机制语义未建立，不再解释为 uncapped）且 raw 0 保存在 stack_rule；未知绝不填 0 |
| effect→stat 关联（P1-2） | `test_stat_link.py` | 直接 ID 唯一 → `linked` + `stat_id`；specialTag/tags 组合 → `composite` 并保留 base stat 与限定符；无对应属性 → `unknown_namespace`；重复 stat → `ambiguous`；非 JSON → `unparsed`；无 property → `not_applicable`；幂等；`PRAGMA foreign_key_check` 干净 |
| Buff/ailment UI 0 语义（P1-6） | `test_stat_link.py`（Tunklab 映射断言） | `maxInstances:0` → `max_stacks=NULL`、`ui_max_instances=0`、`max_stacks_basis=raw_ui`、evidence `unparsed`、lifecycle/stack_rule 无 `uncapped` |
| known / absent / not_applicable 状态区分 | `test_boundaries_known_absent.py`（`EvidenceStatusFrameworkTests`） | 框架层同时存三种 evidence_status，互不覆盖 |
| 单位转换（秒→毫秒、百分比→比率，原值+规范值双保存） | `test_units_evidence.py` | `duration 2.5` → `duration_ms 2500`，evidence raw `2.5` / normalized `2500` / unit `ms`；`15%` → `0.15` ratio，raw 文本保留；`0%`/`-10%`/`7`/`5-10%`（unparsed）；无二次换算 |
| 未知枚举／未知字段（进 extensions 或 issue，不丢不崩） | `test_unknowns_extensions.py` | 未知 entity kind → 泛化实体 + `extensions.raw` + `unmapped_entity_kind` issue；未知 damage 索引 → 原始索引字符串 `"10"`；未知 stat property 999999 → effects.extensions；未知 ability 字段保留在 `source_records.raw_payload`；unparsed 值 → issue |
| 引用缺失（保留 raw_target_ref + issue，不伪造实体） | `test_missing_references.py` | `has_tree` 目标不存在 → `to_entity_id NULL` + `raw_target_ref`；node 前置不存在 → `node_edges.from_node_entity_id NULL` + raw JSON；relation 两端缺失 → unresolved + raw；不生成 missing-skill/missing-buff/tree 实体；Validator 关系/外键检查通过 |
| 资源损坏（哈希不符/截断拒绝） | `test_fetch_cache_integrity.py` | body 被改（同长度/追加/截断）或 meta.sha256 不符 → `cached_result()` 返回 None；offline `fetch()` 抛 `OfflineError` 而不是复用坏缓存；304 由既有 `test_fetch.py` 覆盖 |
| 更新删除边界（完整枚举 vs 抓取失败） | `test_diff_deletion_boundaries.py` | `full_enumeration=False` → `confirmed=False`、`removed_candidates` 仍计数、note 说明 undetermined；新增行不能确认删除；完全相同的库 `identical=True`；`full_enumeration=True` 的删除确认见 xfail bug |
| 发布失败恢复（非 valid 拒绝、current 不被 partial 替换） | `test_publish_failure_recovery.py` | status `staging`/`partial`/无 `dataset_meta` → `publish_dataset` 抛错且 `current.json` 字节不变、不建 release；复制后哈希不符 → 抛错、旧 current 与旧 release 不变、无 `.tmp` 残留；成功发布 → current 指向新 release；并断言真实项目 `data/current.json` 未被改动 |
| 逻辑指纹（排除时间戳/自增 id，两次重建一致） | `test_fingerprint_rebuild.py` | 同一 fixture 两次建库（记录顺序不同）→ `logical_sha256` 相同、各表指纹与行数相同；篡改 `created_at` 后指纹不变；改内容后指纹变化；完整性/外键干净；重建不膨胀行数 |
| 审查门槛（P1-3/P1-4/P1-5/P2-3） | `test_validation_gates.py` / `test_version_gate.py` / `test_publish_immutability.py` | 拆分子域（idols/blessings/season）；season 无实体且无 issue → fail，有 issue → warn；清空 Buff 关系、去掉 stat 链接、删除 blessing 子域、删除 weaver 来源索引映射均 fail；`claimed_version=9.9.9`、未知版本、模块版本冲突均 fail（仅 `version-compat.json` 带证据的版本对可通过）；同 ID 重发内容不同被拒、内容相同幂等；db 写后/manifest 写前/current 切换前三处故障注入不改变旧 current 与旧 release；partial 写 release 不改 current（`force_current` 才切换）；发布锁串行化并发发布 |

## 已知实现缺陷（expectedFailure，已上报 Lead）

1. **`le_data/report/diff.py`：`full_enumeration=True` 直接崩溃**
   `diff_databases(old, new, full_enumeration=True)` → `KeyError: 'added'`（`_entity_diff` 返回的是
   `added_count`，而第 104 行读 `entity_level["added"]`）。即使修正 key，条件
   `full_enumeration and not added_count == 0` 也把“删除确认”绑在“新增行数”上，语义相反。
   复现测试：`test_diff_deletion_boundaries.DiffDeletionBoundaryTests.test_full_enumeration_confirms_removals`。
   期望：完整枚举成功时，有删除即 `confirmed=True`（建议 `full_enumeration and removed_count > 0`）。

2. **`le_data/normalize/mapping_tunklab.py`：缺失字段被标成 `known`（normalized 为 NULL）**
   `_map_node` 对 `description`/`max_points` 等直接 `_evidence(field, raw.get(...), ...)`；当 raw 为
   None/缺失时得到 `evidence_status='known'` 且 `normalized_value=NULL`（空描述还同时产生 known+absent
   两行）；`player_ability_id`、`ability_id`、`max_stacks` 同样模式。Validator 的
   `field_evidence_known_values` 会因此报 warn。复现测试：
   `test_boundaries_known_absent.KnownAbsentBoundaryTests.test_missing_max_points_should_have_absent_evidence`。

## 备注（不算 bug，但影响测试写法）

- 任务说明假设 `le_data.report.manifest` 有模块级 `DATA_DIR` 常量；实际 `manifest.py` 在
  `publish_dataset` 内部 `from ..paths import DATA_DIR`，因此测试 patch 的是
  `le_data.paths.DATA_DIR` + `manifest.NORMALIZED_DIR`/`manifest.RELEASES_DIR`（已验证有效）。
- **已修复（审查 P1-5/P2-3，2026-11-01）**：`publish_dataset` 现在有显式
  `allow_partial` / `force_current` 参数并返回 `result`（`published` / `partial_released` /
  `idempotent`）；`cmd_publish` 已接线。partial release 默认只写 release、不改 `current.json`，
  `--force-current` 才切换。旧的“`publish_dataset` 拒绝 partial、`--allow-partial` 不可达”
  结论不再成立；新契约见 `reports/discovery/review-validation-publish.md` 与
  `test_publish_immutability.py`。
