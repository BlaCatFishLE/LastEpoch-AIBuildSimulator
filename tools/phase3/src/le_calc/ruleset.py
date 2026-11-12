"""Ruleset loader and accessors for the phase-3 Black Hole calculator (v1).

The ruleset JSON (tools/phase3/rules/ruleset-bh-stage1-v1.json) is the
authoritative source for Black Hole skill-tree modifiers and for the v1
mechanical decisions.  Player-side sources come from the input snapshot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

DEFAULT_RULESET_PATH = (
    Path(__file__).resolve().parents[2] / "rules" / "ruleset-bh-stage1-v1.json"
)


class RulesetError(ValueError):
    pass


@dataclass
class Ruleset:
    data: dict[str, Any]
    path: Optional[str] = None
    sha256: Optional[str] = None

    # -- convenient accessors ------------------------------------------------
    @property
    def ruleset_id(self) -> str:
        return self.data["ruleset_id"]

    @property
    def game_version(self) -> str:
        return self.data.get("game_version", "unknown")

    @property
    def black_hole(self) -> dict[str, Any]:
        return self.data["black_hole"]

    @property
    def component(self) -> dict[str, Any]:
        return self.data["black_hole"]["components"][0]

    @property
    def structural(self) -> dict[str, Any]:
        return self.data["black_hole"].get("structural", {})

    @property
    def has_red_giant(self) -> bool:
        return "red_giant" in self.structural and self.structural["red_giant"].get("convert")

    @property
    def has_binary_system(self) -> bool:
        bs = self.structural.get("binary_system")
        return bool(bs and bs.get("model") == "two_components")

    @property
    def singularities(self) -> dict[str, Any]:
        return self.structural.get("singularity", {})

    @property
    def weakened_reality(self) -> dict[str, Any]:
        return self.structural.get("weakened_reality", {})

    @property
    def skill_tree_nodes(self) -> list[dict[str, Any]]:
        return self.data.get("skill_tree_nodes", [])

    def node(self, node_id: str) -> Optional[dict[str, Any]]:
        for node in self.skill_tree_nodes:
            if str(node.get("node_id")) == str(node_id):
                return node
        return None

    @property
    def duration(self) -> dict[str, Any]:
        return self.data["duration"]

    @property
    def ignite(self) -> dict[str, Any]:
        return self.data["ignite"]

    @property
    def target_side(self) -> dict[str, Any]:
        return self.data["target_side"]

    @property
    def cooldown(self) -> dict[str, Any]:
        return self.data["cooldown"]

    @property
    def cast(self) -> dict[str, Any]:
        return self.data["cast"]

    @property
    def action_policies(self) -> list[dict[str, Any]]:
        return self.data.get("action_policies", [])

    @property
    def scenario(self) -> dict[str, Any]:
        return self.data["scenario"]

    @property
    def rules(self) -> list[dict[str, Any]]:
        return self.data.get("rules", [])

    def with_snapshot_points(self, snapshot):
        """Bind only rules with an explicit per-point formula; no inferred ratios."""
        points = (snapshot.derived.get("skill_node_points") or {}).get("bh2")
        if points is None:
            from .workflow.components import snapshot_node_points
            points = snapshot_node_points(snapshot.build_meta)
            if points is None:
                return self
            snapshot.derived["skill_node_points"] = {"bh2": points}
        data = dict(self.data)
        data["skill_tree_nodes"] = [dict(node, effects=[dict(e) for e in node.get("effects", [])])
                                    for node in self.skill_tree_nodes]
        for node in data["skill_tree_nodes"]:
            for effect in node.get("effects", []):
                if (effect.get("operation") == "more" and
                        "value_per_point" in effect):
                    effect["value_total"] = (float(effect["value_per_point"]) *
                                             float(points.get(str(node["node_id"]), 0)))
        return Ruleset(data, self.path, self.sha256)

    def tree_more_effects(self, applies_keys: set[str]) -> list[dict[str, Any]]:
        """Damage 'more' effects from skill-tree nodes declared in the ruleset."""
        out: list[dict[str, Any]] = []
        for node in self.skill_tree_nodes:
            for eff in node.get("effects", []):
                if eff.get("stat_key") != "damage" or eff.get("operation") != "more":
                    continue
                if not set(eff.get("applies_to", [])) & applies_keys:
                    continue
                out.append(
                    {
                        "node_id": str(node.get("node_id")),
                        "name": node.get("name"),
                        "group": eff.get("group"),
                        "value": float(eff.get("value_total", 0.0)),
                        "damage_tags": list(eff.get("damage_tags", [])),
                        "condition_code": eff.get("condition_code"),
                        "rule_id": eff.get("rule_id"),
                    }
                )
        return out

    def tree_duration_terms(self) -> list[dict[str, Any]]:
        return [
            {"node_id": str(t["node_id"]), "value": float(t["value"])}
            for t in self.duration.get("tree_terms", [])
        ]

    def tree_cooldown_flat_terms(self) -> list[dict[str, Any]]:
        return [
            {"node_id": str(t["node_id"]), "value": float(t["value"])}
            for t in self.cooldown.get("flat_adjustment_s", [])
        ]

    def tree_cooldown_reduction_terms(self) -> list[dict[str, Any]]:
        return [
            {"node_id": str(t["node_id"]), "value": float(t["value"])}
            for t in self.cooldown.get("cooldown_reduction", [])
        ]

    def tree_charge_terms(self) -> list[dict[str, Any]]:
        return [
            {"node_id": str(t["node_id"]), "value": float(t["value"])}
            for t in self.cooldown.get("extra_max_charges", [])
        ]


def load_ruleset(path: str | Path | None = None) -> Ruleset:
    p = Path(path) if path is not None else DEFAULT_RULESET_PATH
    if not p.is_file():
        raise RulesetError(f"ruleset not found: {p}")
    raw = p.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise RulesetError(f"ruleset is not valid JSON: {exc}") from exc
    for key in ("ruleset_id", "black_hole", "duration", "ignite", "cooldown", "cast", "scenario"):
        if key not in data:
            raise RulesetError(f"ruleset missing required key: {key}")
    return Ruleset(data=data, path=str(p), sha256=hashlib.sha256(raw).hexdigest())


__all__ = ["DEFAULT_RULESET_PATH", "Ruleset", "RulesetError", "load_ruleset"]
