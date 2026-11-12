"""Unit tests for the scaling formula and display-rounding semantics."""

from __future__ import annotations

import sys
import pathlib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _support  # noqa: E402,F401

from affix_db.formula import (  # noqa: E402
    IDOL_ALTAR_FRACTIONAL_TAGS,
    MOD_ADDED,
    MOD_INCREASED,
    MOD_MORE,
    PropertyDisplayInfo,
    compute_display,
    compute_modifier,
    exact_decimal_string,
    fraction_from_clean,
    fraction_to_decimal_string,
    game_display_value,
    js_math_round,
    range_summary,
    unity_round_to_int,
)

FIRE_RES_INFO = PropertyDisplayInfo(
    property_id=13,
    property_name="Fire Resistance",
    mod_type=MOD_ADDED,
    rounding_for_added=0,
    display_added_as_percentage=True,
    display_added_as_tenth_of_value=False,
    display_as_percentage_of=False,
    display_as_added_to=False,
    dont_display_plus=False,
    less_is_better=False,
)

FLAT_HEALTH_INFO = PropertyDisplayInfo(
    property_id=7,
    property_name="Health",
    mod_type=MOD_ADDED,
    rounding_for_added=1,
    display_added_as_percentage=False,
    display_added_as_tenth_of_value=False,
    display_as_percentage_of=False,
    display_as_added_to=False,
    dont_display_plus=False,
    less_is_better=False,
)


class ModifierTests(unittest.TestCase):
    def test_documented_modifiers(self) -> None:
        cases = [
            (0.0, 0.0, 1.0),
            (0.5, 0.0, 1.5),  # body armour vs standard affix
            (0.17, 0.0, 1.17),  # shield/catalyst/amulet
            (0.75, 0.0, 1.75),  # 1H weapon vs standard
            (2.2, 0.0, 3.2),  # 2H weapon vs standard
            (0.75, 0.75, 1.0),  # 1H weapon vs weapon-standard affix
            (2.2, 0.75, 3.2 / 1.75),
            (-0.83, -0.83, 1.0),  # idol base vs idol-standard affix
            (-0.62, -0.62, 1.0),
        ]
        for base_modifier, standard_modifier, expected in cases:
            with self.subTest(base=base_modifier, standard=standard_modifier):
                self.assertAlmostEqual(
                    compute_modifier(base_modifier, standard_modifier), expected, places=12
                )

    def test_zero_division_is_guarded(self) -> None:
        with self.assertRaises(ZeroDivisionError):
            compute_modifier(0.5, -1.0)


class RoundingTests(unittest.TestCase):
    def test_js_round_half_up(self) -> None:
        cases = [(7.5, 8), (22.5, 23), (13.5, 14), (112.5, 113), (2.5, 3), (-2.5, -2)]
        for value, expected in cases:
            self.assertEqual(js_math_round(value), expected, msg=f"js_math_round({value})")

    def test_unity_round_half_even(self) -> None:
        cases = [(7.5, 8), (22.5, 22), (13.5, 14), (112.5, 112), (2.5, 2), (-2.5, -2)]
        for value, expected in cases:
            self.assertEqual(unity_round_to_int(value), expected, msg=f"unity_round_to_int({value})")

    def test_exact_decimal_strings(self) -> None:
        self.assertEqual(exact_decimal_string(0.915), "0.915")
        self.assertEqual(exact_decimal_string(1.125), "1.125")
        self.assertEqual(fraction_to_decimal_string(fraction_from_clean(0.61) * fraction_from_clean(1.17)), "0.7137")
        self.assertEqual(exact_decimal_string(7.5), "7.5")


class DisplayTests(unittest.TestCase):
    def test_percentage_property(self) -> None:
        result = compute_display(0.075, FIRE_RES_INFO, MOD_ADDED)
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, 8)
        self.assertEqual(result.unit, "%")
        self.assertEqual(result.scale, 100)

    def test_flat_integer_property(self) -> None:
        result = compute_display(12.0, FLAT_HEALTH_INFO, MOD_ADDED)
        self.assertEqual(result.value, 12)
        self.assertEqual(result.unit, "")
        self.assertEqual(result.scale, 1)

    def test_increased_property_is_percentage(self) -> None:
        info = PropertyDisplayInfo(
            property_id=10,
            property_name="Armour",
            mod_type=MOD_INCREASED,
            rounding_for_added=0,
            display_added_as_percentage=False,
            display_added_as_tenth_of_value=False,
            display_as_percentage_of=False,
            display_as_added_to=False,
            dont_display_plus=False,
            less_is_better=False,
        )
        result = compute_display(0.405, info, MOD_INCREASED)
        self.assertEqual(result.value, 41)  # js round(40.499999...)? 0.405*100 = 40.5 -> 41
        self.assertEqual(result.unit, "%")

    def test_more_property_default_hundredth(self) -> None:
        info = PropertyDisplayInfo(
            property_id=0,
            property_name="Damage",
            mod_type=MOD_MORE,
            rounding_for_added=None,
            display_added_as_percentage=False,
            display_added_as_tenth_of_value=False,
            display_as_percentage_of=False,
            display_as_added_to=False,
            dont_display_plus=False,
            less_is_better=False,
            more_rounding=0,
        )
        self.assertEqual(compute_display(0.1234, info, MOD_MORE).value, 12)

    def test_unresolved_property_fallback(self) -> None:
        result = compute_display(0.123, None, MOD_ADDED)
        self.assertFalse(result.resolved)


class RangeSummaryTests(unittest.TestCase):
    def test_fire_resistance_body_armor_t7(self) -> None:
        summary = range_summary(0.61, 0.75, 0.5, 0.0, FIRE_RES_INFO, MOD_ADDED)
        self.assertEqual(summary["modifier"], 1.5)
        self.assertEqual(summary["effective_min_decimal"], "0.915")
        self.assertEqual(summary["effective_max_decimal"], "1.125")
        self.assertAlmostEqual(summary["effective_min"], 0.915, places=12)
        self.assertAlmostEqual(summary["effective_max"], 1.125, places=12)
        self.assertEqual(summary["tunklab_display_min"], 92)
        self.assertEqual(summary["tunklab_display_max"], 113)
        self.assertEqual(summary["display_min"], 92)
        self.assertEqual(summary["display_max"], 112)
        self.assertEqual(summary["display_unit"], "%")

    def test_fire_resistance_shield_t7(self) -> None:
        summary = range_summary(0.61, 0.75, 0.17, 0.0, FIRE_RES_INFO, MOD_ADDED)
        self.assertEqual(summary["modifier_decimal"], "1.17")
        self.assertEqual(summary["effective_min_decimal"], "0.7137")
        self.assertEqual(summary["effective_max_decimal"], "0.8775")
        self.assertEqual(summary["tunklab_display_min"], 71)
        self.assertEqual(summary["tunklab_display_max"], 88)

    def test_fire_resistance_helmet_t7_is_unscaled(self) -> None:
        summary = range_summary(0.61, 0.75, 0.0, 0.0, FIRE_RES_INFO, MOD_ADDED)
        self.assertEqual(summary["modifier"], 1.0)
        self.assertEqual(summary["display_min"], 61)
        self.assertEqual(summary["display_max"], 75)

    def test_hybrid_affix_extra_roll_slot(self) -> None:
        # Freeze Rate Multiplier and Cold Resistance T1 extra roll: 0.05 on amulet (x1.17)
        summary = range_summary(0.05, 0.05, 0.17, 0.0, FIRE_RES_INFO, MOD_ADDED)
        self.assertEqual(summary["effective_min_decimal"], "0.0585")
        self.assertEqual(summary["tunklab_display_min"], 6)

    def test_idol_altar_fractional_tags(self) -> None:
        self.assertIn(4, IDOL_ALTAR_FRACTIONAL_TAGS)
        self.assertNotIn(1, IDOL_ALTAR_FRACTIONAL_TAGS)


if __name__ == "__main__":
    unittest.main()
