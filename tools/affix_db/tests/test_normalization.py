"""Normalization tests (extraction -> relational rows) against the frozen snapshot."""

from __future__ import annotations

import sys
import pathlib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _support  # noqa: E402

from affix_db.build import build  # noqa: E402
from affix_db.normalize import normalize  # noqa: E402
from affix_db.extract import extract_snapshot  # noqa: E402


@unittest.skipUnless(_support.SNAPSHOT_DIR.is_dir(), "frozen source snapshot not present")
class NormalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.extraction = extract_snapshot(_support.SNAPSHOT_DIR / "chunks")
        cls.normalized = normalize(cls.extraction)

    def test_counts(self) -> None:
        counts = self.normalized.counts()
        self.assertEqual(counts["affixes"], 1156)
        self.assertEqual(counts["equipment_types"], 47)  # 39 equippable + 8 non-equippable
        self.assertEqual(counts["equipment_sub_bases"], 1570)
        self.assertEqual(counts["equipment_globals"], 7)
        self.assertEqual(counts["affix_tiers"], 1156)
        self.assertEqual(counts["affix_base_ranges"], 21533)
        self.assertEqual(counts["affix_eligible_bases"], 2539)
        self.assertEqual(counts["omen_special_ranges"], 5)
        self.assertEqual(counts["unresolved_can_roll_on"], 2)

    def test_every_affix_has_ranges(self) -> None:
        self.assertEqual(self.normalized.stats["affixes_without_ranges"], 0)
        self.assertEqual(self.normalized.stats["affixes_with_ranges"], 1156)
        self.assertEqual(self.normalized.issues, [])

    def test_unresolved_bases_are_the_pseudo_ids(self) -> None:
        unresolved = {row["base_type_id"] for row in self.normalized.unresolved_can_roll_on}
        self.assertEqual(unresolved, {11, 24})
        for row in self.normalized.unresolved_can_roll_on:
            self.assertEqual(row["excluded_by_tunklab"], 1)

    def test_no_range_rows_for_unresolved_or_excluded_bases(self) -> None:
        for row in self.normalized.affix_base_ranges:
            self.assertNotIn(row["base_type_id"], (11, 24, 105, 40))

    def test_fire_resistance_ranges_exist_for_all_eligible_slots(self) -> None:
        rows = [
            row
            for row in self.normalized.affix_base_ranges
            if row["affix_id"] == 13 and row["tier"] == 7 and row["roll_slot"] == 0
        ]
        base_ids = {row["base_type_id"] for row in rows}
        self.assertEqual(base_ids, {0, 1, 2, 3, 4, 17, 18, 19, 20, 21, 22})
        by_base = {row["base_type_id"]: row for row in rows}
        self.assertEqual(by_base[1]["effective_min_decimal"], "0.915")
        self.assertEqual(by_base[1]["effective_max_decimal"], "1.125")
        self.assertEqual(by_base[1]["display_max"], 112)
        self.assertEqual(by_base[1]["tunklab_display_max"], 113)
        self.assertEqual(by_base[0]["display_min"], 61)
        self.assertEqual(by_base[0]["display_max"], 75)
        self.assertEqual(by_base[18]["tunklab_display_min"], 71)
        self.assertEqual(by_base[18]["tunklab_display_max"], 88)

    def test_hybrid_affix_has_multiple_roll_slots(self) -> None:
        rows = [
            row
            for row in self.normalized.affix_base_ranges
            if row["affix_id"] == 14 and row["base_type_id"] == 20 and row["tier"] == 1
        ]
        self.assertEqual(len(rows), 2)
        slots = sorted(row["roll_slot"] for row in rows)
        self.assertEqual(slots, [0, 1])
        extra = [row for row in rows if row["roll_slot"] == 1][0]
        self.assertAlmostEqual(extra["raw_min"], 0.05, places=12)
        self.assertEqual(extra["tunklab_display_min"], 6)

    def test_affix_master_fields_are_preserved(self) -> None:
        affix = next(row for row in self.normalized.affixes if row["affix_id"] == 13)
        self.assertEqual(affix["slug"], "fire_resistance")
        self.assertEqual(affix["name"], "Fire Resistance")
        self.assertEqual(affix["title"], "of Embers")
        self.assertEqual(affix["type_name"], "Suffix")
        self.assertEqual(affix["standard_affix_effect_modifier"], 0.0)
        self.assertEqual(affix["t6_compatibility"], 2)
        self.assertEqual(affix["can_roll_on"], [0, 1, 2, 3, 4, 18, 19, 20, 21, 22, 17])
        self.assertEqual(len(affix["specific_reroll_chances"]), 2)
        # raw payload is preserved as-is
        self.assertIn("tiers", affix["raw"])
        self.assertEqual(affix["raw"]["affixId"], 13)

    def test_equipment_types(self) -> None:
        by_id = {row["base_type_id"]: row for row in self.normalized.equipment_types}
        body_armor = by_id[1]
        self.assertEqual(body_armor["base_type_name"], "Body Armor")
        self.assertEqual(body_armor["affix_effect_modifier"], 0.5)
        self.assertEqual(body_armor["grid_x"], 2)
        self.assertEqual(body_armor["grid_y"], 3)
        self.assertEqual(body_armor["maximum_affixes"], 4)
        self.assertEqual(by_id[101]["is_equippable"], 0)

    def test_stat_property_tables(self) -> None:
        self.assertEqual(len(self.normalized.player_properties), 113)
        fire = next(row for row in self.normalized.player_properties if row["property_id"] == 13)
        self.assertEqual(fire["property_name"], "Fire Resistance")
        self.assertEqual(fire["display_added_as_percentage"], 1)
        self.assertEqual(fire["rounding_for_added"], 0)
        self.assertEqual(fire["rounding_for_added_name"], "Hundredth")

    # -- idol specific ---------------------------------------------------
    def test_throne_of_ambition_all_resistance_is_4_percent(self) -> None:
        rows = [
            row
            for row in self.normalized.affix_base_ranges
            if row["affix_id"] == 1070 and row["base_type_id"] == 33 and row["tier"] == 1
        ]
        self.assertEqual(len(rows), 2)  # two roll slots (self + minions)
        for row in rows:
            self.assertEqual(row["raw_min"], 0.008)
            self.assertAlmostEqual(row["modifier"], 95 / 17, places=12)
            self.assertEqual(row["effective_min_decimal"], "0.04470588235294118")
            self.assertEqual(row["display_min"], 4)
            self.assertEqual(row["display_max"], 4)
            self.assertEqual(row["tunklab_display_min"], 4)
            self.assertEqual(row["display_unit"], "%")

    def test_equipment_globals_include_omen_modifier(self) -> None:
        globals_by_key = {row["key"]: row["value"] for row in self.normalized.equipment_globals}
        self.assertIn("omenIdolAffixEffectModifier", globals_by_key)
        self.assertEqual(globals_by_key["omenIdolAffixEffectModifier"], 0)
        self.assertIn("globalIdolRerollChance", globals_by_key)

    def test_omen_sub_bases_flagged(self) -> None:
        flagged = [
            row
            for row in self.normalized.equipment_sub_bases
            if row["base_type_id"] == 29 and row["affix_effectiveness"] == 1
        ]
        self.assertEqual(sorted(row["sub_type_id"] for row in flagged), [10, 11, 12, 13, 14])
        self.assertTrue(all(row["only_drop_when_requiring_affix_effectiveness"] == 1 for row in flagged))

    def test_omen_special_path_for_affix_111(self) -> None:
        rows = [
            row
            for row in self.normalized.omen_special_ranges
            if row["affix_id"] == 111 and row["sub_type_id"] == 11
        ]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["sub_base_slug"], "grand_arcane_omen_idol")
        self.assertEqual(row["raw_min"], 0.2)
        self.assertEqual(row["raw_max"], 0.45)
        self.assertEqual(row["omen_affix_effect_modifier"], 0)
        self.assertEqual(row["modifier"], 1.0)
        self.assertEqual(row["effective_min_decimal"], "0.2")
        self.assertEqual(row["effective_max_decimal"], "0.45")
        self.assertEqual((row["display_min"], row["display_max"]), (20, 45))
        self.assertEqual((row["tunklab_display_min"], row["tunklab_display_max"]), (20, 45))
        self.assertEqual(row["derived"], 1)
        self.assertTrue(row["eligibility_status"].startswith("unresolved"))
        self.assertIn("affixEffectiveness", row["evidence"])
        # tunklab's regular canRollOn-based ranges intentionally do NOT include it.
        self.assertFalse(
            any(
                row["affix_id"] == 111 and row["base_type_id"] == 29
                for row in self.normalized.affix_base_ranges
            )
        )


@unittest.skipUnless(_support.SNAPSHOT_DIR.is_dir(), "frozen source snapshot not present")
class NormalizedBuildTest(unittest.TestCase):
    def test_build_from_snapshot_produces_expected_manifest_counts(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = build(_support.SNAPSHOT_DIR, tmp, progress=lambda message: None)
            self.assertEqual(result.manifest["counts"]["affixes"], 1156)
            self.assertEqual(result.manifest["counts"]["affix_base_ranges"], 21533)
            self.assertEqual(result.manifest["integrity_problems"], [])
            self.assertTrue(result.sqlite_path.is_file())
            self.assertTrue((pathlib.Path(tmp) / "README.md").is_file())
            self.assertTrue((pathlib.Path(tmp) / "manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
