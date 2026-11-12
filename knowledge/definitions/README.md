# 可执行技能定义（knowledge/definitions）

本目录存放可执行 `le-skill-template-v3` 定义，与 `knowledge/skills/`（知识索引中的 `le-knowledge-skill-template-v1`
说明模板）职责不同：

- `knowledge/skills/`：知识/证据/参数路径说明，参与知识索引；
- `knowledge/definitions/`：生产可执行定义，含共享语义、selector 与证据入口；不参与知识索引，
  不写当前构筑 roll/节点总值/实例 ID。

当前（完整清单元数据见 `index.json`；`preferred` 表示同技能多版本中的当前候选）：

- `black-hole-v3/v4.json`、`fr-flame-reave-v3/v4.json`、`void-cleave-fire-main-v3/v4.json`：
  同一技能的两个 plan 版本（v3 → plan-v1/v2/v3 执行语义，v4 → plan-v4 执行语义），由 selector 按技能/职业/标签择一；
  不是“v4 取代 v3”的废弃关系，两个版本都登记为候选，不能仅凭版本后缀删除 v3。
- `ballista-armed-construction-v4.json`：Ballista（ba1574）首个原生定义，当前唯一 Ballista 候选。

当前实例经 `binding_selector` 从真实 calculation-input/BuildSpec 解析，定义文件本身不含案例数值。

schema 见 `tools/phase3/schemas/skill-template-v3.json`；绑定实现见
`tools/phase3/src/le_calc/definitions_v3.py`；契约见 `docs/contracts/definitions-v3.md`。
