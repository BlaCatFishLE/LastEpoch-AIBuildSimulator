"""共享计算块适配器（P2）—— ``le_calc.mechanics``。

目的（实施方案 §6.2）：BH 与 FR hit 两条数值链各自实现了同一批数学算子。
P2 先把它们包成**签名统一、策略显式**的适配器；只有在回归逐位一致的前提下
才合并生产调用点，不因“去重”改变任何数值。

本包约定：

* 算子只做数学/投影，不读规则文件、不读当前案例、无模块级可变状态；
  snapshot/ruleset/scenario 由调用方显式传入（``le_calc.mechanics.registry``
  记录每个入口与集成状态）。
* 模型间差异通过数据类策略参数表达（穿透模式、负抗性策略、DR 来源、
  标签未知策略、crit capability……），不在函数体里按模型名分支。
* P2 仅 ``target_side`` 接入生产（``damage`` / ``hit_damage``）；其余适配器
  先交付等价性测试，接入清单见 :func:`registry.integration_report`。
* 规则/证据的权威源仍是 ``tools/phase3/rules/`` 与 ``docs/evidence/``；
  本包不复制游戏数值。

模块速览::

    tag_match           标签适用性（BH metadata 忽略 vs FR 子集限制）
    increased_aggregate 加算池
    more_groups         more 分组乘积（group 必须来自显式机制/规则字段）
    crit_expected       avg crit（BH capability=False）
    target_side         目标侧结算（P2 已接入生产）
    frequency           冷却/攻速→施放速率与窗口骨架（事件流语义不合并）
    ledger              contribution→行投影薄封装
    registry            适配器声明 / 集成状态 / 模型能力
"""

from __future__ import annotations

from . import (
    crit_expected,
    frequency,
    increased_aggregate,
    ledger,
    more_groups,
    registry,
    tag_match,
    target_side,
)

__all__ = [
    "crit_expected",
    "frequency",
    "increased_aggregate",
    "ledger",
    "more_groups",
    "registry",
    "tag_match",
    "target_side",
]
