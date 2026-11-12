# Ballista beta_5 本轮机制核对（2026-11-12）

## 输入与证据

用户选择 Ballista，不更换主技能。ba1574 / Rogue / Falconer，来自当前真实导入的专精和目录。BuildSpec revision 为 rev-c83726fd2c25625d；当前数据版本为 phase1-dataset-003 / 1.5.2，不由角色存档推断已经验证游戏最新版本。

本轮只读提取证据：ballista-catalog-20261112.json、ballista-runtime-and-node-values-20261112.json、explosion-and-trap-components-20261112.json（均在本目录）。这三份证据保留原字段与来源locator。

## 区分三类事件

- 召唤技能 ba1574：召唤100053实体，基础存续10s、上限2；这不是一个有直接伤害的玩家命中。
- 实体弩箭 entity_bbl7_57：基础40 Physical，200%附加伤害效能，基础暴击5%/200%；不能把附加效能显示200误当200倍。
- Explosives ability_gen327：基础50 Fire、250%效能；另一同名能力 ability_gen237 为40 Fire/200%，必须核对触发引用，不按显示名择一。
- 当前已分配 Explosive Delivery 与 Armed Construction；它们分别关联箭矢命中与到期/销毁，不能把两者共用一个频率。

## 当前来源的关键语义

Ballista 31点；Armed Construction4、Shared Enhancements3、Perfect Aim4、Flaming Shots4、Explosive Delivery1。这里点数只是本次实例记录，共享知识始终从当前输入读点数。

玩家伤害属性共享比例、玩家暴击属性共享比例、Minion专用属性、Falcon专用属性需要四种不同绑定；例如装备的Falcon出血共享不能自动作用Ballista，Forged Weapons手套共享也不能跨召唤物生效。槽位上的平坦Bow附加与玩家技能伤害不能直接计入全部minion。

本轮正式登记8条知识，均为derived/not_implemented，不声称已有Ballista DPS执行器。普通属性仍复用既有词条映射，无需逐行建立文章。当前dataset导出的189mods没有未解析输入，不等于189mods对Ballista均有已实现执行语义。

## 外部对照（只作描述/身份核对）

- 官方 Rogue Skill Tree：https://support.lastepoch.com/hc/en-us/articles/46362944419611-Rogue-Skill-Tree （页面更新2026-03-26）。本轮仅核对共享/暴击共享、两种爆炸和药剂条件的描述，存在版本/个别节点说明差异，不回写1.5.2数值。
- 官方 Other Skills：https://support.lastepoch.com/hc/en-us/articles/46361981143579-Other-Skills 。区分两个同名Explosives并核对Ballista子技能50 Fire/250%组件。
- 官方 Armed Construction 修复公告：https://forum.lastepoch.com/t/upcoming-bug-fix-to-ballista/68776 。确认其作用在到期/销毁爆炸，area与radius历史曾有差异，禁止照旧攻略半径公式计算。

## 尚需执行能力

1. actor池隔离与显式属性转移（带原来源、目标actor、比例、operation、ownership）。
2. owner/node/装备对actor具体行为组件的绑定，不用Minion标签自动继承玩家全池。
3. summon/attack/expire/replace/trigger各时钟及同时事件顺序。
4. 单体命中口径、装备条件、触发频率与异常继承；固定场景假设可显示，但不能用无定义频率输出总DPS。

没有新建Ballista专用计算器，没有给缺失DPS填0；本次准备状态是needs_research。游戏内实测、完整回归、网页矩阵均未执行。
