"""Shared synthetic fixtures for the lead-owned phase-3 engine tests.

The numbers mirror the expected shape of the fixed build (fire resistance
stacker, binary system, Heat Wave) but are NOT the real calculation result.
"""

from __future__ import annotations

from typing import Any, Optional

from le_calc import model
from le_calc.ruleset import Ruleset, load_ruleset


def _c(
    cid: str,
    stat_key: str,
    operation: str,
    value: float,
    *,
    unit: str = "percent",
    source_kind: str = "equipment",
    label: Optional[str] = None,
    path: Optional[str] = None,
    tags: Optional[list[str]] = None,
    target: Optional[str] = None,
    group: Optional[str] = None,
    included: bool = True,
    reason: Optional[str] = None,
    status: str = model.STATUS_CONFIRMED,
    condition_code: Optional[str] = None,
    condition_status: str = model.CONDITION_ALWAYS,
) -> model.Contribution:
    return model.Contribution(
        contribution_id=cid,
        source_kind=source_kind,
        source_label=label or cid,
        source_path=path or cid,
        stat_name=label or stat_key,
        stat_key=stat_key,
        operation=operation,
        value=value,
        unit=unit,
        damage_tags=list(tags or []),
        target=target,
        condition_code=condition_code,
        condition_status=condition_status,
        group=group,
        included=included,
        exclusion_reason=reason,
        status=status,
        evidence=[model.Evidence(kind="catalog", detail="synthetic test fixture", locator=cid)],
    )


def make_snapshot(fire_resistance: Optional[float] = 760.0) -> model.Snapshot:
    """Synthetic snapshot matching the real build's expected stat shape."""
    contributions: list[model.Contribution] = [
        _c("test:fire-res", model.STAT_RESISTANCE, "added", fire_resistance or 0.0, target="fire", label="+X% Fire Resistance"),
        _c("test:cold-res", model.STAT_RESISTANCE, "added", 129.0, target="cold"),
        # increased damage pools (values are fractions: 1.77 == +177%)
        _c("test:inc-fire", model.STAT_DAMAGE, "increased", 1.77, tags=["Fire"]),
        _c("test:inc-elem-dot", model.STAT_DAMAGE, "increased", 8.35, tags=["Elemental", "DoT"]),
        _c("test:inc-dot", model.STAT_DAMAGE, "increased", 0.60, tags=["DoT"]),
        _c("test:inc-spell", model.STAT_DAMAGE, "increased", 2.00, tags=["Spell"]),
        _c("test:inc-idol-fire", model.STAT_DAMAGE, "increased", 0.71, tags=["Fire"]),
        # added spell damage
        _c("test:added-spell", model.STAT_ADDED_DAMAGE, "added", 65.0, unit="flat", tags=["Spell"]),
        # ignite duration rows (head 52% + four idols)
        _c("test:ignite-dur-1", model.STAT_AILMENT_DURATION, "increased", 0.52, target="ignite"),
        _c("test:ignite-dur-2", model.STAT_AILMENT_DURATION, "increased", 0.21, target="ignite"),
        _c("test:ignite-dur-3", model.STAT_AILMENT_DURATION, "increased", 0.21, target="ignite"),
        _c("test:ignite-dur-4", model.STAT_AILMENT_DURATION, "increased", 0.23, target="ignite"),
        _c("test:ignite-dur-5", model.STAT_AILMENT_DURATION, "increased", 0.24, target="ignite"),
        _c("test:chill-dur", model.STAT_AILMENT_DURATION, "increased", 0.52, target="chill", included=False, reason="chill duration is out of scope"),
        _c("test:ailment-effect", model.STAT_AILMENT_EFFECT, "increased", 0.35, target="ignite"),
        # penetration
        _c("test:fire-pen", model.STAT_PENETRATION, "added", 0.26, tags=["Fire"]),
        # cast speed / cooldown
        _c("test:cast-speed", model.STAT_CAST_SPEED, "increased", 0.108),
        _c("test:cdr-waist", model.STAT_COOLDOWN_RECOVERY, "increased", 0.32),
        _c("test:cdr-ring", model.STAT_COOLDOWN_RECOVERY, "increased", 0.08),
        _c("test:cdr-gloves", model.STAT_COOLDOWN_RECOVERY, "increased", 0.0675, status=model.STATUS_DERIVED),
        # explicitly excluded: not applicable
        _c("test:melee-dmg", model.STAT_DAMAGE, "increased", 0.90, tags=["Melee"], included=False, reason="melee damage does not apply to a spell"),
        _c("test:minion-fire-res", model.STAT_RESISTANCE, "added", 0.12, target="fire", tags=["Minion"], included=False, reason="minion modifier"),
        _c("test:spell-hit-dmg", model.STAT_DAMAGE, "increased", 0.50, tags=["Spell", "Hit"], included=False, reason="hit-only modifier"),
    ]
    return model.Snapshot(
        build_meta={
            "build": {"build_id": "letools:o3ZRm9my", "revision_id": "rev-test", "content_sha256": "test"},
            "dataset": {"dataset_id": "phase1-dataset-003", "target_game_version": "1.5.2"},
            "calc_input": {"path": "synthetic", "sha256": "test", "mod_count": 153, "unresolved_inputs": 0},
            "build_spec_sha256": "test-build-spec",
            "calc_input_sha256": "test-calc-input",
            "database_sha256": "test-db",
            "dataset_logical_content_sha256": "test-dataset",
        },
        character={"level": 100, "class": "Mage", "mastery": "Sorcerer"},
        attributes={
            "strength": 10.0,
            "vitality": 10.0,
            "intelligence": 5.0,
            "dexterity": 2.0,
            "attunement": 2.0,
        },
        resistances={
            "fire": {"total": float(fire_resistance or 0.0), "cap": 75.0, "capped": min(75.0, float(fire_resistance or 0.0)), "overcapped": max(0.0, float(fire_resistance or 0.0) - 75.0)},
            "cold": {"total": 129.0, "cap": 75.0, "capped": 75.0, "overcapped": 54.0},
        },
        contributions=contributions,
        derived={"mana_max": 177.6},
        issues=[],
        excluded_sources=[
            {"source_id": "skill_tree:sb44eQ", "label": "Enchant Weapon", "reason": "out of scope", "category": "scope"}
        ],
    )


def make_ruleset() -> Ruleset:
    return load_ruleset()


def make_ruleset_without_structural() -> Ruleset:
    """Copy of the ruleset with Red Giant/Binary System removed (diagnostic)."""
    rs = load_ruleset()
    data: dict[str, Any] = json_copy(rs.data)
    data["black_hole"]["structural"] = {}
    return Ruleset(data=data, path=rs.path, sha256=rs.sha256 + "-nostruct")


def json_copy(data: Any) -> Any:
    import copy

    return copy.deepcopy(data)


__all__ = ["json_copy", "make_ruleset", "make_ruleset_without_structural", "make_snapshot"]
