# -*- coding: utf-8 -*-
"""测试工具：临时知识库 fixture（不依赖仓库里正在登记的实时条目数）。

fixture 只复制**契约 schema**（``knowledge/schemas/*.json``，Lead 维护的条目定义），
条目内容全部在临时目录里现造，因此测试数量与真实登记进度解耦。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMAS_DIR = REPO_ROOT / "knowledge" / "schemas"
SCHEMA_FILES = ("mechanism-v1.json", "skill-template-v1.json")


def write_json(path: Path, document: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def mechanism(
    mechanism_id: str,
    *,
    selectors: list[dict[str, Any]] | None = None,
    aliases: list[str] | None = None,
    components: list[str] | None = None,
    tags: list[str] | None = None,
    conditions: list[str] | None = None,
    required_nodes: list[str] | None = None,
    rule_ref: dict[str, Any] | None = None,
    handler: dict[str, str] | None = None,
    evidence_state: str = "confirmed",
    implementation_state: str = "validated",
    title_zh: str | None = None,
    title_en: str | None = None,
    revision: int = 1,
    open_questions: list[str] | None = None,
    case_scope: list[str] | None = None,
    evidence_refs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema": "le-knowledge-mechanism-v1",
        "mechanism_id": mechanism_id,
        "revision": revision,
        "title": {"zh": title_zh or f"{mechanism_id} 标题"},
        "selectors": selectors
        if selectors is not None
        else [{"kind": "node", "namespace": "node", "ids": ["test:1"]}],
        "applicability": {
            "components": components if components is not None else ["hit"],
            "tags": tags if tags is not None else [],
            "conditions": conditions if conditions is not None else [],
            "required_nodes": required_nodes if required_nodes is not None else [],
        },
        "rule_ref": rule_ref
        if rule_ref is not None
        else {
            "kind": "ruleset_rule",
            "ref": f"rule-{mechanism_id}",
            "path": "tools/phase3/rules/ruleset-fixture.json",
        },
        "evidence_state": evidence_state,
        "implementation_state": implementation_state,
        "evidence_refs": evidence_refs
        if evidence_refs is not None
        else [{"kind": "dataset", "locator": "data/releases/fixture/data.sqlite#fixture"}],
        "validation_refs": [f"test_{mechanism_id}"],
        "research_notes": {"open_questions": open_questions or []},
        "case_scope": case_scope if case_scope is not None else ["fixture-case"],
    }
    if aliases:
        document["aliases"] = list(aliases)
    if title_en:
        document["title"]["en"] = title_en
    if handler:
        document["handler_ref"] = handler
    return document


def skill_template(
    skill_id: str,
    *,
    ability_id: str = "fr11mv",
    mappings: list[dict[str, str]] | None = None,
    title_zh: str | None = None,
    title_en: str | None = None,
    components: list[dict[str, Any]] | None = None,
    calculation_blocks: list[dict[str, str]] | None = None,
    damage_tags: list[str] | None = None,
    schedule: dict[str, Any] | None = None,
    unsupported: list[dict[str, Any]] | None = None,
    case_scope: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "le-knowledge-skill-template-v1",
        "skill_id": skill_id,
        "revision": 1,
        "ability_id": ability_id,
        "title": {"zh": title_zh or f"{skill_id} 标题", **({"en": title_en} if title_en else {})},
        "class": "Sentinel",
        "mastery": "Spellblade",
        "damage_tags": damage_tags if damage_tags is not None else ["fire", "melee"],
        "model_id": "hit",
        "components": components
        if components is not None
        else [
            {
                "component_id": "hit_main",
                "damage_type": "fire",
                "kind": "hit",
                "tags": ["melee"],
                "base_damage": 2,
                "added_sources": ["fixture"],
                "scaling": 4,
                "crit": {"supported": True},
                "notes": "fixture component",
            }
        ],
        "calculation_blocks": calculation_blocks
        if calculation_blocks is not None
        else [{"block": "more_groups", "module": "le_calc.hit_damage", "symbol": "more_product"}],
        "mappings": mappings if mappings is not None else [],
        "parameter_paths": [
            {
                "parameter": "base_damage",
                "from": "examples/fixture/inputs/calculation-input.json #/skills/0/base_damage",
                "unit": "damage",
                "required": True,
            }
        ],
        "schedule": schedule
        if schedule is not None
        else {
            "driver": "cooldown_limited",
            "cooldown_s": 3.0,
            "hit_weights": [1.0, 0.5, 0.5, 0.5],
            "hits_per_cast": 4,
            "windows_s": [5, 30, 120],
            "fractional_casts": "allowed",
        },
        "supported_interactions": ["critical_hit"],
        "unsupported": unsupported
        if unsupported is not None
        else [{"item": "技能等级缩放", "reason": "dataset levelScaling=[]", "blocks_full_dps": True}],
        "assumptions": [
            {
                "assumption_id": "fixture-rotation",
                "status": "assumed",
                "note": "循环平均系数（fixture）",
                "evidence_locator": "docs/fixture-derivations.md",
            }
        ],
        "capability_ref": {
            "model_id": "hit",
            "pages": ["player-report"],
            "recalculation": "supported",
        },
        "validation_refs": [f"test_{skill_id}"],
        "case_scope": case_scope if case_scope is not None else ["fixture-case"],
    }


DEFAULT_MECHANISMS: dict[str, dict[str, Any]] = {
    "fixture-ignite": mechanism(
        "fixture-ignite",
        selectors=[
            {"kind": "node", "namespace": "node", "ids": ["test:10", "node:test:11"]},
            {"kind": "stat", "namespace": "stat", "ids": ["ignite_damage"]},
        ],
        aliases=["Ignite Scaling", "点燃增伤池"],
        title_zh="点燃测试机制",
        title_en="Ignite test mechanism",
        components=["ignite", "dot"],
        tags=["fire", "ignite"],
        conditions=["ignite_active"],
        required_nodes=["node:test:10"],
        handler={"module": "le_calc.ignite", "symbol": "ignite_scaling_pool"},
        evidence_state="assumed",
        open_questions=["点燃玩家侧增伤池构成是否完整？"],
    ),
    "fixture-monster-dr": mechanism(
        "fixture-monster-dr",
        selectors=[{"kind": "stat", "namespace": "stat", "ids": ["monster_dr"]}],
        aliases=["Monster DR", "怪物减伤"],
        title_zh="怪物减伤测试机制",
        title_en="Monster DR test mechanism",
        components=["target_side"],
        tags=["defense"],
        handler={"module": "le_calc.monster_dr", "symbol": "monster_dr_factor"},
        evidence_state="confirmed",
        case_scope=["fixture-case", "other-case"],
    ),
    "fixture-rhythm": mechanism(
        "fixture-rhythm",
        selectors=[
            {"kind": "node", "namespace": "node", "ids": ["fr11mv:3"]},
            {"kind": "ability", "namespace": "ability", "ids": ["fr11mv"]},
        ],
        aliases=["Rhythm of Fire", "节奏之火"],
        title_zh="Rhythm 测试机制",
        components=["hit", "frequency"],
        tags=["fire", "melee"],
        conditions=["rhythm_stacks"],
        handler={"module": "le_calc.hit_damage", "symbol": "rotation_more"},
        evidence_state="derived",
        case_scope=["fixture-case"],
    ),
}

DEFAULT_SKILLS: dict[str, dict[str, Any]] = {
    "fixture-fr-template": skill_template(
        "fixture-fr-template",
        ability_id="fr11mv",
        title_zh="火焰重击测试模板",
        title_en="Flame Reave test template",
        mappings=[{"selector": "node:fr11mv:3", "mechanism_id": "fixture-rhythm"}],
    )
}


def make_knowledge_dir(
    temp_root: Path,
    *,
    mechanisms: dict[str, dict[str, Any]] | None = None,
    skills: dict[str, dict[str, Any]] | None = None,
    with_schemas: bool = True,
    write_defaults: bool = True,
) -> Path:
    """在 ``temp_root/knowledge/`` 下创建自包含知识库并返回该目录。"""
    knowledge_dir = Path(temp_root) / "knowledge"
    (knowledge_dir / "mechanisms").mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "skills").mkdir(parents=True, exist_ok=True)
    if with_schemas:
        schemas = knowledge_dir / "schemas"
        schemas.mkdir(parents=True, exist_ok=True)
        for name in SCHEMA_FILES:
            shutil.copyfile(SCHEMAS_DIR / name, schemas / name)
    if write_defaults:
        for mechanism_id, document in (mechanisms or DEFAULT_MECHANISMS).items():
            write_json(knowledge_dir / "mechanisms" / f"{mechanism_id}.json", document)
        for skill_id, document in (skills or DEFAULT_SKILLS).items():
            write_json(knowledge_dir / "skills" / f"{skill_id}.json", document)
    else:
        for mechanism_id, document in (mechanisms or {}).items():
            write_json(knowledge_dir / "mechanisms" / f"{mechanism_id}.json", document)
        for skill_id, document in (skills or {}).items():
            write_json(knowledge_dir / "skills" / f"{skill_id}.json", document)
    return knowledge_dir


class KnowledgeFixture:
    """一个临时仓库根：``knowledge/`` 源 + ``data/cache/knowledge.sqlite`` 索引。"""

    def __init__(self, root: Path, knowledge_dir: Path) -> None:
        self.root = Path(root)
        self.knowledge_dir = Path(knowledge_dir)
        self.index_path = self.root / "data" / "cache" / "knowledge.sqlite"
        self.index_md_path = self.knowledge_dir / "INDEX.generated.md"

    def rebuild(self, **kwargs: Any) -> dict[str, Any]:
        from le_knowledge import api

        return api.rebuild(
            root=self.root,
            knowledge_dir=self.knowledge_dir,
            index_path=self.index_path,
            index_md_path=self.index_md_path,
            **kwargs,
        )

    def search(self, query: Any, **kwargs: Any) -> dict[str, Any]:
        from le_knowledge import api

        return api.search(
            query,
            root=self.root,
            knowledge_dir=self.knowledge_dir,
            index_path=self.index_path,
            **kwargs,
        )

    def show(self, entry_id: str, **kwargs: Any) -> dict[str, Any]:
        from le_knowledge import api

        return api.show(
            entry_id,
            root=self.root,
            knowledge_dir=self.knowledge_dir,
            index_path=self.index_path,
            **kwargs,
        )


def fixture(temp_dir: Path, **kwargs: Any) -> KnowledgeFixture:
    knowledge_dir = make_knowledge_dir(temp_dir, **kwargs)
    return KnowledgeFixture(temp_dir, knowledge_dir)
