# fixtures（合成样例，非真实来源数据）

本目录只放 **人工构造** 的回归样例，用于验证失败模式与边界行为（工作指导书 §13）。
文件名统一以 `synthetic_` 开头；**不是** Tunklab / LE Tools / 本地游戏的真实响应，不得当作覆盖率或数据质量证据。

| 文件 | 用途 | 覆盖 |
|---|---|---|
| `synthetic_boundary_records.jsonl` | 0 / false / null / 空字符串 / 缺失字段、单位换算、未知枚举与未知字段 | workguide §13「缺失与 0」「单位转换」「未知枚举／字段」 |
| `synthetic_reference_records.jsonl` | 关系目标不存在、节点前置不存在、树 ID 不存在 | workguide §13「引用缺失」 |

约定：

- `source` 固定为 `tunklab`，`source_snapshot_id` 使用 `synthetic-snap-*`，`resource_name` 与文件名一致，
  `notes` 标注 `SYNTHETIC fixture - not real Last Epoch data`；
- 记录形状与 `SourceRecord`（`source`、`source_snapshot_id`、`resource_name`、`entity_kind`、
  `source_scope`、`source_id`、`locator`、`raw`、`parse_status`、`notes`）一致，可直接 `SourceRecord.from_dict`；
- `raw` 使用真实 Tunklab 适配器的字段名（`nodeName`、`stats[].property`、`duration`、`maxInstances`、
  `baseDamage.damage` 等），但值是为了触发边界而编造的；
- 各 `source_scope` 是作用域字符串（如树 ID `"42"`、`"77"`），不是真实技能树。

对应测试：

- `tests/test_boundaries_known_absent.py`
- `tests/test_units_evidence.py`
- `tests/test_unknowns_extensions.py`
- `tests/test_missing_references.py`

相关实现（只读，不在此目录修改）：`tools/ingest/src/le_data/normalize/mapping_tunklab.py`、
`tools/ingest/src/le_data/normalize/base.py`。
