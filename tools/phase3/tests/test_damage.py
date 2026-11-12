"""Golden tests for the Black Hole body damage core and the Ignite model."""

from __future__ import annotations

import copy
import unittest

import support

from le_calc import ignite as ignite_mod
from le_calc import model
from le_calc.damage import compute_body, compute_duration, tags_match
from le_calc.ruleset import load_ruleset

REL = 1e-9


class TagMatchingTest(unittest.TestCase):
    def test_type_and_category_gating(self):
        from le_calc.damage import BODY_COLD_TAGS, BODY_FIRE_TAGS, IGNITE_TAGS

        # increased Elemental and DoT applies to both body components and ignite
        self.assertTrue(tags_match(["Elemental", "DoT"], BODY_FIRE_TAGS))
        self.assertTrue(tags_match(["Elemental", "DoT"], BODY_COLD_TAGS))
        self.assertTrue(tags_match(["Elemental", "DoT"], IGNITE_TAGS))
        # spell damage applies to the (spell) body but not to an ailment
        self.assertTrue(tags_match(["Spell"], BODY_FIRE_TAGS))
        self.assertFalse(tags_match(["Spell"], IGNITE_TAGS))
        # fire damage applies to fire body + ignite, never to cold body
        self.assertTrue(tags_match(["Fire"], BODY_FIRE_TAGS))
        self.assertFalse(tags_match(["Fire"], BODY_COLD_TAGS))
        self.assertTrue(tags_match(["Fire"], IGNITE_TAGS))
        # melee / minion / hit-only never apply
        self.assertFalse(tags_match(["Melee"], BODY_FIRE_TAGS))
        self.assertFalse(tags_match(["Minion"], IGNITE_TAGS))
        self.assertFalse(tags_match(["Spell", "Hit"], BODY_FIRE_TAGS))
        # untagged modifiers apply to everything
        self.assertTrue(tags_match([], BODY_COLD_TAGS))


class BodyDamageTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = support.make_snapshot()
        self.ruleset = load_ruleset()
        self.scenario = self.ruleset.scenario
        self.body = compute_body(self.snapshot, self.ruleset, self.scenario)

    def test_duration_is_additive(self):
        info = compute_duration(self.snapshot, self.ruleset)
        self.assertAlmostEqual(info["increased_total"], 1.6, places=12)
        self.assertAlmostEqual(info["duration_s"], 2.75 * 2.6, places=12)

    def test_two_components_from_binary_and_red_giant(self):
        self.assertEqual(len(self.body["components"]), 2)
        ids = {c["component_id"] for c in self.body["components"]}
        self.assertEqual(ids, {"bh-binary-fire", "bh-binary-cold"})
        types = {c["damage_type"] for c in self.body["components"]}
        self.assertEqual(types, {"Fire", "Cold"})

    def test_tick_schedule_merges_remainder_into_last_tick(self):
        self.assertEqual(self.body["tick_count"], 24)
        fire = self.body["components"][0]
        ticks = fire["ticks"]
        self.assertEqual(len(ticks), 24)
        self.assertAlmostEqual(ticks[-1]["coverage_fraction"], 0.25 / 0.3, places=12)
        self.assertAlmostEqual(ticks[0]["offset_s"], 0.0, places=12)
        # total damage equals nominal rate x duration exactly
        self.assertAlmostEqual(
            fire["damage_total"],
            fire["per_tick_damage"] * (7.15 / 0.3),
            places=6,
        )

    def test_tick_model_ceil_full_ticks_matches_1_5_3_re(self):
        ruleset = copy.deepcopy(self.ruleset)
        ruleset.data["black_hole"]["tick_model"]["remainder_policy"] = "ceil_full_ticks"
        body = compute_body(self.snapshot, ruleset, self.scenario)
        self.assertEqual(body["tick_count"], 24)
        self.assertEqual(body["tick_model"]["remainder_policy"], "ceil_full_ticks")
        fire = body["components"][0]
        self.assertEqual(len(fire["ticks"]), 24)
        self.assertAlmostEqual(fire["ticks"][-1]["coverage_fraction"], 1.0, places=12)
        self.assertAlmostEqual(
            fire["damage_total"],
            fire["per_tick_damage"] * 24,
            places=6,
        )
        # the integer-tick model is ~0.7% above nominal rate * duration
        nominal = fire["per_tick_damage"] * (7.15 / 0.3)
        self.assertAlmostEqual(fire["damage_total"] / nominal, 24 / (7.15 / 0.3), places=12)

    def test_fire_component_golden_numbers(self):
        fire = next(c for c in self.body["components"] if c["damage_type"] == "Fire")
        self.assertAlmostEqual(fire["base_value"], 48.0)
        self.assertAlmostEqual(fire["added_damage_total"], 65.0)
        self.assertAlmostEqual(fire["per_tick_base"], 204.0)
        self.assertAlmostEqual(fire["increased"]["total"], 13.63, places=12)
        self.assertAlmostEqual(fire["more_product"], 1.6 * 1.3 * 1.3 * 2.0 * 2.0 * 1.6, places=12)
        self.assertAlmostEqual(fire["target"]["penetration"], 0.26)
        self.assertAlmostEqual(fire["target"]["multiplier"], 1.26)
        expected_tick = 204.0 * 14.63 * (1.6 * 1.3 * 1.3 * 2.0 * 2.0 * 1.6) * 1.26
        self.assertAlmostEqual(fire["per_tick_damage"], expected_tick, delta=expected_tick * REL)
        expected_total = expected_tick * (7.15 / 0.3)
        self.assertAlmostEqual(fire["damage_total"], expected_total, delta=expected_total * REL)

    def test_cold_component_golden_numbers(self):
        cold = next(c for c in self.body["components"] if c["damage_type"] == "Cold")
        self.assertAlmostEqual(cold["increased"]["total"], 11.15, places=12)
        self.assertAlmostEqual(cold["more_product"], 1.6 * 1.3 * 2.0 * 2.0 * 1.6, places=12)
        self.assertAlmostEqual(cold["target"]["multiplier"], 1.0)
        expected_tick = 204.0 * 12.15 * (1.6 * 1.3 * 2.0 * 2.0 * 1.6)
        self.assertAlmostEqual(cold["per_tick_damage"], expected_tick, delta=expected_tick * REL)

    def test_expression_tree_is_traceable(self):
        fire = next(c for c in self.body["components"] if c["damage_type"] == "Fire")
        expr = fire["expression"]
        self.assertEqual(expr["kind"], "mul")
        kinds = [child["kind"] for child in expr["children"]]
        self.assertIn("add", kinds)
        self.assertIn("factor", kinds)
        # the inc node lists every applicable source row plus the INT attribute row
        inc_node = next(c for c in expr["children"] if c["node_id"].endswith(":inc"))
        ids = [row["contribution_id"] for row in inc_node["rows"]]
        self.assertIn("test:inc-fire", ids)
        self.assertIn("test:inc-elem-dot", ids)
        self.assertIn("ruleset:attribute:intelligence", ids)
        self.assertIn("test:inc-spell", ids)  # body component is a Spell
        self.assertNotIn("test:ailment-effect", ids)  # ailment effect is ignite-only

    def test_no_binary_structural_single_cold_component(self):
        ruleset = support.make_ruleset_without_structural()
        body = compute_body(self.snapshot, ruleset, self.scenario)
        self.assertEqual(len(body["components"]), 1)
        self.assertEqual(body["components"][0]["damage_type"], "Cold")
        # tree more groups still apply (tidal/umbral/supermassive/singularity/weakened)
        self.assertAlmostEqual(
            body["components"][0]["more_product"], 1.6 * 1.3 * 2.0 * 2.0 * 1.6, places=12
        )


class IgniteTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = support.make_snapshot()
        self.ruleset = load_ruleset()
        self.scenario = self.ruleset.scenario
        self.body = compute_body(self.snapshot, self.ruleset, self.scenario)
        self.ignite = ignite_mod.compute_ignite(
            self.snapshot,
            self.ruleset,
            spawn_time_s=0.0,
            black_hole_duration_s=self.body["duration"]["duration_s"],
        )

    def test_single_stack_core(self):
        core = ignite_mod.compute_ignite_core(self.snapshot, self.ruleset)
        expected_more = 1.6 * 1.3 * 1.3 * 2.0 * 2.0  # tidal/flaming/umbral/supermassive/singularity
        self.assertAlmostEqual(core["duration_multiplier"], 2.41, places=12)
        # inc pool: fire/elemental/dot rows + Intelligence(0.04*5); ailment effect is NOT in inc
        self.assertAlmostEqual(core["increased_total"], 11.63, places=12)
        self.assertAlmostEqual(core["more_product"], expected_more, places=12)
        # fire pen 0.26 + ailment effect 0.35 treated as extra fire penetration
        self.assertAlmostEqual(core["penetration"]["total"], 0.61, places=12)
        self.assertAlmostEqual(core["target"]["multiplier"], 1.61, places=12)
        expected_stack = 40.0 * 2.41 * 12.63 * expected_more * 1.61
        self.assertAlmostEqual(core["per_stack_total"], expected_stack, delta=expected_stack * REL)

    def test_weakened_reality_not_inherited(self):
        groups = {g["group"] for g in self.ignite["more_groups"]}
        self.assertNotIn("node:bh2:27", groups)
        self.assertNotIn("node:bh2:9", groups)
        self.assertIn("node:bh2:2", groups)
        self.assertIn("node:bh2:7", groups)

    def test_heat_wave_application(self):
        app = self.ignite["application"]
        self.assertEqual(app["points"], 5)
        self.assertEqual(app["roll_count"], 8)
        self.assertAlmostEqual(app["expected_stacks_per_roll"], 0.02 * 5 * 760.0, places=12)
        self.assertAlmostEqual(app["expected_stacks_applied"], 8 * 76.0, places=12)

    def test_totals_and_tail(self):
        totals = self.ignite["totals"]
        expected_total = 8 * 76.0 * (40.0 * 2.41 * 12.63 * 1.6 * 1.3 * 1.3 * 2.0 * 2.0 * 1.61)
        self.assertAlmostEqual(totals["damage"], expected_total, delta=expected_total * REL)
        self.assertGreater(totals["tail_damage"], 0.0)
        self.assertAlmostEqual(totals["damage"], totals["damage_before_black_hole_end"] + totals["tail_damage"], delta=expected_total * REL)
        # last roll at offset 7, expiry at 7 + 6.025
        self.assertAlmostEqual(totals["last_ailment_expiry_s"], 13.025, places=9)

    def test_zero_fire_resistance_means_no_ignite(self):
        snapshot = support.make_snapshot(fire_resistance=0.0)
        ignite = ignite_mod.compute_ignite(snapshot, self.ruleset, 0.0, 7.15)
        self.assertEqual(ignite["totals"]["damage"], 0.0)
        self.assertAlmostEqual(ignite["application"]["expected_stacks_per_roll"], 0.0)

    def test_heat_wave_half_second_rolls_keep_same_stacks_per_second(self):
        ruleset = copy.deepcopy(self.ruleset)
        ruleset.data["ignite"]["heat_wave"]["rolls_per_second"] = 2
        ignite = ignite_mod.compute_ignite(
            self.snapshot,
            ruleset,
            spawn_time_s=0.0,
            black_hole_duration_s=self.body["duration"]["duration_s"],
        )
        app = ignite["application"]
        self.assertEqual(app["roll_count"], 15)  # offsets 0, 0.5, ..., 7.0
        self.assertAlmostEqual(app["expected_stacks_per_second"], 0.02 * 5 * 760.0, places=12)
        self.assertAlmostEqual(app["expected_stacks_per_roll"], 0.5 * 76.0, places=12)
        self.assertAlmostEqual(app["expected_stacks_applied"], 15 * 38.0, places=12)
        self.assertAlmostEqual(app["chance_per_second_percent"], 7600.0, places=9)


if __name__ == "__main__":
    unittest.main()
