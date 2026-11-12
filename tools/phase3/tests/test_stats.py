"""Tests for the static snapshot builder (``le_calc.stats``).

Uses the real frozen inputs (read-only) so the snapshot format is exercised
against the delivered fixpoint.  No file writes outside ``tempfile`` and no
network access.
"""

from __future__ import annotations

import json
import math
import unittest

from le_calc import model
from le_calc.inputs import load_bundle

from _dataset_asset import require_dataset_db
from le_calc.stats import (
    ATTRIBUTE_PER_POINT_EFFECTS,
    ATTRIBUTES_URL,
    SnapshotError,  # noqa: F401  (imported for API completeness / future tests)
    build_snapshot,
    compute_attributes,
    compute_resistances,
    snapshot_json,
)

EXPECTED_RESISTANCES = {
    "fire": {"total": 856.0, "cap": 75.0, "capped": 75.0, "overcapped": 781.0},
    "cold": {"total": 161.0, "cap": 75.0, "capped": 75.0, "overcapped": 86.0},
    "lightning": {"total": 160.0, "cap": 75.0, "capped": 75.0, "overcapped": 85.0},
    "physical": {"total": 89.0, "cap": 75.0, "capped": 75.0, "overcapped": 14.0},
    "void": {"total": 66.0, "cap": 75.0, "capped": 66.0, "overcapped": 0.0},
    "necrotic": {"total": 102.0, "cap": 75.0, "capped": 75.0, "overcapped": 27.0},
    "poison": {"total": 84.0, "cap": 75.0, "capped": 75.0, "overcapped": 9.0},
    "elemental": {"total": 70.0, "cap": 75.0, "capped": 70.0, "overcapped": 0.0},
    "all": {"total": 21.0, "cap": 75.0, "capped": 21.0, "overcapped": 0.0},
}



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_dataset_db()
        cls.bundle = load_bundle(verify_database_file=False)
        cls.snapshot = build_snapshot(cls.bundle)
        cls.rows = {c.contribution_id: c for c in cls.snapshot.contributions}

    # -- helpers ------------------------------------------------------------
    def contrib(self, contribution_id: str) -> model.Contribution:
        self.assertIn(contribution_id, self.rows, f"missing contribution {contribution_id}")
        return self.rows[contribution_id]

    def mod_rows(self) -> list[model.Contribution]:
        return [c for c in self.snapshot.contributions if c.contribution_id.startswith("mod:")]

    def cover_mods(self) -> set[str]:
        covered: set[str] = set()
        for contribution in self.mod_rows():
            covered.add(contribution.contribution_id[len("mod:"):].split(":")[0])
        return covered

    # -- identity / fingerprints -------------------------------------------
    def test_build_meta_has_report_contract_keys(self) -> None:
        meta = self.snapshot.build_meta
        for key in ("build", "dataset", "calc_input"):
            self.assertIn(key, meta)
        for key in (
            "build_spec_sha256",
            "calc_input_sha256",
            "database_sha256",
            "dataset_logical_content_sha256",
            "content_sha256",
        ):
            self.assertIn(key, meta)
            self.assertTrue(meta[key])
        self.assertEqual(meta["build"]["revision_id"], "rev-c746411ccebee9c0")
        self.assertEqual(
            meta["build"]["content_sha256"],
            "c746411ccebee9c04b67da4a40cfc21d1e6770530e284bd2de8160ed0199932b",
        )
        self.assertEqual(meta["build"]["revision_seq"], 8)
        self.assertEqual(meta["build"]["previous_revision_id"], "rev-28c4b4efac800bed")
        self.assertEqual(meta["calc_input"]["mod_count"], 148)
        self.assertEqual(
            meta["calc_input"]["sha256"],
            "531f7a50ec338cf0765bf0008a05098f044e38f5e134420241f149ef4f9aa561",
        )
        self.assertEqual(meta["dataset"]["target_game_version"], "1.5.2")
        self.assertFalse(meta["revision_selection"]["current_pointer_drift"])
        self.assertEqual(
            meta["revision_selection"]["pinned_from"], "calculation-input.revision_id"
        )

    def test_character_uses_catalog_class_values(self) -> None:
        character = self.snapshot.character
        self.assertEqual(character["level"], 100)
        self.assertEqual(character["class"], "Mage")
        self.assertEqual(character["mastery"], "Sorcerer")
        self.assertEqual(character["class_entity_id"], "class:letools:classInfoForSources:1")
        self.assertEqual(character["class_base"]["baseMana"], 50)
        self.assertEqual(character["class_base"]["manaPerLevel"], 0.50506)
        self.assertEqual(character["class_base"]["baseIntelligence"], 3)
        self.assertEqual(character["class_base"]["baseHealth"], 100)
        self.assertIn("attunement", ATTRIBUTE_PER_POINT_EFFECTS)
        self.assertEqual(character["attribute_per_point_source"], ATTRIBUTES_URL)

    # -- coverage -----------------------------------------------------------
    def test_all_mods_have_ledger_rows(self) -> None:
        expected = {mod["mod_id"] for mod in self.bundle.mods}
        self.assertEqual(len(expected), 148)
        self.assertEqual(self.cover_mods(), expected)
        # 137 mod rows + 2 doubled-if-300-mana split rows + 11 description rows
        self.assertEqual(len(self.mod_rows()), 150)
        self.assertEqual(
            sum(1 for contribution in self.mod_rows()
                if contribution.stat_name == "Unique tooltip description"),
            11,
        )

    def test_all_19_passive_nodes_and_17_skill_nodes_are_represented(self) -> None:
        passive_nodes = {
            c.source_path.rsplit(".", 1)[-1]
            for c in self.snapshot.contributions
            if c.contribution_id.startswith("passive:mg-1:")
        }
        self.assertEqual(len(passive_nodes), 19)
        skill_nodes = {
            c.source_path.rsplit(".", 1)[-1]
            for c in self.snapshot.contributions
            if c.contribution_id.startswith("skill:bh2:")
        }
        self.assertEqual(len(skill_nodes), 17)
        self.assertEqual(
            skill_nodes,
            {"2", "3", "4", "6", "7", "9", "10", "15", "17", "20", "21", "23", "24", "25", "27", "28", "30"},
        )

    def test_every_contribution_has_evidence_and_mod_rows_identify_mod_id(self) -> None:
        for contribution in self.snapshot.contributions:
            self.assertTrue(contribution.evidence, f"no evidence for {contribution.contribution_id}")
        mod_id_by_contribution = {}
        for mod in self.bundle.mods:
            mod_id_by_contribution[f"mod:{mod['mod_id']}"] = mod["mod_id"]
        for contribution in self.mod_rows():
            expected_mod_id = mod_id_by_contribution.get(contribution.contribution_id)
            if expected_mod_id is None:  # :doubled suffix row
                expected_mod_id = mod_id_by_contribution[contribution.contribution_id.rsplit(":", 1)[0]]
            haystack = " ".join(
                f"{evidence.detail} {evidence.locator or ''}" for evidence in contribution.evidence
            )
            self.assertIn(expected_mod_id, haystack)

    def test_every_excluded_row_has_reason_and_every_conditional_row_has_code(self) -> None:
        for contribution in self.snapshot.contributions:
            if not contribution.included:
                self.assertTrue(
                    contribution.exclusion_reason,
                    f"excluded row without reason: {contribution.contribution_id}",
                )
            if contribution.condition_code:
                self.assertIn(
                    contribution.condition_status,
                    (
                        model.CONDITION_ALWAYS,
                        model.CONDITION_SATISFIED,
                        model.CONDITION_NOT_SATISFIED,
                        model.CONDITION_UNKNOWN,
                    ),
                )
            self.assertFalse(math.isnan(float(contribution.value)))

    # -- attributes ---------------------------------------------------------
    def test_attributes(self) -> None:
        self.assertEqual(
            self.snapshot.attributes,
            {
                "strength": 10.0,
                "vitality": 18.0,
                "intelligence": 42.0,
                "dexterity": 2.0,
                "attunement": 2.0,
            },
        )
        self.assertEqual(list(self.snapshot.attributes), list(model.ATTRIBUTE_ORDER))

    def test_attribute_sources(self) -> None:
        # passives: Arcanist 8 + Calculated Destruction 8 + Arcane Warden 5 +
        # Arcane Insight 8 + Arcane Focus 8 = 37 Intelligence from passives
        passive_int = sum(
            c.value
            for c in self.snapshot.contributions
            if c.stat_key == model.STAT_ATTRIBUTE
            and (c.target or "") == "intelligence"
            and c.source_kind == "passive_node"
            and c.active
        )
        self.assertEqual(passive_int, 37.0)
        self.assertEqual(self.contrib("base:attribute:intelligence").value, 3.0)
        self.assertEqual(self.contrib("mod:equipment.ring2#implicit#1").value, 2.0)  # all attributes
        self.assertEqual(self.contrib("passive:mg-1:25:e0").value, 8.0)  # Chronomancy vitality
        self.assertEqual(self.contrib("mod:equipment.ring2#unique_mod#3").value, 8.0)  # Strength
        self.assertEqual(self.contrib("mod:equipment.ring2#unique_mod#4").value, 8.0)  # Vitality

    # -- resistances --------------------------------------------------------
    def test_resistance_totals_in_percentage_points(self) -> None:
        for name, expected in EXPECTED_RESISTANCES.items():
            row = self.snapshot.resistances[name]
            for key, value in expected.items():
                self.assertEqual(row[key], value, f"{name}.{key}")
        self.assertEqual(set(self.snapshot.resistances), set(model.RESISTANCE_NAMES))
        self.assertIn("note", self.snapshot.resistances["elemental"])
        self.assertIn("note", self.snapshot.resistances["all"])

    def test_resistance_breakdown_and_uncapped_helpers(self) -> None:
        fire = self.snapshot.resistances["fire"]
        self.assertEqual(fire["breakdown"], {"specific": 765.0, "elemental": 70.0, "all": 21.0})
        self.assertEqual(self.snapshot.uncapped_resistance("fire"), 856.0)
        cold = self.snapshot.resistances["cold"]
        self.assertEqual(cold["breakdown"], {"specific": 70.0, "elemental": 70.0, "all": 21.0})
        # vitality-derived poison/necrotic resistance is included
        self.assertEqual(self.snapshot.resistances["poison"]["total"], 84.0)

    def test_compute_resistances_ignores_inactive_rows(self) -> None:
        rows = [
            model.Contribution(
                contribution_id="test:res",
                source_kind="derived",
                source_label="test",
                source_path="test",
                stat_name="Fire Resistance",
                stat_key=model.STAT_RESISTANCE,
                operation="added",
                value=0.5,
                unit="percent",
                target="fire",
                included=False,
                exclusion_reason="test",
            )
        ]
        self.assertEqual(compute_resistances(rows)["fire"]["total"], 0.0)
        self.assertEqual(compute_attributes([])["intelligence"], 0.0)

    # -- mana ---------------------------------------------------------------
    def test_mana_max_and_breakdown(self) -> None:
        self.assertAlmostEqual(self.snapshot.derived["mana_max"], 219.7810434, places=6)
        sources = self.snapshot.derived["mana_max_sources"]
        self.assertAlmostEqual(sources["base_flat"], 100.00094, places=6)
        self.assertEqual(sources["flat_total"], 98.0)  # waist 32 + chest 24 + passives 20+18 + attunement 4
        self.assertEqual(sources["increased_total"], 0.11)
        self.assertIn("derived:attribute:attunement-mana", [row["contribution_id"] for row in sources["rows"]])
        self.assertEqual(self.contrib("base:mana:level100").status, model.STATUS_DERIVED)

    def test_300_mana_condition_is_not_satisfied(self) -> None:
        rows = [
            c
            for c in self.snapshot.contributions
            if c.condition_code == "max_mana_gte_300"
        ]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row.condition_status, model.CONDITION_NOT_SATISFIED)
            self.assertFalse(row.active)
        base_values = {
            self.contrib("mod:idols.2#affix#normal#1.0").value,
            self.contrib("mod:idols.3#affix#normal#1.0").value,
        }
        self.assertEqual(base_values, {0.4, 0.31})
        # each base row stays active at its base value
        self.assertTrue(self.contrib("mod:idols.2#affix#normal#1.0").active)

    # -- glove overcap-cold derivations -------------------------------------
    def test_glove_derived_overcapped_cold_rows(self) -> None:
        cdr = self.contrib("mod:equipment.hands#affix#sealed#0.0")
        cast = self.contrib("mod:equipment.hands#affix#primordial#0.0")
        self.assertEqual(cdr.status, model.STATUS_DERIVED)
        self.assertEqual(cast.status, model.STATUS_DERIVED)
        self.assertEqual(cdr.value, 0.1075)  # 1% x min(200, 86)/8
        self.assertEqual(cast.value, 0.172)  # 1% x min(200, 86)/5
        self.assertIn("continuous ratio", cdr.notes)
        self.assertIn("integer", cdr.notes)
        self.assertIn("integer", cast.notes)
        self.assertTrue(cdr.active and cast.active)

    # -- doubled fire rows --------------------------------------------------
    def test_doubled_fire_affixes_are_split(self) -> None:
        for mod_id in ("idols.2#affix#normal#1.0", "idols.3#affix#normal#1.0"):
            base = self.contrib(f"mod:{mod_id}")
            extra = self.contrib(f"mod:{mod_id}:doubled")
            self.assertEqual(base.condition_status, model.CONDITION_ALWAYS)
            self.assertTrue(base.active)
            self.assertEqual(extra.condition_code, "max_mana_gte_300")
            self.assertEqual(extra.condition_status, model.CONDITION_NOT_SATISFIED)
            self.assertFalse(extra.active)
            self.assertEqual(base.damage_tags, ["Fire"])

    # -- condition rows -----------------------------------------------------
    def test_apex_glyph_rows_are_not_satisfied(self) -> None:
        rows = [
            self.contrib("mod:equipment.head#unique_mod#0"),
            self.contrib("mod:equipment.head#unique_mod#2"),
        ]
        for row in rows:
            self.assertEqual(row.condition_code, "standing_on_glyph_of_dominion")
            self.assertEqual(row.condition_status, model.CONDITION_NOT_SATISFIED)
            self.assertFalse(row.active)

    def test_ambition_stays_at_zero_stacks(self) -> None:
        row = self.contrib("mod:idols.4#unique_mod#0")
        self.assertEqual(row.condition_code, "ambition_stack_from_boss_hit")
        self.assertEqual(row.condition_status, model.CONDITION_NOT_SATISFIED)
        self.assertFalse(row.active)
        # no active Ambition per-stack more damage is produced
        self.assertFalse(
            any(
                "ambition" in c.contribution_id.lower() and c.active
                for c in self.snapshot.contributions
            )
        )

    def test_crescendoing_rage_minimal_rule(self) -> None:
        row = self.contrib("mod:equipment.ring2#affix#sealed#0.0")
        self.assertEqual(row.stat_key, model.STAT_DAMAGE)
        self.assertEqual(row.operation, "more")
        self.assertEqual(row.value, 0.5)
        self.assertEqual(row.condition_code, "crescendoing_rage_third_cast")
        self.assertEqual(row.condition_status, model.CONDITION_UNKNOWN)
        self.assertIn("every third ability cast", row.notes)
        evidence_text = " ".join(e.detail for e in row.evidence)
        self.assertIn("Crescendoing Rage", evidence_text)

    def test_idol_altar_condition_is_unresolved_and_not_applied(self) -> None:
        row = self.contrib("mod:equipment.idol_altar#implicit#0")
        self.assertEqual(row.condition_status, model.CONDITION_UNKNOWN)
        self.assertFalse(row.included)
        self.assertEqual(row.status, model.STATUS_UNRESOLVED)
        self.assertIn("idol grid", row.exclusion_reason)
        self.assertEqual(self.snapshot.derived["increased_cooldown_recovery_speed_total"], 0.7575)

    def test_preparation_and_frozen_steel_are_conditional(self) -> None:
        preparation = self.contrib("passive:mg-1:4:buff-damage")
        self.assertEqual(preparation.value, 0.8)
        self.assertEqual(preparation.condition_code, "preparation_next_elemental_spell")
        self.assertEqual(preparation.condition_status, model.CONDITION_UNKNOWN)
        self.assertFalse(preparation.active)
        # Frozen Steel's third stat is normalised to property=-1 by the planner table: the
        # adapter must NOT interpret "6" as +600% (or any) global damage.
        frozen_steel = self.contrib("passive:mg-1:36:e2")
        self.assertEqual(frozen_steel.stat_key, model.STAT_UNSUPPORTED)
        self.assertFalse(frozen_steel.included)
        self.assertEqual(frozen_steel.condition_code, "enemy_chilled_or_frozen")
        self.assertEqual(frozen_steel.condition_status, model.CONDITION_UNKNOWN)
        self.assertEqual(frozen_steel.status, model.STATUS_UNRESOLVED)
        self.assertIn("property=-1", frozen_steel.exclusion_reason)
        self.assertIn("NOT interpreted", frozen_steel.exclusion_reason)
        self.assertTrue(
            any("Damage Against Chilled Or Frozen" in (e.detail or "") for e in frozen_steel.evidence)
        )
        # the catalog stat effect carries the real normalized value (0.06 = +6%),
        # while the planner table entry stores 6 with property=-1
        self.assertTrue(
            any("0.06" in (e.detail or "") for e in frozen_steel.evidence)
        )
        raw_entry = (frozen_steel.raw or {}).get("entry") or {}
        self.assertEqual(raw_entry.get("property"), -1)
        self.assertEqual(raw_entry.get("value"), 6)

    # -- weapon / skill-scope exclusions -----------------------------------
    def test_weapon_penetration_melee_and_minion_exclusions(self) -> None:
        fire_pen = self.contrib("mod:equipment.weapon1#affix#normal#0.0")
        self.assertEqual(fire_pen.stat_key, model.STAT_PENETRATION)
        self.assertEqual(fire_pen.value, 0.22)
        self.assertEqual(fire_pen.damage_tags, ["Fire"])
        minion_pen = self.contrib("mod:equipment.weapon1#affix#normal#0.1")
        self.assertFalse(minion_pen.included)
        self.assertIn("minion", minion_pen.exclusion_reason)
        melee = self.contrib("mod:equipment.weapon1#implicit#0")
        self.assertFalse(melee.included)
        self.assertIn("melee", melee.exclusion_reason)
        supernova = self.contrib("mod:equipment.chest#unique_mod#6")
        self.assertFalse(supernova.included)
        self.assertIn("Elemental Nova", supernova.exclusion_reason)

    def test_level_of_skills_rows_are_informational(self) -> None:
        rows = [c for c in self.mod_rows() if c.stat_name == "Level of Skills"]
        self.assertEqual(len(rows), 11)
        for row in rows:
            self.assertFalse(row.included)
            self.assertEqual(row.status, model.STATUS_CONFIRMED)
            self.assertIn("informational", row.exclusion_reason)

    def test_crit_and_minion_rows_are_not_consumed(self) -> None:
        crit = self.contrib("mod:equipment.amulet#implicit#0")  # critical strike chance
        self.assertFalse(crit.included)
        self.assertIn("hit=false", crit.exclusion_reason)
        minion_res = self.contrib("mod:idols.0#affix#corrupted#0.0")  # minion elemental res
        self.assertFalse(minion_res.included)
        self.assertIn("minion", minion_res.exclusion_reason)

    def test_ignite_duration_and_effect_rows(self) -> None:
        ignite_duration = self.contrib("mod:equipment.head#unique_mod#4")
        self.assertEqual(ignite_duration.stat_key, model.STAT_AILMENT_DURATION)
        self.assertEqual(ignite_duration.value, 0.52)
        self.assertEqual(ignite_duration.target, "ignite")
        self.assertTrue(ignite_duration.active)
        chill_duration = self.contrib("mod:equipment.head#unique_mod#5")
        self.assertFalse(chill_duration.included)
        spreading = self.contrib("mod:idols.0#affix#normal#0.1")
        self.assertFalse(spreading.included)
        self.assertIn("Spreading Flames", spreading.exclusion_reason)
        penetration_with_ignite = self.contrib("mod:equipment.feet#unique_mod#1")
        self.assertEqual(penetration_with_ignite.stat_key, model.STAT_PENETRATION_WITH_IGNITE)
        self.assertEqual(penetration_with_ignite.value, 0.35)
        self.assertEqual(penetration_with_ignite.target, "ignite")
        self.assertEqual(penetration_with_ignite.status, model.STATUS_CONFIRMED)
        self.assertTrue(penetration_with_ignite.included)

    # -- passives -----------------------------------------------------------
    def test_passive_per_point_scaling(self) -> None:
        self.assertEqual(self.contrib("passive:mg-1:0:e0").value, 8.0)  # Arcanist INT 1x8
        self.assertEqual(self.contrib("passive:mg-1:2:e0").value, 0.56)  # Elementalist fire 7%x8
        self.assertEqual(self.contrib("passive:mg-1:12:e0").value, 20.0)  # Mana Shell 4x5
        self.assertEqual(self.contrib("passive:mg-1:18:e0").value, 0.48)  # Pyromancer fire 6%x8
        self.assertEqual(self.contrib("passive:mg-1:18:e1").value, 0.4)  # ignite chance 5%x8
        self.assertEqual(self.contrib("passive:mg-1:23:e0").value, 0.64)  # Inferno fire DoT 8%x8
        self.assertEqual(self.contrib("passive:mg-1:23:e2").value, 0.15)  # 5-point threshold more
        self.assertEqual(self.contrib("passive:mg-1:25:e3").value, 0.4)  # Chronomancy ignite 5%x8
        self.assertEqual(self.contrib("passive:mg-1:56:e0").value, 18.0)  # Unsealed Mana 3x6
        self.assertEqual(self.contrib("passive:mg-1:56:e1").value, 0.18)  # cast speed 3%x6
        self.assertEqual(self.contrib("passive:mg-1:63:e0").value, 0.15)  # Lavamancer fire pen
        self.assertEqual(self.contrib("passive:mg-1:65:e0").value, 0.25)  # Recollection CDR
        self.assertEqual(self.contrib("passive:mg-1:32:e1").value, 5.0)  # Arcane Warden INT 1x5
        self.assertEqual(self.contrib("passive:mg-1:35:e1").value, 1.0)  # Infused Weapon spell dmg
        self.assertEqual(self.contrib("passive:mg-1:67:e0").value, 0.14)  # elemental res 2%x7
        self.assertEqual(self.contrib("passive:mg-1:33:e1").value, 0.18)  # elemental res 3%x6

    def test_passive_exclusions_have_reasons(self) -> None:
        for contribution_id in (
            "passive:mg-1:63:e1",  # fire leech
            "passive:mg-1:55:e0",  # health
            "passive:mg-1:36:e0",  # melee cold damage
            "passive:mg-1:20:e1",  # spell crit per INT
            "passive:mg-1:12:e1",  # ward per second
        ):
            row = self.contrib(contribution_id)
            self.assertFalse(row.included)
            self.assertTrue(row.exclusion_reason)

    def test_passive_planner_properties_are_audited_against_catalog(self) -> None:
        """Every included passive row must be a mapped property whose value matches the
        catalog node stat effect; property=-1 entries must never be interpreted."""
        from le_calc.catalog import Catalog

        allowed_properties = {0, 1, 3, 8, 13, 14, 15, 20, 21, 26, 27, 28, 30, 42, 43, 52, 59, 64, 70}
        with Catalog(self.bundle.database_path) as catalog:
            mg_tree = catalog.tree_by_source_id("mg-1")
            for contribution in self.snapshot.contributions:
                if contribution.source_kind != "passive_node":
                    continue
                if ":buff-" in contribution.contribution_id:
                    continue  # Preparation buff rows use the buff table, audited separately
                entry = (contribution.raw or {}).get("entry") or {}
                property_id = entry.get("property")
                node_id = contribution.source_path.rsplit(".", 1)[-1]
                entry_index = int(contribution.contribution_id.rsplit(":e", 1)[1])
                catalog_node = catalog.node_by_index(mg_tree["entity_id"], node_id)
                effects = catalog.effects_for_owner(catalog_node["entity_id"])
                matches = [
                    effect
                    for effect in effects
                    if str(effect.get("effect_id")).endswith(f"#stat#{entry_index}")
                ]
                self.assertEqual(len(matches), 1, contribution.contribution_id)
                values = [
                    float(value["value_numeric"])
                    for value in (matches[0].get("values") or [])
                    if value.get("value_numeric") is not None
                ]
                if contribution.included:
                    self.assertNotEqual(property_id, -1, contribution.contribution_id)
                    self.assertIn(property_id, allowed_properties, contribution.contribution_id)
                    self.assertTrue(values, contribution.contribution_id)
                    # per-point values in the planner table are fractions like the catalog
                    self.assertAlmostEqual(
                        float(entry.get("value")), values[0], places=9, msg=contribution.contribution_id
                    )
                else:
                    self.assertTrue(contribution.exclusion_reason)
                    if property_id == -1:
                        self.assertFalse(contribution.included)

    # -- skill nodes --------------------------------------------------------
    def test_skill_node_damage_multipliers(self) -> None:
        expected = {
            "skill:bh2:2:more": 0.6,
            "skill:bh2:7:more-fire": 0.3,
            "skill:bh2:10:more": 0.3,
            "skill:bh2:23:more-boss": 1.0,
            "skill:bh2:27:more-center": 0.6,
            "skill:bh2:28:more": 1.0,
        }
        for contribution_id, value in expected.items():
            row = self.contrib(contribution_id)
            self.assertEqual(row.source_kind, "skill_node")
            self.assertEqual(row.stat_key, model.STAT_DAMAGE)
            self.assertEqual(row.operation, "more")
            self.assertEqual(row.value, value)
            self.assertTrue(row.active)
        self.assertEqual(self.contrib("skill:bh2:7:more-fire").damage_tags, ["Fire"])
        self.assertEqual(self.contrib("skill:bh2:23:more-boss").condition_code, "target_is_boss_or_rare")
        self.assertEqual(self.contrib("skill:bh2:23:more-boss").condition_status, model.CONDITION_SATISFIED)
        weakened = self.contrib("skill:bh2:27:more-center")
        self.assertEqual(weakened.condition_code, "target_within_center_1m")
        self.assertEqual(weakened.condition_status, model.CONDITION_SATISFIED)
        self.assertIn("does not affect ailments", weakened.notes)

    def test_skill_node_duration_and_cooldown_rows(self) -> None:
        durations = {
            "skill:bh2:4:duration": 0.8,
            "skill:bh2:15:duration": 0.1,
            "skill:bh2:24:duration": 0.4,
            "skill:bh2:25:duration": 0.3,
        }
        for contribution_id, value in durations.items():
            row = self.contrib(contribution_id)
            self.assertEqual(row.stat_key, model.STAT_SKILL_DURATION_INCREASED)
            self.assertEqual(row.value, value)
        recurrence = self.contrib("skill:bh2:17:cooldown-reduction")
        self.assertEqual(recurrence.stat_key, model.STAT_COOLDOWN_REDUCTION)
        self.assertEqual(recurrence.value, 0.6)
        endless_maw = self.contrib("skill:bh2:25:cooldown-flat")
        self.assertEqual(endless_maw.stat_key, model.STAT_COOLDOWN_FLAT)
        self.assertEqual(endless_maw.value, 2.0)
        self.assertEqual(endless_maw.unit, "seconds")
        charges = self.contrib("skill:bh2:30:charges")
        self.assertEqual(charges.stat_key, model.STAT_CHARGES)
        self.assertEqual(charges.value, 1.0)
        self.assertEqual(charges.unit, "stack")

    def test_heat_wave_skill_node_is_a_flag_row(self) -> None:
        row = self.contrib("skill:bh2:6:flag-ignite-chance")
        self.assertEqual(row.stat_key, model.STAT_AILMENT_CHANCE)
        self.assertEqual(row.operation, "flag")
        self.assertEqual(row.target, "ignite")
        self.assertEqual(row.value, 5.0)
        self.assertIn("2%", row.notes)
        self.assertIn("uncapped", row.notes.lower())

    def test_skill_node_groups_are_node_scoped(self) -> None:
        for contribution in self.snapshot.contributions:
            if contribution.source_kind != "skill_node":
                continue
            self.assertIsNotNone(contribution.group)
            self.assertTrue(contribution.group.startswith("node:bh2:"))
            node_id = contribution.source_path.rsplit(".", 1)[-1]
            self.assertEqual(contribution.group, f"node:bh2:{node_id}")

    def test_binary_system_is_structural_not_an_extra_more(self) -> None:
        flag = self.contrib("skill:bh2:9:flag-binary")
        self.assertFalse(flag.included)
        self.assertIn("structural", flag.exclusion_reason)
        damage_flag = self.contrib("skill:bh2:9:flag-damage")
        self.assertIn("structural", damage_flag.exclusion_reason)
        # only Singularity contributes an active node:bh2:9-independent more for binary
        active_more_groups = {
            c.group for c in self.snapshot.contributions if c.active and c.operation == "more" and c.group
        }
        self.assertNotIn("node:bh2:9", active_more_groups)

    # -- derived summary ----------------------------------------------------
    def test_derived_summary_numbers(self) -> None:
        derived = self.snapshot.derived
        self.assertEqual(derived["uncapped_fire_resistance_percent"], 856.0)
        self.assertEqual(derived["overcapped_fire_resistance_percent"], 781.0)
        self.assertEqual(derived["overcapped_cold_resistance_percent"], 86.0)
        self.assertAlmostEqual(derived["mana_max"], 219.7810434, places=6)
        self.assertEqual(derived["intelligence"], 42.0)
        self.assertEqual(derived["attunement"], 2.0)
        self.assertEqual(derived["increased_cast_speed_total"], 0.352)
        self.assertEqual(derived["increased_cooldown_recovery_speed_total"], 0.7575)
        self.assertEqual(derived["cooldown_reduction_total"], 0.6)
        self.assertEqual(derived["increased_bh_duration_total"], 1.6)
        self.assertEqual(derived["skill_duration_s"], 7.15)
        self.assertEqual(derived["increased_ignite_duration_total"], 1.81)
        self.assertEqual(derived["ignite_duration_s"], 7.025)
        self.assertEqual(derived["increased_ignite_effect_total"], 0.0)
        self.assertEqual(derived["penetration_with_ignite_total"], 0.35)
        self.assertEqual(
            derived["penetration_with_ignite_rows"],
            ["mod:equipment.feet#unique_mod#1"],
        )
        self.assertEqual(derived["flat_added_spell_damage_total"], 66.0)
        self.assertEqual(derived["fire_penetration_total"], 0.41)
        self.assertEqual(derived["heat_wave_points"], 5)
        self.assertEqual(derived["heat_wave_expected_stacks_per_roll"], 85.6)
        self.assertEqual(derived["ignite_chance_per_second_percent"], 8560.0)
        self.assertEqual(derived["cooldown_flat_total_s"], 2.0)
        self.assertEqual(derived["extra_max_charges"], 1.0)
        self.assertEqual(derived["ailment_chance_ignite_total"], 0.4)
        self.assertAlmostEqual(
            derived["cast_time_s"], 1.0 / (1.8333375 * 0.8 * 1.352), places=9
        )

    def test_derived_idol_layout_from_build_positions(self) -> None:
        layout = self.snapshot.derived["idol_layout"]
        self.assertEqual(len(layout), 8)
        by_id = {row["instance_id"]: row for row in layout}
        self.assertEqual(by_id["idols.0"]["x"], 1)
        self.assertEqual(by_id["idols.0"]["y"], 3)
        self.assertEqual(by_id["idols.0"]["idol_size"], "Grand")
        self.assertEqual(by_id["idols.4"]["idol_size"], "Adorned")
        self.assertEqual(by_id["idols.5"]["idol_size"], "Minor")
        self.assertEqual(by_id["idols.4"]["x"], 4)
        self.assertEqual(by_id["idols.4"]["y"], 4)
        for row in layout:
            self.assertEqual(row["grid_validation"], "not_verified")
            self.assertIsNone(row["source_width"])

    def test_derived_rows_are_consistent_with_contributions(self) -> None:
        # cross-check the derived aggregates against the active contributions
        cast = sum(
            c.value
            for c in self.snapshot.active_contributions()
            if c.stat_key == model.STAT_CAST_SPEED and c.operation in ("increased", "added")
        )
        self.assertEqual(round(cast, 9), self.snapshot.derived["increased_cast_speed_total"])
        cdr = sum(
            c.value
            for c in self.snapshot.active_contributions()
            if c.stat_key == model.STAT_COOLDOWN_RECOVERY and c.operation in ("increased", "added")
        )
        self.assertEqual(round(cdr, 9), self.snapshot.derived["increased_cooldown_recovery_speed_total"])
        pen = sum(
            c.value
            for c in self.snapshot.active_contributions()
            if c.stat_key == model.STAT_PENETRATION and "Fire" in c.damage_tags
        )
        self.assertEqual(round(pen, 9), self.snapshot.derived["fire_penetration_total"])

    # -- excluded sources / issues -----------------------------------------
    def test_excluded_sources_cover_out_of_scope_trees(self) -> None:
        source_ids = {row["source_id"] for row in self.snapshot.excluded_sources}
        for tree_id in ("sb44eQ", "me27", "fw3d", "fr4wl"):
            self.assertIn(f"skill_tree:{tree_id}", source_ids)
        self.assertIn("skill_tree:weaver", source_ids)
        self.assertIn("active_buff:sb44eQ", source_ids)
        # 2026-11-04 re-review: 5 non-DPS drop-rate blessings are excluded upstream
        self.assertIn("blessing_non_dps:drop_rate", source_ids)
        for row in self.snapshot.excluded_sources:
            for key in ("source_id", "label", "reason", "category"):
                self.assertIn(key, row)
            self.assertEqual(row["category"], row["category"])

    def test_issues_are_well_formed(self) -> None:
        self.assertGreaterEqual(len(self.snapshot.issues), 10)
        for issue in self.snapshot.issues:
            for key in ("issue_id", "severity", "title", "detail", "scope"):
                self.assertIn(key, issue)
            self.assertIn(issue["severity"], ("info", "warning", "error"))
        self.assertFalse([i for i in self.snapshot.issues if i["severity"] == "error"])
        ids = [issue["issue_id"] for issue in self.snapshot.issues]
        self.assertEqual(len(ids), len(set(ids)))
        titles = " ".join(issue["title"] for issue in self.snapshot.issues)
        self.assertIn("non-DPS", titles)
        self.assertIn("revision pin", titles)

    # -- determinism / serialization ---------------------------------------
    def test_snapshot_json_is_deterministic_and_roundtrips(self) -> None:
        text_a = snapshot_json(self.snapshot)
        text_b = snapshot_json(build_snapshot(load_bundle(verify_database_file=False)))
        self.assertEqual(text_a, text_b)
        self.assertTrue(text_a.endswith("\n"))
        payload = json.loads(text_a)
        self.assertEqual(payload["build_meta"], self.snapshot.build_meta)
        restored = model.Snapshot.from_dict(payload)
        self.assertEqual(len(restored.contributions), len(self.snapshot.contributions))
        self.assertEqual(len(restored.active_contributions()), len(self.snapshot.active_contributions()))
        self.assertEqual(restored.attributes, self.snapshot.attributes)
        self.assertEqual(restored.resistances, self.snapshot.resistances)
        self.assertNotIn("generated_now", text_a)

    def test_snapshot_has_no_wall_clock_field(self) -> None:
        text = snapshot_json(self.snapshot)
        self.assertNotIn("retrieved_at", text)
        self.assertNotIn("computed_at", text)
        # only the frozen revision timestamps are allowed
        self.assertIn("2026-11-04T14:37:59.117920+00:00", text)


if __name__ == "__main__":
    unittest.main()
