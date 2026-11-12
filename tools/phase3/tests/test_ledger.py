"""Tests for the source ledger builder (``le_calc.ledger``).

The ledger is the auditable "属性来源账" artifact: one flat row per
contribution with source identity, player label, operation/scope, condition,
inclusion decision, exclusion reason, value status and evidence references.
"""

from __future__ import annotations

import json
import unittest

from le_calc import model
from le_calc.inputs import load_bundle

from _dataset_asset import require_dataset_db
from le_calc.ledger import (
    SCHEMA_VERSION,
    build_source_ledger,
    contribution_row,
    ledger_document,
    ledger_json,
    summarize,
)
from le_calc.stats import build_snapshot

REQUIRED_ROW_KEYS = {
    "row_id",
    "contribution_id",
    "source_id",
    "source_kind",
    "source_path",
    "source_location",
    "source_label",
    "player_label",
    "stat_name",
    "stat_key",
    "target",
    "operation",
    "raw_value",
    "normalized_value",
    "unit",
    "damage_tags",
    "condition_code",
    "condition_status",
    "group",
    "included",
    "active",
    "exclusion_reason",
    "value_source",
    "status",
    "evidence",
    "evidence_refs",
    "notes",
}



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

class SourceLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_dataset_db()
        cls.bundle = load_bundle(verify_database_file=False)
        cls.snapshot = build_snapshot(cls.bundle)
        cls.document = ledger_document(cls.snapshot)
        cls.rows = cls.document["rows"]

    # -- row identity / coverage -------------------------------------------
    def test_one_row_per_contribution(self) -> None:
        self.assertEqual(len(self.rows), len(self.snapshot.contributions))
        self.assertEqual(len({row["row_id"] for row in self.rows}), len(self.rows))
        self.assertEqual([row["row_id"] for row in self.rows[:3]], ["sl-0001", "sl-0002", "sl-0003"])
        self.assertEqual(
            [row["row_id"] for row in self.rows[-2:]], ["sl-0240", "sl-0241"]
        )

    def test_rows_are_sorted_deterministically(self) -> None:
        expected = [
            contribution.contribution_id
            for contribution in sorted(
                self.snapshot.contributions, key=model.contribution_sort_key
            )
        ]
        self.assertEqual([row["contribution_id"] for row in self.rows], expected)
        rebuilt_snapshot = build_snapshot(load_bundle(verify_database_file=False))
        rebuilt = build_source_ledger(rebuilt_snapshot)
        self.assertEqual([row["contribution_id"] for row in rebuilt], expected)
        self.assertEqual(
            ledger_json(ledger_document(self.snapshot)),
            ledger_json(ledger_document(rebuilt_snapshot)),
        )

    def test_every_mod_is_present_with_source_identity(self) -> None:
        mod_rows = [row for row in self.rows if row["contribution_id"].startswith("mod:")]
        self.assertEqual(len(mod_rows), 150)
        covered = {row["contribution_id"][4:].split(":")[0] for row in mod_rows}
        self.assertEqual(covered, {mod["mod_id"] for mod in self.bundle.mods})
        for row in mod_rows:
            self.assertTrue(row["source_path"].startswith("data."))
            self.assertTrue(row["source_location"])
            self.assertTrue(row["source_label"])

    def test_rows_have_required_display_fields(self) -> None:
        for row in self.rows:
            missing = REQUIRED_ROW_KEYS - set(row)
            self.assertEqual(missing, set(), f"{row['contribution_id']} missing {missing}")
            self.assertIn(row["operation"], ("added", "increased", "more", "base", "flag"))
            self.assertIsInstance(row["included"], bool)
            self.assertIsInstance(row["active"], bool)
            self.assertGreaterEqual(len(row["evidence"]), 1)
            self.assertGreaterEqual(len(row["evidence_refs"]), 1)
            self.assertIn("—", row["player_label"])

    def test_evidence_locator_contains_mod_id_for_mods(self) -> None:
        mods_by_id = {mod["mod_id"]: mod for mod in self.bundle.mods}
        for row in self.rows:
            if not row["contribution_id"].startswith("mod:"):
                continue
            mod_id = row["contribution_id"][4:].split(":")[0]
            evidence_text = " ".join(
                f"{item.get('detail', '')} {item.get('locator', '')}" for item in row["evidence"]
            )
            self.assertIn(mods_by_id[mod_id]["mod_id"], evidence_text)

    # -- row content --------------------------------------------------------
    def test_row_exposes_operation_and_scope(self) -> None:
        by_id = {row["contribution_id"]: row for row in self.rows}
        fire_pen = by_id["mod:equipment.weapon1#affix#normal#0.0"]
        self.assertEqual(fire_pen["stat_key"], model.STAT_PENETRATION)
        self.assertEqual(fire_pen["operation"], "added")
        self.assertEqual(fire_pen["unit"], "percent")
        self.assertEqual(fire_pen["damage_tags"], ["Fire"])
        self.assertAlmostEqual(fire_pen["normalized_value"], 0.22)
        self.assertEqual(fire_pen["raw_value"], "22%")

        mana = by_id["mod:equipment.waist#implicit#0"]
        self.assertEqual(mana["stat_key"], model.STAT_MANA)
        self.assertEqual(mana["operation"], "added")
        self.assertEqual(mana["unit"], "flat")
        self.assertEqual(mana["normalized_value"], 32.0)

        increased_mana = by_id["mod:equipment.chest#implicit#1"]
        self.assertEqual(increased_mana["operation"], "increased")
        self.assertEqual(increased_mana["unit"], "percent")
        self.assertAlmostEqual(increased_mana["normalized_value"], 0.11)

        duration = by_id["mod:equipment.head#unique_mod#4"]
        self.assertEqual(duration["stat_key"], model.STAT_AILMENT_DURATION)
        self.assertEqual(duration["target"], "ignite")
        self.assertEqual(duration["condition_status"], "always")

    def test_excluded_rows_are_visible_with_reasons(self) -> None:
        excluded = [row for row in self.rows if not row["included"]]
        self.assertGreater(len(excluded), 100)
        for row in excluded:
            self.assertTrue(row["exclusion_reason"])
        minion_pen = next(
            row for row in self.rows if row["contribution_id"] == "mod:equipment.weapon1#affix#normal#0.1"
        )
        self.assertFalse(minion_pen["included"])
        self.assertIn("minion", minion_pen["exclusion_reason"])

    def test_conditional_rows_keep_condition_evidence(self) -> None:
        by_id = {row["contribution_id"]: row for row in self.rows}
        glyph = by_id["mod:equipment.head#unique_mod#2"]
        self.assertEqual(glyph["condition_code"], "standing_on_glyph_of_dominion")
        self.assertEqual(glyph["condition_status"], "not_satisfied")
        self.assertFalse(glyph["active"])
        self.assertTrue(any("Glyph of Dominion" in (e.get("detail") or "") for e in glyph["evidence"]))

        crescendo = by_id["mod:equipment.ring2#affix#sealed#0.0"]
        self.assertEqual(crescendo["condition_code"], "crescendoing_rage_third_cast")
        self.assertEqual(crescendo["condition_status"], "unknown")
        self.assertEqual(crescendo["operation"], "more")
        self.assertAlmostEqual(crescendo["normalized_value"], 0.5)

        idol_altar = by_id["mod:equipment.idol_altar#implicit#0"]
        self.assertEqual(idol_altar["status"], "unresolved")
        self.assertFalse(idol_altar["included"])
        self.assertIn("idol grid", idol_altar["exclusion_reason"])

    def test_derived_rows_mark_their_process(self) -> None:
        by_id = {row["contribution_id"]: row for row in self.rows}
        cdr = by_id["mod:equipment.hands#affix#sealed#0.0"]
        self.assertEqual(cdr["status"], "derived")
        self.assertAlmostEqual(cdr["normalized_value"], 0.1075)
        self.assertIn("1% × min(200", cdr["notes"])
        cast = by_id["mod:equipment.hands#affix#primordial#0.0"]
        self.assertEqual(cast["status"], "derived")
        self.assertAlmostEqual(cast["normalized_value"], 0.172)
        mana_attr = by_id["derived:attribute:attunement-mana"]
        self.assertEqual(mana_attr["status"], "derived")
        self.assertEqual(mana_attr["source_kind"], "derived")

    def test_skill_node_rows_are_traceable(self) -> None:
        skill_rows = [row for row in self.rows if row["source_kind"] == "skill_node"]
        self.assertEqual(len(skill_rows), 30)
        tidal = next(row for row in skill_rows if row["contribution_id"] == "skill:bh2:2:more")
        self.assertEqual(tidal["group"], "node:bh2:2")
        self.assertAlmostEqual(tidal["normalized_value"], 0.6)
        self.assertTrue(tidal["evidence_refs"])
        self.assertIn("bh2 node 2", tidal["evidence_refs"][0] + " " + json.dumps(tidal["evidence"], ensure_ascii=False))

    # -- summary ------------------------------------------------------------
    def test_summary_matches_snapshot(self) -> None:
        summary = self.document["summary"]
        self.assertEqual(summary["total_rows"], len(self.snapshot.contributions))
        self.assertEqual(summary["active_rows"], len(self.snapshot.active_contributions()))
        self.assertEqual(summary["included_rows"], 120)
        self.assertEqual(summary["excluded_rows"], 121)
        self.assertEqual(summary["unresolved_rows"], 2)
        self.assertEqual(
            summary["by_source_kind"],
            {
                "base_character": 6,
                "blessing": 6,
                "derived": 7,
                "equipment": 97,
                "idol": 39,
                "idol_altar": 8,
                "passive_node": 48,
                "skill_node": 30,
            },
        )
        self.assertEqual(
            sum(summary["exclusion_reason_histogram"].values()),
            summary["excluded_rows"],
        )
        self.assertEqual(summary["by_status"]["confirmed"] + summary["by_status"]["derived"] + summary["by_status"]["unresolved"], summary["total_rows"])
        self.assertEqual(
            summary["by_condition"]["target_is_boss_or_rare=satisfied"], 1
        )
        self.assertEqual(
            summary["by_condition"]["target_within_center_1m=satisfied"], 1
        )
        self.assertEqual(
            summary["by_condition"]["max_mana_gte_300=not_satisfied"], 2
        )

    def test_summary_function_is_pure(self) -> None:
        first = summarize(self.rows)
        second = summarize(self.rows)
        self.assertEqual(first, second)
        self.assertEqual(sum(first["exclusion_reason_histogram"].values()), first["excluded_rows"])

    # -- document / determinism --------------------------------------------
    def test_document_shape_and_fingerprints(self) -> None:
        self.assertEqual(self.document["schema"], SCHEMA_VERSION)
        self.assertEqual(self.document["generated_by"], "le_calc.ledger")
        self.assertEqual(self.document["build_meta"], self.snapshot.build_meta)
        meta = self.document["build_meta"]
        self.assertIn("calc_input_sha256", meta)
        self.assertIn("database_sha256", meta)
        self.assertIn("revision_selection", meta)
        self.assertEqual(meta["build"]["revision_id"], "rev-c746411ccebee9c0")
        self.assertEqual(meta["build"]["revision_seq"], 8)
        self.assertFalse(meta["revision_selection"]["current_pointer_drift"])

    def test_ledger_json_is_sorted_and_roundtrips(self) -> None:
        text = ledger_json(self.document)
        self.assertTrue(text.endswith("\n"))
        payload = json.loads(text)
        self.assertEqual(payload["schema"], SCHEMA_VERSION)
        self.assertEqual(len(payload["rows"]), len(self.rows))
        keys = list(payload["rows"][0].keys())
        self.assertEqual(keys, sorted(keys))
        self.assertNotIn("retrieved_at", text)

    def test_contribution_row_helper_marks_active(self) -> None:
        contribution = self.snapshot.contributions[0]
        row = contribution_row(contribution, 1)
        self.assertEqual(row["row_id"], "sl-0001")
        self.assertEqual(row["source_id"], contribution.contribution_id)
        self.assertEqual(row["normalized_value"], contribution.value)
        self.assertEqual(row["active"], contribution.active)


if __name__ == "__main__":
    unittest.main()
