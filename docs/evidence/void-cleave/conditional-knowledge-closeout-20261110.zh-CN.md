# 条件机制知识收尾与文档审查（2026-11-10）

## 结论

原先没有必须停止知识收尾的有效要求；任务未完成的责任仍在执行者。但现行文档中确有过时限制与终点缺失，容易造成误导，已按用户要求纠正。

## 找到的具体文档问题

1. `knowledge/README.md` 原规则 6 写“登记范围以当前已验收案例（BH、FR）为限”，错误排除了新构筑；现改为当前任务任意构筑，历史案例同等。
2. 同文件原规则 5 写“只有 implementation_state=validated 且适用的条目参与自动绑定”，与现行领域代码允许 validated/reusable 不符，也容易误解成未完整测试不能入库。现明确登记、实现与证据状态独立；假设/未解决也可登记，不虚报 validated。
3. `docs/bd-reuse-pipeline-v1-implementation.zh-CN.md` 的新增机制路径仍写“测试后登记”，与用户的最小验证要求冲突；已取消这个登记前置条件。
4. 根入口与 workflow 的日常步骤止于结果/网页与测试范围，没有“结果先交付后仍要完成知识收尾”的结束条件。现增加专门条款，避免把先输出结果理解成任务已结束。
5. 原 AGENTS 的“发现未知机制记账”要求规则、证据、mechanisms.md，但没有直接要求对已实现的案例临时规则更新正式知识 JSON。现覆盖新增及修正机制，并明确只写 profile 或文档不算共享沉淀。

这些冲突本应由 agent 依据用户更高优先级指令主动解决，不能作为漏做工作的理由。

## 本轮完成的知识

新增 14 条共享机制，修订既有 Ambition、Haste 两条，并给旧 Frenzy 复合条目补充边界，避免把特定技能的附加火伤当成通用 Frenzy。

共享规则配方：`tools/phase3/rules/shared-conditional-mechanisms-v1.json`。
配方去掉旧槽位/实例路径；按稳定装备/词条/技能节点身份和效果语义匹配，再从当前 matched_instances 读取值与点数。常量来自规则和 dataset，不保存当前 4/5/3 点的合计倍率。

| 条目 | 操作 |
|---|---|
| `buff-frenzy-speed` | created · Frenzy 攻击速度与效果缩放 |
| `item-sword-catcher-frenzy-added-melee` | created · Sword Catcher 的 Frenzy 附加近战伤害 |
| `fr-effect-ambition-stacks` | updated · Throne of Ambition 层数增伤 |
| `fr-effect-haste` | updated · Haste 移动速度及效果缩放 |
| `buff-incisive-rage` | created · Incisive Rage 暴击率与 Deadly Strike |
| `buff-crescendoing-rage-main-hit-cycle` | created · Crescendoing Rage 主技能命中计数 |
| `buff-blood-rage-common-speed` | created · Blood Rage 状态的公共攻速和移速 |
| `item-vaions-chariot-movement-to-damage` | created · Vaion's Chariot 移速转加算伤害 |
| `skill-hellish-chasm-next-fire` | created · Hellish Chasm 后续火焰技能增伤 |
| `skill-nether-coating` | created · Nether Coating 攻速与致盲增伤 |
| `skill-void-supremacy` | created · Void Supremacy 对减速目标增伤 |
| `skill-dark-duellist` | created · Dark Duellist 准备反击时的全局增伤 |
| `skill-abyssal-retribution` | created · Abyssal Retribution 后续主技能增伤 |
| `skill-entropic-blows` | created · Entropic Blows 的 Time Rot 层数增伤 |
| `skill-vorpal-force` | created · Vorpal Force 的 Bleed 层数增伤及上限 |
| `target-critical-vulnerability` | created · Critical Vulnerability 目标被暴击率 |

## 范围与最小核对

知识记录负责检索与规则/处理器入口；共享配方不是新的伤害执行器。本轮没有增加自动配方物化器，调用方仍须按当前构筑的稳定来源生成 profile；已运行的实例 profile 保持不变，避免重复注入贡献或更改现有结果。

未确定的计数相位、Deadly Strike 合成与持续伤害继承保留 assumed 和限制；辅助技能增益依然是固定已获得状态，不模拟获取成本。Void Supremacy 和 Abyssal Retribution 配方限定当前已支持的后续技能，不能作用于任意主技能。

本轮仅按正常加载重建知识索引、做代表性稳定 ID 检索及规则/handler 引用核对；不重跑数值模拟、不打开网页、不运行完整回归。
