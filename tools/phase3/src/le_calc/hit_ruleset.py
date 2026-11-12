"""Hit-damage ruleset loader for the Flame Reave (fr11mv) hit model.

Owner: fr-engine (task-2).  Consumers: ``hit_damage``/``hit_simulate``/
``hit_buffs``/``hit_report``/``cli``.

The ruleset JSON (``tools/phase3/rules/ruleset-fr-hit-stage1-v2.json``) is the
authoritative source for:

* the attack model (base rate source, dual-wield averaging, speed pools),
* the damage model (component policy, added-damage scaling, pools),
* critical strikes, target-side multipliers (penetration / monster DR),
* per-cast hit events, the rotation policy,
* ``effects[]``: every conditional effect that is not part of the static
  snapshot (Enchant Weapon/Frenzy/Blade Weaving/Firebrand/Rhythm/idols/...).
  ``coverage`` says whether the engine models it (``modeled``), deliberately
  does not (``not_modeled``) or cannot yet (``unresolved``);
  ``status`` uses the project rule vocabulary and marks parameter confidence.
* ``rules[]``: auditable rule statements with ``status`` and evidence locators.

Schema: ``le-ruleset-hit-v1``.  See DESIGN §5.2.

Entry point::

    from le_calc.hit_ruleset import load_ruleset
    ruleset = load_ruleset("tools/phase3/rules/ruleset-fr-hit-stage1-v2.json")
    ruleset.rules            # list[dict]
    ruleset.effects          # list[dict]
    ruleset.rotation         # dict
    ruleset.to_dict()        # data + path + sha256
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from . import model

SCHEMA_VERSION = "le-ruleset-hit-v1"
EFFECT_COVERAGE = ("modeled", "not_modeled", "unresolved")
DEFAULT_RULESET_PATH = (
    Path(__file__).resolve().parents[2] / "rules" / "ruleset-fr-hit-stage1-v2.json"
)

REQUIRED_TOP_KEYS = (
    "schema",
    "ruleset_id",
    "game_version",
    "skill",
    "attack_model",
    "damage_model",
    "critical",
    "target",
    "hit_events",
    "effects",
    "rotation",
    "rules",
    "open_questions",
)


class HitRulesetError(ValueError):
    """Fatal ruleset-contract violation (missing/invalid schema fields)."""


@dataclass
class RulesetDocument:
    """Loaded ``le-ruleset-hit-v1`` document plus file identity."""

    data: dict[str, Any]
    path: Optional[str] = None
    sha256: Optional[str] = None

    # -- accessors used by the engine ---------------------------------------
    @property
    def ruleset_id(self) -> str:
        return str(self.data["ruleset_id"])

    @property
    def schema(self) -> str:
        return str(self.data["schema"])

    @property
    def game_version(self) -> str:
        return str(self.data.get("game_version") or "unknown")

    @property
    def skill(self) -> dict[str, Any]:
        return dict(self.data.get("skill") or {})

    @property
    def attack_model(self) -> dict[str, Any]:
        return dict(self.data.get("attack_model") or {})

    @property
    def damage_model(self) -> dict[str, Any]:
        return dict(self.data.get("damage_model") or {})

    @property
    def critical(self) -> dict[str, Any]:
        return dict(self.data.get("critical") or {})

    @property
    def target(self) -> dict[str, Any]:
        return dict(self.data.get("target") or {})

    @property
    def hit_events(self) -> dict[str, Any]:
        return dict(self.data.get("hit_events") or {})

    @property
    def rotation(self) -> dict[str, Any]:
        return dict(self.data.get("rotation") or {})

    @property
    def effects(self) -> list[dict[str, Any]]:
        return list(self.data.get("effects") or [])

    @property
    def rules(self) -> list[dict[str, Any]]:
        return list(self.data.get("rules") or [])

    @property
    def open_questions(self) -> list[dict[str, Any]]:
        return list(self.data.get("open_questions") or [])

    def effect(self, effect_id: str) -> Optional[dict[str, Any]]:
        for effect in self.effects:
            if str(effect.get("id")) == str(effect_id):
                return effect
        return None

    def rule(self, rule_id: str) -> Optional[dict[str, Any]]:
        for rule in self.rules:
            if str(rule.get("id") or rule.get("rule_id")) == str(rule_id):
                return rule
        return None

    # -- containers behaviour (report code reads .data or dict-like access) --
    def to_dict(self) -> dict[str, Any]:
        out = dict(self.data)
        out.setdefault("path", self.path)
        out.setdefault("sha256", self.sha256)
        return out

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __iter__(self) -> Iterator[str]:
        return iter(self.data)

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rule in self.rules:
            status = str(rule.get("status") or "unresolved")
            counts[status] = counts.get(status, 0) + 1
        return dict(sorted(counts.items()))

    def coverage_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for effect in self.effects:
            coverage = str(effect.get("coverage") or "unresolved")
            counts[coverage] = counts.get(coverage, 0) + 1
        return dict(sorted(counts.items()))


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise HitRulesetError(message)


def _validate_evidence(evidence: Any, owner: str) -> None:
    _require(isinstance(evidence, list) and bool(evidence), f"{owner}: evidence must be a non-empty list")
    for index, item in enumerate(evidence):
        _require(isinstance(item, dict), f"{owner}: evidence[{index}] must be an object")
        _require(bool(item.get("kind")), f"{owner}: evidence[{index}].kind missing")
        _require(bool(item.get("detail")), f"{owner}: evidence[{index}].detail missing")
        _require(bool(item.get("locator")), f"{owner}: evidence[{index}].locator missing")


def validate_document(data: dict[str, Any]) -> None:
    """Fail closed on a malformed ``le-ruleset-hit-v1`` document."""
    _require(isinstance(data, dict), "ruleset must be a JSON object")
    for key in REQUIRED_TOP_KEYS:
        _require(key in data, f"ruleset missing required key: {key}")
    _require(data["schema"] == SCHEMA_VERSION, f"ruleset.schema {data['schema']!r} != {SCHEMA_VERSION!r}")
    _require(bool(data["ruleset_id"]), "ruleset_id must be non-empty")
    _require(isinstance(data["skill"], dict), "skill must be an object")
    _require(bool((data["skill"] or {}).get("ability_id")), "skill.ability_id missing")
    _require(isinstance(data["attack_model"], dict), "attack_model must be an object")
    _require(bool((data["attack_model"] or {}).get("base_rate_source")), "attack_model.base_rate_source missing")
    _require(isinstance(data["damage_model"], dict), "damage_model must be an object")
    _require(bool((data["damage_model"] or {}).get("components")), "damage_model.components missing")
    _require(isinstance(data["critical"], dict), "critical must be an object")
    _require(isinstance(data["target"], dict), "target must be an object")
    _require(isinstance(data["hit_events"], dict), "hit_events must be an object")
    _require(int((data["hit_events"] or {}).get("hits_per_cast") or 0) > 0, "hit_events.hits_per_cast missing")
    _require(isinstance(data["rotation"], dict), "rotation must be an object")
    _require(bool((data["rotation"] or {}).get("policy")), "rotation.policy missing")
    _require(isinstance(data["open_questions"], list), "open_questions must be a list")

    _require(isinstance(data["rules"], list), "rules must be a list")
    seen_rules: set[str] = set()
    for index, rule in enumerate(data["rules"]):
        owner = f"rules[{index}]"
        _require(isinstance(rule, dict), f"{owner} must be an object")
        rule_id = str(rule.get("id") or rule.get("rule_id") or "")
        _require(bool(rule_id), f"{owner}.id missing")
        _require(rule_id not in seen_rules, f"duplicate rule id {rule_id!r}")
        seen_rules.add(rule_id)
        _require(
            rule.get("status") in model.VALID_STATUS,
            f"{owner} ({rule_id}) status {rule.get('status')!r} not in {model.VALID_STATUS}",
        )
        _require(bool(rule.get("statement")), f"{owner} ({rule_id}).statement missing")
        _validate_evidence(rule.get("evidence"), f"{owner} ({rule_id})")
        if rule.get("status") in (model.STATUS_ASSUMED, model.STATUS_UNRESOLVED):
            _require(
                rule.get("assumption") or rule.get("open_question"),
                f"{owner} ({rule_id}) is {rule['status']} but has no assumption/open_question",
            )

    _require(isinstance(data["effects"], list), "effects must be a list")
    seen_effects: set[str] = set()
    for index, effect in enumerate(data["effects"]):
        owner = f"effects[{index}]"
        _require(isinstance(effect, dict), f"{owner} must be an object")
        effect_id = str(effect.get("id") or "")
        _require(bool(effect_id), f"{owner}.id missing")
        _require(effect_id not in seen_effects, f"duplicate effect id {effect_id!r}")
        seen_effects.add(effect_id)
        _require(bool(effect.get("label")), f"{owner} ({effect_id}).label missing")
        _require(
            effect.get("coverage") in EFFECT_COVERAGE,
            f"{owner} ({effect_id}).coverage {effect.get('coverage')!r} not in {EFFECT_COVERAGE}",
        )
        _require(
            effect.get("status") in model.VALID_STATUS,
            f"{owner} ({effect_id}).status {effect.get('status')!r} not in {model.VALID_STATUS}",
        )
        _require(isinstance(effect.get("modifiers", []), list), f"{owner} ({effect_id}).modifiers must be a list")
        condition = effect.get("condition")
        _require(condition is None or isinstance(condition, dict), f"{owner} ({effect_id}).condition must be an object")
        _require(bool(effect.get("next_probe")), f"{owner} ({effect_id}).next_probe missing (required)")
        _require(isinstance(effect.get("include_in"), list), f"{owner} ({effect_id}).include_in missing")
        _require(
            effect.get("coverage") != "modeled" or bool(effect.get("include_in")),
            f"{owner} ({effect_id}) coverage=modeled but include_in is empty",
        )
        _validate_evidence(effect.get("evidence"), f"{owner} ({effect_id})")
        _require(
            effect.get("coverage") != "modeled"
            or bool(effect.get("modifiers"))
            or bool(effect.get("applied_via")),
            f"{owner} ({effect_id}) coverage=modeled but modifiers and applied_via are both empty",
        )
        for modifier in effect.get("modifiers") or []:
            _require(isinstance(modifier, dict), f"{owner} ({effect_id}) modifier must be an object")
            _require(bool(modifier.get("stat_key")), f"{owner} ({effect_id}) modifier.stat_key missing")
            _require(
                modifier.get("operation") in ("added", "increased", "more", "base", "flag"),
                f"{owner} ({effect_id}) modifier.operation invalid: {modifier.get('operation')!r}",
            )

    components = (data["damage_model"] or {}).get("components") or []
    _require(isinstance(components, list), "damage_model.components must be a list")
    for index, component in enumerate(components):
        owner = f"damage_model.components[{index}]"
        _require(isinstance(component, dict), f"{owner} must be an object")
        _require(bool(component.get("component_id")), f"{owner}.component_id missing")
        _require(bool(component.get("damage_type")), f"{owner}.damage_type missing")
        _require(isinstance(component.get("damage_tags", []), list), f"{owner}.damage_tags must be a list")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def parse_ruleset(data: dict[str, Any], *, path: Optional[str] = None, sha256: Optional[str] = None) -> RulesetDocument:
    """Validate an in-memory ruleset document and wrap it."""
    validate_document(data)
    return RulesetDocument(data=dict(data), path=path, sha256=sha256)


def load_ruleset(path: str | Path | None = None) -> RulesetDocument:
    """Load and validate the hit ruleset JSON (default: stage-1 ruleset)."""
    target = Path(path) if path is not None else DEFAULT_RULESET_PATH
    if not target.is_file():
        raise HitRulesetError(f"ruleset not found: {target}")
    raw = target.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HitRulesetError(f"ruleset is not valid JSON ({target}): {exc}") from exc
    if not isinstance(data, dict):
        raise HitRulesetError(f"ruleset must be a JSON object: {target}")
    document = parse_ruleset(data, path=str(target), sha256=hashlib.sha256(raw).hexdigest())
    return document


def ruleset_fingerprint(document: RulesetDocument | dict[str, Any]) -> dict[str, Any]:
    """Compact identity block for results/manifests."""
    if isinstance(document, RulesetDocument):
        return {
            "ruleset_id": document.ruleset_id,
            "schema": document.schema,
            "game_version": document.game_version,
            "path": document.path,
            "sha256": document.sha256,
            "status_counts": document.status_counts(),
            "coverage_counts": document.coverage_counts(),
        }
    data = dict(document)
    counts: dict[str, int] = {}
    for rule in data.get("rules") or []:
        status = str(rule.get("status") or "unresolved")
        counts[status] = counts.get(status, 0) + 1
    coverage: dict[str, int] = {}
    for effect in data.get("effects") or []:
        key = str(effect.get("coverage") or "unresolved")
        coverage[key] = coverage.get(key, 0) + 1
    return {
        "ruleset_id": data.get("ruleset_id"),
        "schema": data.get("schema"),
        "game_version": data.get("game_version"),
        "status_counts": dict(sorted(counts.items())),
        "coverage_counts": dict(sorted(coverage.items())),
    }


__all__ = [
    "DEFAULT_RULESET_PATH",
    "EFFECT_COVERAGE",
    "HitRulesetError",
    "REQUIRED_TOP_KEYS",
    "RulesetDocument",
    "SCHEMA_VERSION",
    "load_ruleset",
    "parse_ruleset",
    "ruleset_fingerprint",
    "validate_document",
]
