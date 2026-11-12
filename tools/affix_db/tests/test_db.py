"""Query/validation tests against the delivered SQLite database."""

from __future__ import annotations

import sys
import pathlib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _support  # noqa: E402

from affix_db.query import (  # noqa: E402
    all_bases,
    connect,
    get_affix,
    get_base_type,
    is_eligible,
    query,
    query_omen_path,
)
from affix_db.validate import report_to_text, validate_db  # noqa: E402


@unittest.skipUnless(_support.DB_PATH.is_file(), "built affix DB not present (run `build` first)")
class DatabaseQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.connection = connect(_support.DB_PATH)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.close()

    # -- acceptance ------------------------------------------------------
    def test_fire_resistance_body_armor_t7(self) -> None:
        result = query(self.connection, "fire_resistance", "Body Armor", 7)
        self.assertTrue(result.eligible)
        self.assertEqual(len(result.rows), 1)
        row = result.rows[0]
        self.assertEqual(row["affix_id"], 13)
        self.assertEqual(row["base_type_id"], 1)
        self.assertAlmostEqual(row["raw_min"], 0.61, places=12)
        self.assertAlmostEqual(row["raw_max"], 0.75, places=12)
        self.assertAlmostEqual(row["modifier"], 1.5, places=12)
        self.assertEqual(row["effective_min_decimal"], "0.915")
        self.assertEqual(row["effective_max_decimal"], "1.125")
        # game-facing display: Unity/.NET round-half-to-even -> 92..112
        self.assertEqual(row["display_min"], 92)
        self.assertEqual(row["display_max"], 112)
        # tunklab page display: JS Math.round -> 92..113
        self.assertEqual(row["tunklab_display_min"], 92)
        self.assertEqual(row["tunklab_display_max"], 113)
        self.assertEqual(row["display_unit"], "%")
        self.assertIn("standardAffixEffectModifier", row["formula"])

    def test_same_affix_other_slots_have_different_ranges(self) -> None:
        helmet = query(self.connection, 13, "Helmet", 7).rows[0]
        shield = query(self.connection, 13, 18, 7).rows[0]
        amulet = query(self.connection, 13, "Amulet", 7).rows[0]
        self.assertEqual(helmet["display_min"], 61)
        self.assertEqual(helmet["display_max"], 75)
        self.assertAlmostEqual(shield["modifier"], 1.17, places=12)
        self.assertEqual(amulet["tunklab_display_min"], 71)
        self.assertEqual(amulet["tunklab_display_max"], 88)
        self.assertNotEqual(helmet["effective_max_decimal"], shield["effective_max_decimal"])

    def test_all_tiers_for_body_armor(self) -> None:
        result = query(self.connection, 13, 1)
        self.assertEqual(len(result.rows), 8)
        displays = [(row["tier"], row["display_min"], row["display_max"]) for row in result.rows]
        self.assertEqual(
            displays,
            [(1, 8, 14), (2, 15, 21), (3, 22, 32), (4, 33, 43), (5, 45, 68),
             (6, 75, 90), (7, 92, 112), (8, 180, 225)],
        )

    def test_ineligible_base_is_excluded(self) -> None:
        result = query(self.connection, 13, 9)  # 1H Sword
        self.assertFalse(result.eligible)
        self.assertEqual(result.rows, [])
        self.assertFalse(is_eligible(self.connection, 13, 9))

    def test_all_bases_returns_every_eligible_base(self) -> None:
        bases = all_bases(self.connection, 13)
        base_ids = set(bases)
        self.assertEqual(base_ids, {0, 1, 2, 3, 4, 17, 18, 19, 20, 21, 22})
        self.assertEqual(len(bases[1]["rows"]), 8)
        self.assertEqual(bases[1]["base_type"]["display_name"], "Body Armor")

    def test_lookup_by_id_name_and_sub_base_slug(self) -> None:
        self.assertEqual(get_affix(self.connection, 13)["slug"], "fire_resistance")
        self.assertEqual(get_affix(self.connection, "fire_resistance")["affix_id"], 13)
        self.assertEqual(get_affix(self.connection, "Fire Resistance")["affix_id"], 13)
        self.assertEqual(get_base_type(self.connection, 1)["display_name"], "Body Armor")
        self.assertEqual(get_base_type(self.connection, "Body Armor")["base_type_id"], 1)
        # sub-base slug lookup (Refuge Armor is a Body Armor sub-item)
        self.assertEqual(get_base_type(self.connection, "refuge_armor")["base_type_id"], 1)

    def test_missing_affix_raises(self) -> None:
        from affix_db.query import QueryError

        with self.assertRaises(QueryError):
            get_affix(self.connection, "definitely_not_an_affix")

    def test_other_affix_slots(self) -> None:
        # Added Health (25): flat integer display, body armour T5
        health = query(self.connection, "added_health", 1, 5).rows[0]
        self.assertEqual(health["display_unit"], "")
        self.assertEqual(health["effective_min_decimal"], "91.5")
        self.assertEqual(health["display_min"], 92)
        self.assertEqual(health["tunklab_display_min"], 92)
        # Added Melee Physical Damage (63): 1H Axe factor is exactly 1, 2H Axe much higher
        one_hand = query(self.connection, "added_melee_physical_damage", 5, 1).rows[0]
        two_hand = query(self.connection, "added_melee_physical_damage", 12, 1).rows[0]
        self.assertAlmostEqual(one_hand["modifier"], 1.0, places=12)
        self.assertAlmostEqual(two_hand["modifier"], 3.2 / 1.75, places=12)
        self.assertGreater(two_hand["display_max"], one_hand["display_max"])
        # Increased Movement Speed (28) rolls on Boots only
        self.assertEqual(set(all_bases(self.connection, 28)), {3})

    # -- idol specific ---------------------------------------------------
    def test_throne_of_ambition_all_resistance(self) -> None:
        result = query(self.connection, "idol_all_resistances_for_you_and_your_minions", "Adorned Idol", 1)
        self.assertTrue(result.eligible)
        self.assertEqual(len(result.rows), 2)
        for row in result.rows:
            self.assertEqual(row["effective_min_decimal"], "0.04470588235294118")
            self.assertEqual(row["display_min"], 4)
            self.assertEqual(row["display_max"], 4)
            self.assertEqual(row["tunklab_display_min"], 4)
            self.assertEqual(row["display_unit"], "%")
        # The raw roll alone would display as 1% -- exactly the old import bug.
        from affix_db.formula import js_math_round

        self.assertEqual(js_math_round(100 * 0.008), 1)

    def test_omen_special_path(self) -> None:
        result = query_omen_path(self.connection, "idol_fire_resistance", "grand_arcane_omen_idol")
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertEqual(row["base_type_id"], 29)
        self.assertEqual(row["sub_type_id"], 11)
        self.assertEqual(row["raw_min"], 0.2)
        self.assertEqual(row["raw_max"], 0.45)
        self.assertEqual(row["modifier"], 1.0)
        self.assertEqual(row["effective_min_decimal"], "0.2")
        self.assertEqual(row["effective_max_decimal"], "0.45")
        self.assertEqual(row["display_min"], 20)
        self.assertEqual(row["display_max"], 45)
        self.assertEqual(row["derived"], 1)
        self.assertTrue(row["eligibility_status"].startswith("unresolved"))
        # not part of the regular canRollOn-derived ranges
        self.assertEqual(query(self.connection, "idol_fire_resistance", 29, 1).rows, [])


@unittest.skipUnless(_support.DB_PATH.is_file(), "built affix DB not present")
class ValidationTests(unittest.TestCase):
    def test_validation_checks_pass(self) -> None:
        report = validate_db(_support.DB_PATH, _support.MANIFEST_PATH)
        self.assertTrue(report.ok, msg=report_to_text(report))
        names = {check.name for check in report.checks}
        for expected in (
            "acceptance.fire_res_body_armor_t7.effective",
            "acceptance.fire_res_body_armor_t7.display_game",
            "acceptance.fire_res_body_armor_t7.display_tunklab",
            "acceptance.fire_res_helmet_t7_unscaled",
            "eligibility.fire_res_not_on_sword",
            "idol.throne_of_ambition.all_res_adorned",
            "omen.affix111_range",
            "omen.eligibility_marked_unresolved",
            "omen.tunklab_canrollon_excludes_grand",
        ):
            self.assertIn(expected, names)
        self.assertEqual(report.stats["half_even_vs_half_up_endpoint_differences"], 913)

    def test_manifest_hashes_are_valid(self) -> None:
        import json

        manifest = json.loads(_support.MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(manifest["artifact"], "affix-db")
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["counts"]["affix_base_ranges"], 21533)
        self.assertEqual(manifest["counts"]["omen_special_ranges"], 5)
        self.assertEqual(manifest["source"]["data_version"], "1.5.2")
        self.assertEqual(len(manifest["artifacts"]), 12)
        paths = {artifact["path"] for artifact in manifest["artifacts"]}
        self.assertIn("normalized/index/omen-special-ranges.json", paths)
        self.assertIn("normalized/index/equipment-globals.json", paths)
        for artifact in manifest["artifacts"]:
            self.assertEqual(len(artifact["sha256"]), 64, msg=artifact["path"])


if __name__ == "__main__":
    unittest.main()
