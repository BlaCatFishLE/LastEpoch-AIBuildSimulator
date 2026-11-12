"""Tests for the single-cast and cooldown-loop event simulations."""

from __future__ import annotations

import unittest

import support

from le_calc import model
from le_calc.cooldown import compute_cast_time, compute_cooldown
from le_calc.ruleset import load_ruleset
from le_calc.simulate import run_loop, run_single_cast

REL = 1e-9


class CastAndCooldownTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = support.make_snapshot()
        self.ruleset = load_ruleset()

    def test_cast_time(self):
        cast = compute_cast_time(self.snapshot, self.ruleset)
        self.assertAlmostEqual(cast["increased_cast_speed"], 0.108, places=12)
        base_rate = 1.8333375 * 0.8
        self.assertAlmostEqual(cast["base_rate_per_s"], base_rate, places=9)
        self.assertAlmostEqual(cast["cast_time_s"], 1.0 / (base_rate * 1.108), places=12)

    def test_cooldown_formula(self):
        cd = compute_cooldown(self.snapshot, self.ruleset).to_dict()
        self.assertAlmostEqual(cd["base_recharge_s"], 13.99972, places=9)
        self.assertAlmostEqual(cd["flat_total_s"], 2.0, places=12)
        # ICR pool = gear (0.32+0.08+0.0675) + Recurrence 0.6
        self.assertAlmostEqual(cd["icr_total"], 1.0675, places=12)
        expected = (13.99972 + 2.0) / (1.0 + 1.0675)
        self.assertAlmostEqual(cd["effective_recharge_s"], expected, places=12)
        self.assertAlmostEqual(cd["max_charges"], 2.0, places=12)


class SingleCastTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = support.make_snapshot()
        self.ruleset = load_ruleset()
        self.single = run_single_cast(self.snapshot, self.ruleset)

    def test_totals_reconcile(self):
        totals = self.single["totals"]
        self.assertAlmostEqual(
            totals["single_cast_total"],
            totals["body_total"] + totals["ignite_total"],
            places=6,
        )
        self.assertAlmostEqual(
            totals["window_damage_until_black_hole_end"] + totals["ignite_tail_damage"],
            totals["single_cast_total"],
            places=6,
        )
        self.assertAlmostEqual(
            totals["window_damage_until_black_hole_end"],
            totals["body_total"] + totals["ignite_damage_before_black_hole_end"],
            places=6,
        )

    def test_cast_timing(self):
        cast = self.single["cast"]
        expected_cast = 1.0 / (1.8333375 * 0.8 * 1.108)
        self.assertAlmostEqual(cast["cast_time_s"], expected_cast, places=12)
        self.assertAlmostEqual(cast["black_hole_spawn_s"], expected_cast, places=12)
        self.assertAlmostEqual(cast["black_hole_end_s"], expected_cast + 7.15, places=12)
        self.assertAlmostEqual(self.single["totals"]["observation_end_s"], expected_cast + 13.025, places=9)

    def test_event_trace(self):
        events = self.single["event_trace"]
        kinds = {e["kind"] for e in events}
        for kind in ("cast_start", "black_hole_spawn", "body_tick", "ignite_roll", "ignite_tick", "black_hole_destroyed", "ignite_cohort_expiry", "observation_end"):
            self.assertIn(kind, kinds)
        body_events = [e for e in events if e["kind"] == "body_tick"]
        self.assertEqual(len(body_events), 48)  # 24 ticks x 2 components
        rolls = [e for e in events if e["kind"] == "ignite_roll"]
        self.assertEqual(len(rolls), 8)
        ignite_ticks = [e for e in events if e["kind"] == "ignite_tick"]
        self.assertGreater(len(ignite_ticks), 40)
        times = [e["t"] for e in events]
        self.assertEqual(times, sorted(times))
        # event table and top-level totals use the same attribution
        event_sum = sum(e["amount"] for e in events)
        self.assertAlmostEqual(
            event_sum,
            self.single["totals"]["single_cast_total"],
            delta=self.single["totals"]["single_cast_total"] * 1e-9,
        )
        # every damage event is traceable
        for e in body_events:
            self.assertTrue(e["expression_ref"])
            self.assertTrue(e["component_id"])
            self.assertGreater(e["amount"], 0.0)

    def test_time_series_cumulative(self):
        series = self.single["time_series"]
        self.assertGreater(len(series), 10)
        previous = -1.0
        for point in series:
            self.assertGreaterEqual(point["total_cum"], previous - 1e-6)
            previous = point["total_cum"]
            self.assertAlmostEqual(
                point["total_cum"], point["body_cum"] + point["ignite_cum"], places=6
            )
        total = self.single["totals"]["single_cast_total"]
        self.assertAlmostEqual(series[-1]["total_cum"], total, delta=total * 1e-9)

    def test_expected_stacks_reported(self):
        totals = self.single["totals"]
        self.assertAlmostEqual(totals["expected_total_stacks_applied"], 608.0, places=9)

    def test_zero_ignite_ends_observation_at_black_hole_end(self):
        snapshot = support.make_snapshot(fire_resistance=0.0)
        single = run_single_cast(snapshot, self.ruleset)
        self.assertEqual(single["totals"]["ignite_total"], 0.0)
        self.assertEqual(
            [e for e in single["event_trace"] if e["kind"] == "ignite_tick"], []
        )
        self.assertAlmostEqual(
            single["totals"]["observation_end_s"],
            single["cast"]["black_hole_end_s"],
            places=9,
        )


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = support.make_snapshot()
        self.ruleset = load_ruleset()
        self.policies = {p["policy_id"]: p for p in self.ruleset.action_policies}
        self.loop_eager = run_loop(self.snapshot, self.ruleset, self.ruleset.scenario, self.policies["eager"])
        self.loop_patient = run_loop(self.snapshot, self.ruleset, self.ruleset.scenario, self.policies["patient"])
        self.cast_time = compute_cast_time(self.snapshot, self.ruleset)["cast_time_s"]
        self.recharge = compute_cooldown(self.snapshot, self.ruleset).to_dict()["effective_recharge_s"]

    def test_windows_monotone_and_dps(self):
        windows = self.loop_eager["windows"]
        self.assertEqual([w["window_s"] for w in windows], [5.0, 30.0, 120.0])
        for previous, current in zip(windows, windows[1:]):
            self.assertLessEqual(previous["total_damage"], current["total_damage"])
        for window in windows:
            self.assertAlmostEqual(window["dps"], window["total_damage"] / window["window_s"], places=6)
            self.assertAlmostEqual(
                window["total_damage"],
                window["body_damage"] + window["ignite_damage"],
                places=6,
            )
            self.assertGreaterEqual(window["unsettled_tail_damage"], 0.0)
        last = self.loop_eager["time_series"][-1]
        total = windows[-1]["total_damage"]
        self.assertAlmostEqual(last["total_cum"], total, delta=total * 1e-9)

    def test_loop_event_trace_is_limited_and_traceable(self):
        loop = self.loop_eager
        self.assertTrue(loop["event_trace_limited"])
        kinds = {e["kind"] for e in loop["event_trace"]}
        for kind in ("cast_start", "black_hole_destroyed", "body_tick", "ignite_tick"):
            self.assertIn(kind, kinds)
        detailed_casts = {e["cast_id"] for e in loop["event_trace"] if e["kind"] == "body_tick"}
        self.assertEqual(detailed_casts, {"cast-1", "cast-2"})

    def test_loop_early_window_matches_patient_single_cast_window(self):
        """A patient loop with only one cast so far must equal the single cast window."""
        single = run_single_cast(self.snapshot, self.ruleset)
        for window in self.loop_patient["windows"]:
            if window["cast_count"] > 1:
                continue
            sample = min(single["time_series"], key=lambda p: abs(p["t"] - window["window_s"]))
            self.assertAlmostEqual(sample["t"], window["window_s"], places=6)
            self.assertAlmostEqual(
                window["total_damage"],
                sample["total_cum"],
                delta=max(1.0, abs(sample["total_cum"])) * 1e-9,
            )

    def test_eager_allows_overlapping_black_holes(self):
        eager = self.loop_eager
        # synthetic snapshot: cast 0.615s, recharge 7.7387s -> 2 burst + 15 recharged casts
        self.assertEqual(eager["totals"]["cast_count"], 17)
        self.assertGreaterEqual(eager["totals"]["max_concurrent_black_holes"], 2)
        # first two casts use the two initial charges back-to-back
        casts = eager["casts"]
        self.assertAlmostEqual(casts[1]["t"] - casts[0]["t"], self.cast_time, places=6)
        # afterwards one cast per recharge
        for a, b in zip(casts[2:], casts[3:]):
            self.assertAlmostEqual(b["t"] - a["t"], self.recharge, places=6)

    def test_patient_waits_for_black_hole_expiry(self):
        patient = self.loop_patient
        self.assertEqual(patient["totals"]["cast_count"], 16)
        self.assertEqual(patient["totals"]["max_concurrent_black_holes"], 1)
        casts = patient["casts"]
        interval = 7.15 + self.cast_time
        for a, b in zip(casts, casts[1:]):
            self.assertAlmostEqual(b["t"] - a["t"], interval, places=6)

    def test_steady_state_cross_check(self):
        single_total = run_single_cast(self.snapshot, self.ruleset)["totals"]["single_cast_total"]
        self.assertAlmostEqual(
            self.loop_eager["totals"]["steady_state_dps_cross_check"],
            single_total / self.recharge,
            places=4,
        )
        self.assertAlmostEqual(
            self.loop_patient["totals"]["steady_state_dps_cross_check"],
            single_total / (7.15 + self.cast_time),
            places=4,
        )

    def test_eager_higher_than_patient(self):
        self.assertGreater(
            self.loop_eager["totals"]["horizon_damage"],
            self.loop_patient["totals"]["horizon_damage"],
        )

    def test_time_series_carries_resource_and_object_curves(self):
        series = self.loop_eager["time_series"]
        self.assertGreater(len(series), 100)
        self.assertTrue(all("ignite_stacks" in p and "charges" in p for p in series))
        self.assertGreaterEqual(max(p["active_black_holes"] for p in series), 2)
        self.assertTrue(all(0 <= p["charges"] <= 2 for p in series))
        self.assertGreater(len(self.loop_eager["resource"]["charge_events"]), 0)


if __name__ == "__main__":
    unittest.main()
