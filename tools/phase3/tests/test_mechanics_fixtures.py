"""共享真实案例 fixture（P2 等价性测试用，非测试用例）。

读取仓库内**冻结**的两案例输入（snapshot / ruleset / scenario），不做任何
改写；BH 的生产场景按 ``report.build_result`` 的目标块构造方式复现（目标
抗性取 ruleset.target_side.base_resistances，DR 由 ``monster_dr.describe``
注入），这样测试跑的就是真实生产数值路径，而不是自造数组。

注意：``snapshot``/``ruleset`` 对象被缓存，测试中视为只读；需要修改的
scenario 每次返回新拷贝。
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path

from le_calc import model, monster_dr
from le_calc.hit_ruleset import load_ruleset as load_hit_ruleset
from le_calc.ruleset import load_ruleset

REPO_ROOT = Path(__file__).resolve().parents[3]

from _author_assets import require_author_assets  # noqa: E402

require_author_assets()

BH_SNAPSHOT_PATH = REPO_ROOT / "examples/black-hole/snapshot/input-snapshot.affix-db.json"
BH_RULESET_PATH = REPO_ROOT / "tools/phase3/rules/ruleset-bh-stage1-v4.json"
BH_SCENARIO_PATH = REPO_ROOT / "examples/black-hole/scenario/conditional-scenario.json"
FR_SNAPSHOT_PATH = REPO_ROOT / "examples/flame-reave/snapshot/input-snapshot.hit.json"
FR_RULESET_PATH = REPO_ROOT / "tools/phase3/rules/ruleset-fr-hit-stage1-v2.json"
FR_SCENARIO_PATH = REPO_ROOT / "examples/flame-reave/scenario/conditional-scenario.json"


def read_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


@lru_cache(maxsize=1)
def _bh_snapshot() -> model.Snapshot:
    return model.Snapshot.from_dict(read_json(BH_SNAPSHOT_PATH))


@lru_cache(maxsize=1)
def _bh_ruleset():
    return load_ruleset(BH_RULESET_PATH)


@lru_cache(maxsize=1)
def _fr_snapshot() -> model.Snapshot:
    return model.Snapshot.from_dict(read_json(FR_SNAPSHOT_PATH))


@lru_cache(maxsize=1)
def _fr_ruleset():
    return load_hit_ruleset(FR_RULESET_PATH)


def bh_snapshot() -> model.Snapshot:
    """真实 BH 快照（只读）。"""
    return _bh_snapshot()


def bh_ruleset():
    """真实 BH ruleset v4（只读）。"""
    return _bh_ruleset()


def fr_snapshot() -> model.Snapshot:
    """真实 FR hit 快照（只读）。"""
    return _fr_snapshot()


def fr_ruleset():
    """真实 FR ruleset v2（只读）。"""
    return _fr_ruleset()


def fr_scenario() -> dict:
    """真实 FR 场景（每次返回拷贝，测试可安全修改）。"""
    return copy.deepcopy(read_json(FR_SCENARIO_PATH))


def bh_production_scenario(monster_level: int = 100) -> dict:
    """按 ``report.build_result`` 的方式构造 BH 生产场景（目标块 + DR 注入）。"""
    ruleset = bh_ruleset()
    scenario = copy.deepcopy(ruleset.scenario)
    info = monster_dr.describe(monster_level)
    scenario["target"] = {
        "target_id": f"{ruleset.target_side.get('scenario_id', 'scenario')}-target",
        "resistances": dict(ruleset.target_side.get("base_resistances", {})),
        "negative_resistance_allowed": bool(
            ruleset.target_side.get("negative_resistance_allowed", True)
        ),
        "monster_level": info["monster_level"],
        "monster_damage_reduction": info["monster_damage_reduction"],
        "monster_dr_factor": info["monster_dr_factor"],
    }
    return scenario


def merge_target_like_hit(ruleset_document, scenario_target: dict) -> dict:
    """复现 ``hit_damage`` 的 target 合并（后层覆盖；跳过注释键）。"""
    merged = dict((ruleset_document.data.get("target") or {}))
    for key, value in (scenario_target or {}).items():
        if key in ("comment", "note", "notes"):
            continue
        merged[key] = value
    return merged


__all__ = [
    "BH_RULESET_PATH",
    "BH_SCENARIO_PATH",
    "BH_SNAPSHOT_PATH",
    "FR_RULESET_PATH",
    "FR_SCENARIO_PATH",
    "FR_SNAPSHOT_PATH",
    "REPO_ROOT",
    "bh_production_scenario",
    "bh_ruleset",
    "bh_snapshot",
    "fr_ruleset",
    "fr_scenario",
    "fr_snapshot",
    "merge_target_like_hit",
    "read_json",
]
