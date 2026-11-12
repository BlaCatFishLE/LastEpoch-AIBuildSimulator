# knowledge/ — 机制与技能复用知识库

日期：2026-11-11（最近更新；原 2026-11-07）。状态：**现行知识登记与查询入口**；本目录为 JSON 权威源，索引与 Markdown 页由脚本生成；`knowledge/definitions/` 为 v3/v4 共享定义与候选目录（`index.json`）。
依据：[workflow-interface 契约](../docs/contracts/workflow-interface.md) §9；
证据分级与登记规程见 [docs/mechanisms.md](../docs/mechanisms.md)。
可复用结论与作者归档定位的提取视图见 [docs/evidence/extracted/](../docs/evidence/extracted/README.zh-CN.md)。

## 1. 权威源与生成物

| 对象 | 位置 | 提交 | 说明 |
|---|---|---|---|
| 机制元数据 | `knowledge/mechanisms/<mechanism_id>.json` | 是 | **一条机制一个文件**，单个对象，schema `le-knowledge-mechanism-v1` |
| 技能模板 | `knowledge/skills/<skill_id>.json` | 是 | 一个模板一个文件，schema `le-knowledge-skill-template-v1` |
| 共享定义候选目录 | `knowledge/definitions/index.json` | 是 | v3/v4 模板候选的元数据/能力/适用条件；模板正文 `knowledge/definitions/*.json` 才是计算权威 |
| 记录 schema | `knowledge/schemas/*.json` | 是 | 上述两类条目的字段定义 |
| Markdown 索引 | `knowledge/INDEX.generated.md` | 是（生成） | **脚本生成，禁止手改**；含 GENERATED 头、`source_digest` 与生成命令 |
| 查询索引 | `data/cache/knowledge.sqlite` | 否 | 可删除重建，不作为知识源 |

规则：

1. 本目录只保存**元数据、选择条件、参数绑定说明、规则/证据引用**。可执行规则仍在 `tools/phase3/rules/`，
   正式证据仍在 `docs/evidence/`，**不在此抄写第二份数值**。
2. 机制 ID 稳定、小写、优先使用机制/领域前缀（如 `buff-`、`skill-`、`item-`、`target-`）；已发布的旧 ID 保持兼容；
   发布后不改名，语义变化递增 `revision`。
3. `selectors` 的规范 ID 由 `kind:namespace:id` 组合（如 `node:fr11mv:16`、`ability:letools:fr11mv`）；
   `namespace` 是来源命名空间，`ids` 是余段。引擎会为部分类别生成 `kind:id` 兼容选择器
   （`ability:fr11mv`、`stat:penetration` 也能命中）。查询时按规范 ID 书写。
4. `more_group` 必须显式来自机制定义并给出来源定位，禁止仅凭词条文本里的 “more” 一词推定。
5. `evidence_state`（confirmed/derived/assumed/unresolved）与 `implementation_state`（draft/validated/reusable/not_implemented）
   独立；允许先登记 assumed/unresolved 或 draft/not_implemented。当前领域入口允许适用的 validated/reusable 实现参与绑定，但仍须核对范围、来源身份与证据限制；无完整回归不阻止登记，也不得虚报 validated。
6. 登记范围为当前任务涉及的任意构筑；所有历史案例地位相同，不限制为建立流程时的少数案例。不额外扩展为全游戏机制清洗。

## 2. 命令

```powershell
.\scripts\le.ps1 knowledge search --query "<当前技能或机制>"      # 短摘要
.\scripts\le.ps1 knowledge search --query "node:<当前技能ID>:<节点ID>"
.\scripts\le.ps1 knowledge show --id <mechanism-id>
.\scripts\le.ps1 knowledge rebuild                            # 从 JSON 重建 sqlite + INDEX.generated.md
```

查询默认只回短摘要；详情按 ID 获取。查询接口可绕过编排独立调用，不依赖当前案例或聊天上下文。
逐条匹配结果需记录"哪条查询项、精确还是候选、适用性判定"，规则见 workflow-interface §5.1。

## 3. 目录结构

```text
knowledge/
  README.md                     # 本文件（人工维护）
  INDEX.generated.md            # 脚本生成，勿手改
  schemas/mechanism-v1.json
  schemas/skill-template-v1.json
  schemas/skill-template-v2.json   # 原生定义入口（le-skill-template-v2，生成统一计算计划）
  mechanisms/<mechanism_id>.json
  skills/<skill_id>.json
  definitions/index.json + <skill>-v3|v4.json   # 共享定义候选与模板正文
```

## 组件复用 V1

共享记录使用 `reuse_mode=shared`，按整个 BD 的稳定组件 ID 查询；`case_scope` 仅保留验收案例。案例私有项用 `case_bound`，未迁移记录保留旧行为。当前数值取自 matched_instances 或现有输入/条件适配器，历史案例路径只作证据。具体接口见 [组件知识契约](../docs/contracts/component-knowledge-v1.md)。

## 知识收尾（结果先交付，任务结束前完成）

本轮新增、修正或临时写进案例 profile 的可复用机制，完成实现后必须收尾：先查已有条目，复用或更新；没有则在 `knowledge/mechanisms/` 登记稳定 ID、当前参数绑定、规则/处理器与证据入口，并把通用规则从案例实例配置中分离。确属案例私有的输入可保留在案例中，但要写明不能共享的原因。普通属性已有映射时不强制逐行新建机制文章。

证据不完整可登记为 assumed/unresolved，尚未实现可登记 draft/not_implemented；不能把“没有全部实测”“没跑回归”或“先输出网页”当作不登记的理由。实现可复用与证据确认是两个独立维度，不得为入库伪造 validated 状态。

最小收尾只需：条目随正常加载可读、重建知识索引、本轮代表性稳定 ID 能命中。不要重跑导入、计算、网页或全套测试，不逐一巡查其它历史案例。已有完整共享知识且本轮未新增/修改规则时直接复用即可。

结果与网页可以先给用户；“先交付”不表示任务终止。最终说明本次工作完成前，应明确本轮机制是已复用、已更新、已新增，还是保留为案例私有并说明原因。仅保存 case profile、报告 JSON 或 mechanisms.md 链接，不等于已完成共享知识沉淀。

