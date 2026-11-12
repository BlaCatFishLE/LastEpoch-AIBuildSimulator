"""Validation suite for the affix database (DB + manifest + source snapshot).

All checks run offline.  ``validate`` returns structured results so the CLI and
the test-suite can share the same logic.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import SCHEMA_VERSION
from .db import sha256_file
from .formula import (
    ROUNDING_POLICY_TUNKLAB,
    ROUNDING_POLICY_UNITY,
    compute_modifier,
    js_math_round,
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    severity: str = "error"  # 'error' | 'warning' | 'info'

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "severity": self.severity, "detail": self.detail}


@dataclass
class ValidationReport:
    checks: list[CheckResult] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(check.ok or check.severity in ("warning", "info") for check in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.ok and check.severity == "error"]

    def add(self, name: str, ok: bool, detail: str = "", severity: str = "error") -> None:
        self.checks.append(CheckResult(name=name, ok=ok, detail=detail, severity=severity))

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [check.as_dict() for check in self.checks],
            "stats": self.stats,
        }


def _connect(db_path: Path) -> sqlite3.Connection:
    if not Path(db_path).is_file():
        raise FileNotFoundError(f"database not found: {db_path}")
    connection = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _fetch_range(connection: sqlite3.Connection, affix_id: int, base_type_id: int, tier: int, roll_slot: int = 0) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM affix_base_ranges WHERE affix_id=? AND base_type_id=? AND tier=? AND roll_slot=?",
        (affix_id, base_type_id, tier, roll_slot),
    ).fetchone()


def validate_db(db_path: Path | str, manifest_path: Path | str | None = None) -> ValidationReport:
    db_file = Path(db_path)
    report = ValidationReport()
    connection = _connect(db_file)
    try:
        # --- schema / counts -------------------------------------------------
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required_tables = {
            "meta",
            "source_datasets",
            "affixes",
            "affix_tiers",
            "equipment_types",
            "equipment_sub_bases",
            "stat_properties",
            "player_stat_properties",
            "ability_properties",
            "affix_eligible_bases",
            "unresolved_can_roll_on",
            "affix_base_ranges",
            "equipment_globals",
            "omen_special_ranges",
        }
        report.add("schema.tables", required_tables <= tables, f"missing={sorted(required_tables - tables)}")
        schema_version = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        report.add(
            "schema.version",
            schema_version is not None and int(schema_version[0]) == SCHEMA_VERSION,
            f"expected={SCHEMA_VERSION}",
        )

        counts = {
            "affixes": connection.execute("SELECT COUNT(*) FROM affixes").fetchone()[0],
            "equipment_types": connection.execute("SELECT COUNT(*) FROM equipment_types").fetchone()[0],
            "equipment_sub_bases": connection.execute("SELECT COUNT(*) FROM equipment_sub_bases").fetchone()[0],
            "affix_eligible_bases": connection.execute("SELECT COUNT(*) FROM affix_eligible_bases").fetchone()[0],
            "affix_tiers": connection.execute("SELECT COUNT(*) FROM affix_tiers").fetchone()[0],
            "affix_base_ranges": connection.execute("SELECT COUNT(*) FROM affix_base_ranges").fetchone()[0],
            "equipment_globals": connection.execute("SELECT COUNT(*) FROM equipment_globals").fetchone()[0],
            "omen_special_ranges": connection.execute("SELECT COUNT(*) FROM omen_special_ranges").fetchone()[0],
            "stat_properties": connection.execute("SELECT COUNT(*) FROM stat_properties").fetchone()[0],
            "player_stat_properties": connection.execute("SELECT COUNT(*) FROM player_stat_properties").fetchone()[0],
            "ability_properties": connection.execute("SELECT COUNT(*) FROM ability_properties").fetchone()[0],
            "unresolved_can_roll_on": connection.execute("SELECT COUNT(*) FROM unresolved_can_roll_on").fetchone()[0],
        }
        report.stats["counts"] = counts
        report.add("counts.affixes", counts["affixes"] == 1156, f"{counts['affixes']} (expected 1156)")
        report.add("counts.affixes_with_ranges", _all_affixes_have_ranges(connection), "")
        report.add("counts.equipment_types", counts["equipment_types"] == 47, f"{counts['equipment_types']} (39 equippable + 8 non-equippable)")
        report.add("counts.unresolved_vs_tunklab_exclusion", _unresolved_are_tunklab_excluded(connection), "")

        # --- formula consistency --------------------------------------------
        formula_issues: list[str] = []
        display_issues: list[str] = []
        tie_differences = 0
        for row in connection.execute(
            """
            SELECT r.*, a.standard_affix_effect_modifier, e.affix_effect_modifier
            FROM affix_base_ranges r
            JOIN affixes a ON a.affix_id = r.affix_id
            JOIN equipment_types e ON e.base_type_id = r.base_type_id
            """
        ):
            expected_modifier = compute_modifier(e_ := row["affix_effect_modifier"], s_ := row["standard_affix_effect_modifier"])
            if abs(row["modifier"] - expected_modifier) > 1e-12:
                formula_issues.append(f"modifier affix={row['affix_id']} base={row['base_type_id']}")
            if abs(row["effective_min"] - row["raw_min"] * row["modifier"]) > 1e-12:
                formula_issues.append(f"effective_min affix={row['affix_id']} base={row['base_type_id']} tier={row['tier']}")
            if abs(row["effective_max"] - row["raw_max"] * row["modifier"]) > 1e-12:
                formula_issues.append(f"effective_max affix={row['affix_id']} base={row['base_type_id']} tier={row['tier']}")
            if row["tunklab_display_min"] is not None and row["tunklab_display_max"] is not None:
                if row["display_min"] != row["tunklab_display_min"] or row["display_max"] != row["tunklab_display_max"]:
                    tie_differences += 1
            if len(formula_issues) > 10 or len(display_issues) > 10:
                break
        report.add("formula.modifier_and_effective", not formula_issues, "; ".join(formula_issues[:10]))
        report.stats["half_even_vs_half_up_endpoint_differences"] = tie_differences
        report.add(
            "display.policy_metadata",
            _display_policy_rows(connection),
            "display_policy / tunklab_display_policy columns must match the documented constants",
        )

        # --- dataset integrity ----------------------------------------------
        source_rows = list(connection.execute("SELECT * FROM source_datasets"))
        report.add(
            "source.datasets_present",
            {row["kind"] for row in source_rows}
            == {"affixes", "equipment", "player_properties", "player_stat_properties", "ability_properties"},
            f"{sorted(row['kind'] for row in source_rows)}",
        )
        report.add(
            "source.version_consistent",
            all(row["version"] == "1.5.2" for row in source_rows),
            f"versions={sorted({row['version'] for row in source_rows})}",
        )

        # --- acceptance / known values --------------------------------------
        report.checks.extend(_known_value_checks(connection))
        report.checks.extend(_idol_omen_checks(connection))

        # --- eligibility exclusions -----------------------------------------
        report.add("eligibility.fire_res_not_on_sword", _range_absent(connection, 13, 9, 7), "Fire Resistance must not roll on 1H Swords (baseTypeID 9)")
        report.add("eligibility.fire_res_on_body_armor", _range_present(connection, 13, 1, 7), "")
        report.add("eligibility.body_armor_only_for_fire_res", _body_armor_group(connection), "")

        # --- manifest hashes -------------------------------------------------
        if manifest_path is not None:
            report.checks.extend(_manifest_checks(Path(manifest_path)))
        else:
            auto_manifest = db_file.parent.parent / "manifest.json"
            if auto_manifest.is_file():
                report.checks.extend(_manifest_checks(auto_manifest))
    finally:
        connection.close()
    return report


def _all_affixes_have_ranges(connection: sqlite3.Connection) -> bool:
    missing = connection.execute(
        """
        SELECT COUNT(*) FROM affixes a
        WHERE NOT EXISTS (SELECT 1 FROM affix_base_ranges r WHERE r.affix_id = a.affix_id)
        """
    ).fetchone()[0]
    return missing == 0


def _unresolved_are_tunklab_excluded(connection: sqlite3.Connection) -> bool:
    rows = list(connection.execute("SELECT base_type_id, excluded_by_tunklab FROM unresolved_can_roll_on"))
    return all(row["excluded_by_tunklab"] == 1 for row in rows) and {row["base_type_id"] for row in rows} == {11, 24}


def _display_policy_rows(connection: sqlite3.Connection) -> bool:
    bad = connection.execute(
        "SELECT COUNT(*) FROM affix_base_ranges WHERE display_policy != ? OR tunklab_display_policy != ?",
        (ROUNDING_POLICY_UNITY, ROUNDING_POLICY_TUNKLAB),
    ).fetchone()[0]
    return bad == 0


def _range_present(connection: sqlite3.Connection, affix_id: int, base_type_id: int, tier: int) -> bool:
    return _fetch_range(connection, affix_id, base_type_id, tier) is not None


def _range_absent(connection: sqlite3.Connection, affix_id: int, base_type_id: int, tier: int) -> bool:
    return _fetch_range(connection, affix_id, base_type_id, tier) is None


def _body_armor_group(connection: sqlite3.Connection) -> bool:
    row = _fetch_range(connection, 13, 1, 7)
    if row is None:
        return False
    return row["base_type_id"] == 1 and row["display_max"] == 112 and row["tunklab_display_max"] == 113


def _known_value_checks(connection: sqlite3.Connection) -> list[CheckResult]:
    checks: list[CheckResult] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append(CheckResult(name=name, ok=ok, detail=detail))

    # Acceptance: Fire Resistance (13) x Body Armor (1) x T7
    row = _fetch_range(connection, 13, 1, 7)
    if row is None:
        check("acceptance.fire_res_body_armor_t7", False, "range row missing")
        return checks
    check(
        "acceptance.fire_res_body_armor_t7.effective",
        abs(row["effective_min"] - 0.915) < 1e-12 and abs(row["effective_max"] - 1.125) < 1e-12,
        f"effective={row['effective_min']:.6f}..{row['effective_max']:.6f} (exact decimal {row['effective_min_decimal']}..{row['effective_max_decimal']})",
    )
    check(
        "acceptance.fire_res_body_armor_t7.display_game",
        row["display_min"] == 92 and row["display_max"] == 112,
        f"game(Unity half-even) display={row['display_min']}..{row['display_max']} (expected 92..112)",
    )
    check(
        "acceptance.fire_res_body_armor_t7.display_tunklab",
        row["tunklab_display_min"] == 92 and row["tunklab_display_max"] == 113,
        f"tunklab page(JS half-up) display={row['tunklab_display_min']}..{row['tunklab_display_max']} (expected 92..113)",
    )
    check(
        "acceptance.fire_res_body_armor_t7.raw",
        row["raw_min"] == 0.61 and row["raw_max"] == 0.75 and row["modifier"] == 1.5,
        f"raw={row['raw_min']}..{row['raw_max']} modifier={row['modifier']}",
    )

    # Same affix on a different slot must have a different range.
    helmet = _fetch_range(connection, 13, 0, 7)
    check(
        "acceptance.fire_res_helmet_t7_unscaled",
        helmet is not None and helmet["display_min"] == 61 and helmet["display_max"] == 75 and helmet["modifier"] == 1.0,
        f"helmet display={helmet['display_min'] if helmet else None}..{helmet['display_max'] if helmet else None}",
    )
    shield = _fetch_range(connection, 13, 18, 7)
    check(
        "acceptance.fire_res_shield_t7_modifier",
        shield is not None and abs(shield["modifier"] - 1.17) < 1e-12 and shield["tunklab_display_min"] == 71 and shield["tunklab_display_max"] == 88,
        f"shield display(tunklab)={shield['tunklab_display_min'] if shield else None}..{shield['tunklab_display_max'] if shield else None}",
    )

    # Second affix with a different display class (flat integer): Added Health 25.
    health = _fetch_range(connection, 25, 1, 5)
    check(
        "acceptance.added_health_body_armor_t5",
        health is not None
        and health["display_unit"] == ""
        and health["effective_min_decimal"] == "91.5"
        and health["display_min"] == 92
        and health["tunklab_display_min"] == 92,
        f"effective={health['effective_min_decimal'] if health else None} display={health['display_min'] if health else None}",
    )

    # A weapon affix whose standard modifier equals the 1H weapon modifier -> factor 1.
    weapon = _fetch_range(connection, 63, 5, 1)  # Added Melee Physical Damage x 1H Axe
    check(
        "acceptance.added_melee_physical_1h_axe_t1",
        weapon is not None and abs(weapon["modifier"] - 1.0) < 1e-12,
        f"modifier={weapon['modifier'] if weapon else None} (1H axe 0.75 vs affix 0.75)",
    )
    two_hand = _fetch_range(connection, 63, 12, 1)  # same affix x 2H Axe
    check(
        "acceptance.added_melee_physical_2h_axe_t1",
        two_hand is not None and abs(two_hand["modifier"] - (3.2 / 1.75)) < 1e-12,
        f"modifier={two_hand['modifier'] if two_hand else None} (2H axe 2.2 vs affix 0.75)",
    )
    return checks


def _idol_omen_checks(connection: sqlite3.Connection) -> list[CheckResult]:
    """Idol-slot specific checks requested for the refracted-idol build:

    1. ``Throne of Ambition`` (Adorned Silver Idol, baseTypeID 33) corrupted
       affix 1070 must be 4% All Resistances; the old import showed 1% because it
       used the raw 0.008 roll without the Adorned Idol scale factor.
    2. The Omen-idol special path (Grand Arcane Omen Idol, baseTypeID 29 /
       subTypeID 11): tunklab's own canRollOn excludes Grand Idol, but tunklab
       ships ``affixEffectiveness=1`` and ``omenIdolAffixEffectModifier=0``.
       Derived path must give affix 111 (Idol Fire Resistance) = raw 20-45%.
    """
    checks: list[CheckResult] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append(CheckResult(name=name, ok=ok, detail=detail))

    # -- Throne of Ambition / affix 1070 on Adorned Idol (33) --------------
    throne = _fetch_range(connection, 1070, 33, 1, 0)
    if throne is None:
        check("idol.throne_of_ambition.all_res_adorned", False, "affix 1070 range row missing for baseTypeID 33")
    else:
        raw_only_display = js_math_round(100 * throne["raw_min"])
        expected_modifier = 95 / 17  # (1 + (-0.05)) / (1 + (-0.83))
        check(
            "idol.throne_of_ambition.all_res_adorned",
            abs(throne["modifier"] - expected_modifier) < 1e-12
            and throne["effective_min_decimal"] == "0.04470588235294118"
            and throne["display_min"] == 4
            and throne["display_max"] == 4
            and throne["tunklab_display_min"] == 4,
            (
                f"raw={throne['raw_min']:g} modifier={throne['modifier']:.6f} "
                f"effective={throne['effective_min_decimal']} display(game/tunklab)="
                f"{throne['display_min']:g}/{throne['tunklab_display_min']:g} "
                f"(expected 4%; old raw-only display would be {raw_only_display}%)"
            ),
        )
        check(
            "idol.throne_of_ambition.old_import_explained",
            raw_only_display == 1,
            "raw-only display of 0.008 is 1%, which is exactly what the old import used; "
            "the DB applies the Adorned Idol affixEffectModifier (-0.05) and the affix's "
            "standardAffixEffectModifier (-0.83) to get 4.4706% -> 4%",
        )
        # Both roll slots (all resistances for you / for minions) carry the same range.
        slot1 = _fetch_range(connection, 1070, 33, 1, 1)
        check(
            "idol.throne_of_ambition.both_roll_slots",
            slot1 is not None and slot1["display_min"] == 4,
            "both affixProperties slots of affix 1070 are 4% on Adorned Idol",
        )

    # -- Omen idol special path -------------------------------------------
    omen_constant_row = connection.execute(
        "SELECT value_json FROM equipment_globals WHERE key='omenIdolAffixEffectModifier'"
    ).fetchone()
    check(
        "omen.equipment_constant",
        omen_constant_row is not None and json.loads(omen_constant_row[0]) == 0,
        f"tunklab equipment.omenIdolAffixEffectModifier={omen_constant_row[0] if omen_constant_row else None} (expected 0)",
    )
    sub_bases = list(
        connection.execute(
            "SELECT sub_type_id, slug, affix_effectiveness FROM equipment_sub_bases "
            "WHERE base_type_id=29 AND affix_effectiveness=1 ORDER BY sub_type_id"
        )
    )
    check(
        "omen.sub_bases_flagged",
        [row["sub_type_id"] for row in sub_bases] == [10, 11, 12, 13, 14],
        f"affixEffectiveness=1 sub-bases of Grand Idol: {[row['slug'] for row in sub_bases]}",
    )
    omen_row = connection.execute(
        "SELECT * FROM omen_special_ranges WHERE affix_id=111 AND base_type_id=29 AND sub_type_id=11 AND tier=1 AND roll_slot=0"
    ).fetchone()
    if omen_row is None:
        check("omen.affix111_range", False, "derived Omen row missing")
    else:
        check(
            "omen.affix111_range",
            abs(omen_row["modifier"] - 1.0) < 1e-12
            and omen_row["effective_min_decimal"] == "0.2"
            and omen_row["effective_max_decimal"] == "0.45"
            and omen_row["display_min"] == 20
            and omen_row["display_max"] == 45
            and omen_row["tunklab_display_min"] == 20
            and omen_row["tunklab_display_max"] == 45,
            (
                f"Grand Arcane Omen Idol x affix 111: raw={omen_row['raw_min']:g}-{omen_row['raw_max']:g} "
                f"modifier={omen_row['modifier']:g} effective={omen_row['effective_min_decimal']}.."
                f"{omen_row['effective_max_decimal']} display={omen_row['display_min']:g}-{omen_row['display_max']:g}% "
                "(expected 20-45%)"
            ),
        )
        check(
            "omen.eligibility_marked_unresolved",
            omen_row["derived"] == 1 and str(omen_row["eligibility_status"]).startswith("unresolved"),
            f"derived={omen_row['derived']} eligibility_status={omen_row['eligibility_status']}",
        )
        check(
            "omen.evidence_present",
            "affixEffectiveness" in omen_row["evidence"] and "omenIdolAffixEffectModifier" in omen_row["evidence"],
            "evidence cites tunklab affixEffectiveness/omenIdolAffixEffectModifier and external planner/save evidence",
        )
    check(
        "omen.tunklab_canrollon_excludes_grand",
        _range_absent(connection, 111, 29, 1)
        and connection.execute(
            "SELECT COUNT(*) FROM affix_eligible_bases WHERE affix_id=111 AND base_type_id=29"
        ).fetchone()[0]
        == 0,
        "affix 111 canRollOn does not include baseTypeID 29 -> the path is derived, not a tunklab range",
    )
    return checks


def _manifest_checks(manifest_path: Path) -> list[CheckResult]:
    checks: list[CheckResult] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [CheckResult("manifest.readable", False, repr(exc))]
    checks.append(CheckResult("manifest.readable", True, str(manifest_path)))
    root = manifest_path.parent
    for artifact in manifest.get("artifacts", []):
        path = root / artifact["path"]
        if not path.is_file():
            checks.append(CheckResult(f"manifest.file:{artifact['path']}", False, "missing"))
            continue
        if path.stat().st_size != artifact["size_bytes"]:
            checks.append(CheckResult(f"manifest.size:{artifact['path']}", False, "size mismatch"))
            continue
        checks.append(
            CheckResult(
                f"manifest.sha256:{artifact['path']}",
                sha256_file(path) == artifact["sha256"],
                artifact["sha256"][:16],
            )
        )
    return checks


def report_to_text(report: ValidationReport) -> str:
    lines = []
    for check in report.checks:
        status = "PASS" if check.ok else ("WARN" if check.severity in ("warning", "info") else "FAIL")
        lines.append(f"[{status}] {check.name}" + (f": {check.detail}" if check.detail else ""))
    lines.append("")
    lines.append(f"RESULT: {'PASS' if report.ok else 'FAIL'} ({len(report.failures)} failures, {len(report.checks)} checks)")
    return "\n".join(lines)
