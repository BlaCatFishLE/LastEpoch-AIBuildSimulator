# 提取证据（extracted evidence）

本目录把 `knowledge/` 条目中**可复用的结论**与**作者本地归档定位**分开记录，使知识脱离作者的历史案例后仍然可理解、可核对、可复用。

- 生成时间：2026-11-13（公开版发布整理）。
- 覆盖范围：`knowledge/mechanisms/`（机制元数据）、`knowledge/skills/`（技能模板）、`knowledge/definitions/`（共享定义模板）中**含作者本地引用**的 106 个条目。
  没有作者本地引用的条目不需要提取文件：它们本身已在候选树内自洽（规则包、dataset、`docs/evidence/` 证据文档均可直接访问）。
- 索引清单：[_index.json](_index.json)（条目、类别、作者引用数量、结论/限制数量）。

## 1. 约定

| 概念 | 含义 |
|---|---|
| **可复用结论** | 从 knowledge 条目本身提炼的机制/规则结论（标题、聚合口径、适用组件、handler 语义、参数绑定位置等）。每条都带 `source` 字段，指向该条目内的 JSON pointer，可回查核对。**不复制案例数值。** |
| **来源定位** | 规则包 ID、dataset 定位、候选树内的证据文档，以及作者本地归档的位置。 |
| **`archive://` 前缀** | 表示**作者本地归档材料，不随公开仓库分发**。公开版运行时不会读取这类路径；它们只用于说明结论曾经由哪份材料支撑。示例：`archive://examples/<case>/snapshot/input-snapshot.hit.json`、`archive://reports/<case>/<run-id>/result.json`。 |
| **当前实例数值** | 来自**用户自己的存档**：通过 `import` / `prepare` 生成当前 BuildSpec 与 calculation-input 后重新绑定。知识条目不携带任何案例数值，只声明“参数从哪里读”。 |

公开仓库不含 `examples/`、`reports/`、`data/builds/`、`data/raw/`、历史评审与历史文档；这些内容在 `knowledge/**` 与规则包中统一改写为 `archive://<原路径>`。

## 2. 文件字段

每个 `<entry-id>.json`（`entry-id` = `mechanism_id` / `skill_id` / 共享定义 `template_id`）：

| 字段 | 说明 |
|---|---|
| `schema` | 固定 `le-extracted-evidence-v1` |
| `entry_id` / `kind` / `title` / `source_path` | 条目身份与它在 `knowledge/` 的权威文件 |
| `evidence_state` / `implementation_state` | 机制条目的证据/实现状态（词表见 [docs/mechanisms.md](../../mechanisms.md) §1）；技能模板与共享定义没有这两个字段，值记 `null`，并在 `state_note` 说明状态由什么表达 |
| `rule_ref` / `rule_refs` | 主规则引用（文件 + ID/section + locator）及 `verification`（`verified` / `verified_locator` / `declared_none` / `external_dataset` / `failed`） |
| `mechanism_refs` | 仅技能模板：`mappings` 中引用的机制条目及是否存在 |
| `dataset_locators` | 指向 dataset（`data/releases/<dataset-id>/data.sqlite`）的证据定位；dataset 由 `scripts/fetch_dataset.ps1` 获取 |
| `reusable_conclusions` | 可复用结论数组，每项 `{text, source}` |
| `assumptions` | 仅技能模板：条目内的假设及其状态 |
| `author_local_refs` | 原条目中作者本地归档引用的清单，`{pointer, original}`；`pointer` 是原 knowledge 条目内的 JSON pointer，`original` 是原字符串（其中外部路径token 统一带 `archive://` 前缀，去掉前缀即原始仓库相对路径） |
| `limitations` | 已知限制、未覆盖项、open questions、coverage gaps（含 `blocks_full_dps`，如条目提供） |
| `generated_at` | 生成时间（UTC） |

`rule_ref.verification` 的判定方法：在该 `path` 指向的候选树文件内检查 `rules[]` / `effects[]` / 顶层规则段 / `definition.rules` 的 ID 或 section；描述性引用（不是规则 ID）则解析 `locator` 的 JSON pointer。本轮 106 个条目：92 个 `verified`、5 个 `verified_locator`、7 个 `declared_none`（条目显式声明没有可引用的游戏规则 ID）、2 个 `external_dataset`（dataset 实体，需 fetch 的 dataset 文件），**0 个 failed**。

## 3. 怎么用

1. 先用 `knowledge search` / `knowledge show` 或 [docs/mechanisms.md](../../mechanisms.md) 找到条目；
2. 在本目录打开同名 `<entry-id>.json`，先看 `reusable_conclusions` 与 `limitations`；
3. 需要核对实现时按 `rule_ref` 打开 `tools/phase3/rules/` 的规则包与 `handler_ref` 指向的模块；
4. 需要复算时**不要复用作者案例的数值**：按 `knowledge/skills/*.json` 的 `parameter_paths` / `parameter_bindings` 从自己当前存档重新绑定。

`archive://` 路径不可点击、不可读取；它们是“这条结论在作者本地归档中曾经有哪份材料”的说明，不是公开版依赖。

## 4. 与其它入口的关系

- **权威条目**：`knowledge/mechanisms|skills|definitions/*.json`（JSON 是权威源；本目录只是提取视图，不改变条目语义）。
- **人工证据索引**：[docs/mechanisms.md](../../mechanisms.md)（证据分级、状态词表、机制索引、覆盖边界与登记规程）。
- **候选树内证据**：`docs/evidence/reverse-engineering/`、`docs/evidence/{ballista,flame-reave,void-cleave}/`（这些文件随公开仓库分发）。
- **规则版本审计**：[version-audit.json](version-audit.json)（本轮保留/删除的规则包与定义版本及依据）。
- **覆盖边界与已知缺口**：[coverage-boundaries.json](coverage-boundaries.json)。

## 5. 维护

- 新增或修改机制：先更新 `knowledge/` 条目（`le.ps1 knowledge rebuild`），再按上述字段补一份本目录提取文件；`reusable_conclusions[].source` 必须能在条目内找到对应字段，不允许写入条目里没有的数值或结论。
- 修改规则包/定义后：同步 `rule_ref` 与 `verification`，并更新 `docs/mechanisms.md` 对应行。
- 本目录不存数值基准；任何“当前构筑 = 多少”的结果必须由用户自己的存档重新计算得到。
