# Throne of Ambition 编辑器固定层数接入

日期：2026-11-13。性质：既有机制复用与绑定补齐，不是新增游戏内实测。

## 本次输入与缺口

用户指定任务：`data/editor-workspace/dc12317939ab41b0bfef26fa14d60657/evaluations/2ee326c497364b72933a001538e88e6e/research-task.json`。

冻结配置为 `black-hole-beta33-native-20261111`，主技能 `bh2`，100 级 Boss、目标抗性 0、30 秒、`ambition=20`。冻结文档与当前编辑器配置版本均为 11，其他 Buff 按该快照保留。知识解析已命中 `bh-cond-throne-of-ambition` 和 `fr-effect-ambition-stacks`，但当前 Black Hole 定义未绑定 `ambition`，预检因 `unsupported_active_controls` 中断。缺口是当前定义接线，不是未知神像或缺少第二套 DPS 模型。

## 当前来源事实

本次 `calculation-input.json` 的 `idols.2` 稳定定义为 `item:letools:uniqueList.uniques:211`。其真实说明行包括：

- `idols.2#unique_description#1`：每层 2% more Fire Damage。
- `idols.2#unique_description#2`：每层 2% more Cold Damage。
- `idols.2#unique_description#5`：20 Maximum Stacks of Ambition。
- 其他说明保留 Boss/稀有命中、1 秒获取冷却、4 秒未获得新层清空，以及护甲收益。

当前源数组中 `#5` 排在 `#4` 前，不能以数组顺序替代真实说明行身份。主来源数据为 `data/releases/phase1-dataset-003/data.sqlite` 中 unique 211 和 `buff:letools:UIBuffs:79`；既有语义记录见 `tools/phase3/rules/conditional-effects-bh-stage1-v1.json` 的 `idol.throne_of_ambition`。unique tooltip 已明确层内不互乘，因此 20 层使用 ×1.40。

## 实现与范围

新增通用装备 Buff 绑定器 `tools/phase3/src/le_calc/conditional_item_buffs.py` 和规则 `tools/phase3/rules/shared-conditional-item-buffs-v1.json`，由现有共享条件入口加载。核对当前物品定义、Fire/Cold 与层数文本后，输出标准 profile effects，继续复用现有 `effect_binding`、计划 `stack_scale` 和统一执行器。没有新增伤害公式或技能专用执行器。

层数为整数 0–20，0 表示无增伤。同一 Buff 的多个神像来源只绑定一次；显式模板已有同名层数效果时不重复注入。固定层数表示已获取且在观察期维持，不模拟 Black Hole 本体获取层数、其他技能命中成本、冷却或清空；当前目标类型不再次清零已声明的 Buff。护甲保留在装备说明，不进入伤害。

Fire/Cold 两个作用域分别绑定。Ignite 继承按既有假设计入，尚未游戏实测确认，规则、贡献和计划/结果保留 assumed 与范围说明。相关文件及绑定器加入编译和报告依赖指纹，避免旧计划错误复用。

## 本轮定向验证

`tools/phase3/tests/test_conditional_item_buffs.py` 的两个检查已通过：当前来源身份/说明行索引、0 与 20 层的作用值、Fire/Cold 作用域、越界类型/层数拒绝、来源缺失/文本不匹配、重复来源与重复绑定去重，以及规则/实现指纹。未运行全仓或历史案例矩阵。

真实输入链与新报告引用在本次研究任务的独立完成记录中追加，不回写原任务快照。


## 当前真实链完成记录

本轮集成对照发现了预检/结构门禁无法识别的第二处缺口：DoT/充能编译分支未装配标准 profile effects，导致 0 与 20 层结果相同。中间求值 `1d24de227ded417bacda575b7224d1d1` 已标为 failed，不可作为成功报告，原数值产物保留。随后在通用编译入口补齐效果装配、动态叠层、来源错误拒绝与效果账本，未另建技能计算器。

最终新求值为 `336a0cc76e3944319312bf84a32c5316`，30 秒预计 DPS 为 8,581,048.18784884；当前执行计划的 0/20 层对照为 1.40 倍。Fire/Cold 两条贡献均为 0.40，并具有实际应用组件；报告快照和执行场景均保留 20 层，统一结果验证通过。当前配置数值未修改；保存版本号可以变化，语义指纹与原任务相同。

仍有两条既有 `per_attribute_unresolved` 被动缩放缺口，不属于本任务 Ambition 绑定修复；当前结果继续标 partial。未运行历史案例、全仓测试或网页矩阵。完整引用与哈希见 `docs/evidence/conditional/throne-of-ambition-editor-validation-20261113.json`。
