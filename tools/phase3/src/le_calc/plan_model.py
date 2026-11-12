"""统一计划装配的独立中间表示（不是可序列化 profile）。"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ResolvedModel:
    """两个入口共同归一的中间表示：共享定义 + 当前实例 + 场景。

    ``evaluations`` 只用于 plan-v2（FR/BH 多评估）：每项形如
    ``{"evaluation_id", "label", "metrics", "primary", "scenario", "scenario_path",
    "scenario_sha256"}``，其中 ``scenario`` 是已经通过模板控件校验的场景文档。
    plan-v1 保持空列表，序列化后不出现该字段。
    """

    origin: str
    ability_id: str
    definition: dict[str, Any]
    controls: list[dict[str, Any]]
    scenario: dict[str, Any]
    instance: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    evaluations: list[dict[str, Any]] = field(default_factory=list)

    def assembler_view(self) -> dict[str, Any]:
        """共享计划装配器读取的视图；只存在于内存，不写 profile 文件。"""

        view = copy.deepcopy(self.definition)
        view.update(
            {
                "ability_id": self.ability_id,
                "controls": copy.deepcopy(self.controls),
                "controls_by_key": {str(c.get("key")): c for c in self.controls},
                "monster_level": self.scenario.get("monster_level"),
                "resistance": self.scenario.get("resistance"),
                "scenario": copy.deepcopy(self.scenario),
                "origin": self.origin,
                "instance": copy.deepcopy(self.instance),
                "evaluations": copy.deepcopy(self.evaluations),
            }
        )
        return view

    def validate(self) -> None:
        """v3 入口已做结构/selector 校验；profile 入口已由 binding.validate_profile 校验。"""
        if self.origin not in ("profile_v2", "definitions_v3"):
            raise ValueError(f"未知模型来源：{self.origin!r}")
        if not self.ability_id:
            raise ValueError("模型缺少 ability_id")
        if not isinstance(self.controls, list) or not self.controls:
            raise ValueError("模型 controls 必须非空")
        if not isinstance(self.definition, dict) or not self.definition:
            raise ValueError("模型 definition 必须非空")
        if self.evaluations:
            for index, evaluation in enumerate(self.evaluations):
                if not isinstance(evaluation, dict) or not evaluation.get("evaluation_id"):
                    raise ValueError(f"evaluations[{index}] 缺少 evaluation_id")
                if not isinstance(evaluation.get("scenario"), dict):
                    raise ValueError(f"evaluations[{index}].scenario 必须是场景对象")
