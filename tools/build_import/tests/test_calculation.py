"""Tests for the calculator input export layer (``calculation-input-v1``).

Contract: ``docs/phase-2-local-input-and-calc-export.zh-CN.md`` §4/§5.  The fixed
regression sample is the *published* BuildSpec revision under
``data/builds/letools/o3ZRm9my``: the delivered CURRENT revision (user-synced, 17
applied local values, dynamic ``current.json`` pointer), the source-only BASELINE
revision pinned by id, and the historical ``rev-8dc664d73737a8b9`` revision for the
degradation path (no readable layer, no scope, legacy unique data).

2026-11-04 re-review R1: effects whose *source property identity* is a non-DPS
effect (``NON_DPS_PROPERTIES = {104}``, coreDB.propertyList 104 = Increased Drop
Rate) are excluded from this DPS input, even though the BuildSpec keeps them.  The
fixed sample loses exactly 5 drop-rate blessing implicits (153 -> 148 flat entries,
142 -> 137 kind=mod entries).

These tests are independent of the importer arithmetic: expected numbers come from
the contract, the fixed-sample facts recorded by the phase-2 acceptance tests
(29 items / 47 affix instances / 15 target groups / 17 displayed variable rows /
12 hidden unique mods) and from reading the published JSON structure itself — the
export module only re-groups that data and applies the R1 identity filter.

Run from ``tools/build_import``::

    $env:PYTHONPATH='src'; & $py -m unittest tests.test_calculation -v
"""

from __future__ import annotations

import hashlib
import json
import shlex
import unittest
from pathlib import Path

from le_build.calculation import (
    AFFIX_CHANNELS,
    CALCULATION_SCHEMA_VERSION,
    VALUE_SOURCES,
    VALUE_STATUSES,
    build_calculation_input,
    iter_mods,
    unresolved_inputs,
)
from le_build.validate import _validate_schema_node

REPO = Path(__file__).resolve().parents[3]
BUILD_DIR = REPO / "data" / "builds" / "letools" / "o3ZRm9my"
SCHEMA_PATH = REPO / "tools" / "build_import" / "schemas" / "calculation-input-v1.json"
ARTIFACT = REPO / "reports" / "phase-2" / "o3ZRm9my" / "calculation-input.json"
LEGACY_REVISION_ID = "rev-8dc664d73737a8b9"
# 2026-11-04 re-import: the delivered CURRENT revision is the user-synced local-input
# revision (seq 6, 17 applied explicit values); source-only expectations are pinned to
# the seq-5 BASELINE.  The old b0f2/5e0e revisions are historical read-only.
BASELINE_REVISION = "rev-dbcf35ba60b0dd95"
BASELINE_CONTENT_SHA256 = (
    "dbcf35ba60b0dd956d4f08b0faf26b91e888f3762c1e67ec210bd2c89b17d354"
)
CURRENT_REVISION = "rev-c746411ccebee9c0"
CURRENT_CONTENT_SHA256 = (
    "c746411ccebee9c04b67da4a40cfc21d1e6770530e284bd2de8160ed0199932b"
)
# Regenerated from the CURRENT revision (dynamic current.json) after the R1 filter.
CURRENT_ARTIFACT_SIZE = 524731
CURRENT_ARTIFACT_SHA256 = (
    "531f7a50ec338cf0765bf0008a05098f044e38f5e134420241f149ef4f9aa561"
)
CURRENT_LOCAL_OVERRIDE_MODS = 17
FIXED_GENERATED_AT = "2026-11-03T23:30:00+08:00"

EXPECTED_ITEM_ROLES = {"equipment": 11, "idol": 8, "blessing": 10}
EXPECTED_ITEM_COUNT = 29
# 2026-11-04 R1 scope: 5 drop-rate implicit mods are excluded from the DPS input.
EXPECTED_IMPLICIT_MODS = 28
EXPECTED_AFFIX_MODS = 61
EXPECTED_AFFIX_VALUES_PER_CHANNEL = {
    "normal": 48,
    "sealed": 2,
    "primordial": 3,
    "corrupted": 8,
}
EXPECTED_UNIQUE_MODS = 48
EXPECTED_HIDDEN_UNIQUE_MODS = 12
EXPECTED_NON_ROLLABLE_UNIQUE_MODS = 7
EXPECTED_DESCRIPTIONS = 11
EXPECTED_MODS_TOTAL = 148
EXPECTED_MOD_KIND_TOTAL = 137
EXPECTED_EXCLUDED_NON_DPS = 5
EXPECTED_NON_DPS_PROPERTIES = [104]
EXPECTED_NON_DPS_MOD_IDS = {
    "blessings.1#implicit#0",
    "blessings.2#implicit#0",
    "blessings.4#implicit#0",
    "blessings.6#implicit#0",
    "blessings.8#implicit#0",
}
EXPECTED_NON_DPS_ITEM_IDS = {
    "blessings.1",
    "blessings.2",
    "blessings.4",
    "blessings.6",
    "blessings.8",
}
# Combat-relevant blessings must survive the identity filter (over-filter guard).
KEPT_BLESSING_IDS = {
    "blessings.3",
    "blessings.5",
    "blessings.7",
    "blessings.9",
    "blessings.10",
}
# 2026-11-03 per-variable input fix: 15 target groups / 17 displayed variable rows
# on the source-only BASELINE revision (the delivered current revision has 0 missing).
EXPECTED_UNRESOLVED = 15
EXPECTED_VARIABLE_ROWS = 17
# Missing source values target: 4 relic affixes (hands sealed/primordial are
# constant-1% affixes with no missing variable and are no longer input targets).
EXPECTED_MISSING_AFFIX_PATHS = {
    "data.equipment.relic.affixes.1",
    "data.equipment.relic.affixes.2",
    "data.equipment.relic.affixes.3",
    "data.equipment.relic.primordialAffix",
}
OMNIS_ROLL_IDS = set(range(7))
UNSATED_ROLL_IDS = set(range(4))
# Multi-variable roll slots in the fixed sample (one rollId -> several displayed rows).
EXPECTED_MULTI_VARIABLE_ROLLS = {
    ("data.equipment.amulet", 0): [("flat", "1–2"), ("percent", "1%–45%")],
    ("data.equipment.ring2", 3): [("flat", "4–8"), ("flat", "4–8")],
}
# Affix value index of the only missing variable on the relic primordial affix
# (value 0 is the resolvable constant +6 Level).
EXPECTED_RELIC_PRIMORDIAL_VALUE_INDEX = 1
LEGACY_MOD_COUNTS = {"implicit": 22, "affix": 61, "unique_mod": 35}

MOD_REQUIRED_KEYS = {
    "mod_id",
    "item_instance_id",
    "source",
    "channel",
    "kind",
    "stat",
    "property",
    "tags",
    "special_tag",
    "modifier_type",
    "value_numeric",
    "display_value",
    "value_status",
    "value_source",
    "roll",
    "range",
    "hidden",
    "unique",
    "affix",
    "readable_text",
    "provenance",
}
VALID_SOURCES = {"affix", "implicit", "unique_mod"}
VALID_CHANNELS = {"unique", "implicit", "normal", "sealed", "primordial", "corrupted"}
VALID_KINDS = {"mod", "description"}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def load_pointer() -> dict:
    return json.loads((BUILD_DIR / "current.json").read_text(encoding="utf-8"))


def load_revision_spec(revision_id: str) -> dict:
    return json.loads(
        (BUILD_DIR / "revisions" / revision_id / "build.json").read_text(encoding="utf-8")
    )


def load_current_spec() -> dict:
    return load_revision_spec(load_pointer()["revision_id"])


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def schema_errors(document) -> list[str]:
    schema = load_schema()
    errors: list[str] = []
    _validate_schema_node(document, schema, schema, "$", errors)
    return errors


def dumps(document) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True)


def find_mod(document: dict, predicate):
    for mod in document["mods"]:
        if predicate(mod):
            return mod
    return None


def mods_by_source(document: dict, source: str, kind: str = "mod") -> list[dict]:
    return [
        mod
        for mod in document["mods"]
        if mod["source"] == source and mod["kind"] == kind
    ]


def item_instance(document: dict, instance_id: str) -> dict:
    for item in document["items"]:
        if item["instance_id"] == instance_id:
            return item
    raise AssertionError(f"item {instance_id!r} not found in export")


# ---------------------------------------------------------------------------
# Main fixed-sample tests: source-only BASELINE revision + delivered CURRENT revision
# ---------------------------------------------------------------------------
class CalculationExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pointer = load_pointer()
        # Source-only expectations are pinned; the current pointer now has 17 inputs.
        cls.spec = load_revision_spec(BASELINE_REVISION)
        cls.doc = build_calculation_input(cls.spec)
        cls.current_spec = load_current_spec()
        cls.current_doc = build_calculation_input(cls.current_spec)

    # -- document shape -----------------------------------------------------
    def test_schema_version_and_top_level_fields(self) -> None:
        doc = self.doc
        self.assertEqual(CALCULATION_SCHEMA_VERSION, "calculation-input-v1")
        self.assertEqual(doc["schema_version"], "calculation-input-v1")
        for key in (
            "generated_at",
            "build_id",
            "revision_id",
            "source",
            "game_version",
            "dataset_id",
            "scope",
            "character",
            "skills",
            "passive_tree",
            "items",
            "mods",
            "excluded_non_dps",
            "excluded_non_dps_properties",
            "excluded_non_dps_mod_ids",
            "unresolved_inputs",
            "notes",
        ):
            self.assertIn(key, doc, f"missing top-level key {key!r}")
        self.assertEqual(doc["build_id"], "letools:o3ZRm9my")
        self.assertEqual(doc["revision_id"], BASELINE_REVISION)
        self.assertEqual(doc["source"], "letools")
        self.assertEqual(doc["game_version"], "1.5.2")
        self.assertEqual(doc["dataset_id"], "phase1-dataset-002")
        self.assertEqual(doc["scope"]["scope_version"], "letools-build-scope-v2")
        self.assertIn("weaver_tree", {feature.get("feature") for feature in doc["scope"]["excluded_features"]})
        # 2026-11-04 re-review R1: non-DPS effects are excluded and declared.
        self.assertEqual(doc["excluded_non_dps"], EXPECTED_EXCLUDED_NON_DPS)
        self.assertEqual(doc["excluded_non_dps_properties"], EXPECTED_NON_DPS_PROPERTIES)
        self.assertEqual(set(doc["excluded_non_dps_mod_ids"]), EXPECTED_NON_DPS_MOD_IDS)

    def test_character_skills_and_passive_tree(self) -> None:
        doc = self.doc
        self.assertEqual(doc["character"]["level"], 100)
        self.assertEqual(doc["character"]["class"]["name"], "Mage")
        self.assertEqual(doc["character"]["mastery"]["name"], "Sorcerer")
        self.assertEqual(len(doc["skills"]["specializations"]), 5)
        self.assertEqual(len(doc["skills"]["action_bar"]), 5)
        self.assertEqual(doc["passive_tree"]["points_total"], 113)
        self.assertEqual(len(doc["passive_tree"]["nodes"]), 19)
        names = {skill["name"] for skill in doc["skills"]["action_bar"]}
        self.assertIn("Meteor", names)
        self.assertIn("Teleport", names)

    def test_items_wrapper_shape_and_order(self) -> None:
        items = self.doc["items"]
        self.assertEqual(len(items), EXPECTED_ITEM_COUNT)
        roles = {}
        for item in items:
            self.assertTrue(item["instance_id"])
            self.assertTrue(item["display_name"])
            self.assertIsInstance(item["mods"], list)
            self.assertIn("_item", item)
            self.assertEqual(item["_item"]["instance_id"], item["instance_id"])
            roles[item["role"]] = roles.get(item["role"], 0) + 1
        self.assertEqual(roles, EXPECTED_ITEM_ROLES)
        flat = [mod for item in items for mod in item["mods"]]
        self.assertEqual([mod["mod_id"] for mod in flat], [mod["mod_id"] for mod in self.doc["mods"]])
        # An item whose only effects were non-DPS keeps its identity/traceability
        # entry with an empty mods list (2026-11-04 R1 scope).
        empty_items = {item["instance_id"] for item in items if not item["mods"]}
        self.assertEqual(empty_items, EXPECTED_NON_DPS_ITEM_IDS)

    def test_non_dps_property_104_is_excluded_from_dps_input(self) -> None:
        doc = self.doc
        # Never filtered by display text: nothing with the non-DPS source property is
        # in the DPS input, and no drop-rate readable line slipped into an exported mod.
        for mod in doc["mods"]:
            self.assertNotEqual(mod["property"], 104, mod["mod_id"])
            self.assertNotIn("Increases Drop Rate", mod["readable_text"] or "")
            self.assertNotIn("Increased Drop Rate", mod["readable_text"] or "")
        for item in doc["items"]:
            for mod in item["mods"]:
                self.assertNotEqual(mod["property"], 104, mod["mod_id"])
        self.assertEqual(doc["excluded_non_dps"], EXPECTED_EXCLUDED_NON_DPS)
        self.assertEqual(doc["excluded_non_dps_properties"], EXPECTED_NON_DPS_PROPERTIES)
        self.assertEqual(set(doc["excluded_non_dps_mod_ids"]), EXPECTED_NON_DPS_MOD_IDS)
        self.assertFalse(EXPECTED_NON_DPS_MOD_IDS & {mod["mod_id"] for mod in doc["mods"]})
        # The BuildSpec/source is *not* modified: the 5 drop-rate implicit values
        # (property identity 104) and their readable non_dps lines are still there.
        raw_matches = 0
        readable_matches = 0
        for _path, instance in _iter_spec_instances(self.spec):
            for result in (instance.get("implicit_rolls") or {}).get("values") or []:
                for value in (result or {}).get("values") or []:
                    if (value or {}).get("property") == 104:
                        raw_matches += 1
            for line in (instance.get("readable") or {}).get("lines") or []:
                if (line.get("diagnostics") or {}).get("non_dps"):
                    readable_matches += 1
        self.assertEqual(raw_matches, EXPECTED_EXCLUDED_NON_DPS)
        self.assertEqual(readable_matches, EXPECTED_EXCLUDED_NON_DPS)
        # Over-filter guard: combat-relevant blessings keep their (non-drop-rate) mods.
        by_id = {item["instance_id"]: item for item in doc["items"]}
        for instance_id in KEPT_BLESSING_IDS:
            self.assertTrue(by_id[instance_id]["mods"], instance_id)
            self.assertTrue(
                all(mod["property"] != 104 for mod in by_id[instance_id]["mods"])
            )
        self.assertEqual(
            [mod["readable_text"] for mod in by_id["blessings.3"]["mods"]],
            ["18 Ward Gain on Kill", "+199 Ward Decay Threshold"],
        )
        # R1 exclusion is explained by a note (source archive kept, nothing deleted).
        joined_notes = " ".join(doc["notes"])
        self.assertIn("excluded_non_dps", joined_notes)
        self.assertIn("原始来源归档", joined_notes)

    def test_2026_11_04_semantic_fixes_flow_into_readable_text(self) -> None:
        # R2: hideModifierValue - the numeric value is retained structurally, but the
        # player-readable line no longer prepends the +1.
        sealed = find_mod(
            self.doc,
            lambda mod: mod["mod_id"] == "equipment.ring2#affix#sealed#0.0",
        )
        self.assertIsNotNone(sealed)
        self.assertEqual(sealed["readable_text"], "You have Crescendoing Rage")
        self.assertEqual(sealed["value_numeric"], 1.0)
        self.assertEqual(sealed["display_value"], "1")
        # R1: Ward Gain on Kill carries its trigger event in the readable text.
        ward = find_mod(
            self.doc, lambda mod: mod["mod_id"] == "blessings.3#implicit#0"
        )
        self.assertIsNotNone(ward)
        self.assertEqual(ward["readable_text"], "18 Ward Gain on Kill")
        self.assertEqual(ward["value_numeric"], 18)

    def test_flat_mod_counts(self) -> None:
        mods = self.doc["mods"]
        self.assertEqual(len(mods), EXPECTED_MODS_TOTAL)
        self.assertEqual(len(mods_by_source(self.doc, "implicit")), EXPECTED_IMPLICIT_MODS)
        self.assertEqual(len(mods_by_source(self.doc, "affix")), EXPECTED_AFFIX_MODS)
        self.assertEqual(len(mods_by_source(self.doc, "unique_mod")), EXPECTED_UNIQUE_MODS)
        descriptions = [mod for mod in mods if mod["kind"] == "description"]
        self.assertEqual(len(descriptions), EXPECTED_DESCRIPTIONS)
        self.assertEqual(len([mod for mod in mods if mod["kind"] == "mod"]), EXPECTED_MOD_KIND_TOTAL)
        self.assertEqual(
            len([mod for mod in mods if mod["kind"] == "mod"]) + len(descriptions),
            EXPECTED_MODS_TOTAL,
        )
        # Multi-value affixes produce more mods than affix instances: 47 instances.
        affix_paths = {
            mod["provenance"]["source_path"]
            for mod in mods_by_source(self.doc, "affix")
        }
        self.assertEqual(len(affix_paths), 47)
        # 2026-11-04 R1: the removed DPS entries are exactly the excluded ones.
        self.assertEqual(
            EXPECTED_MOD_KIND_TOTAL,
            33 + EXPECTED_AFFIX_MODS + EXPECTED_UNIQUE_MODS - EXPECTED_EXCLUDED_NON_DPS,
        )

    def test_affix_value_counts_per_channel(self) -> None:
        channel_counts = {channel: 0 for channel in AFFIX_CHANNELS}
        for mod in mods_by_source(self.doc, "affix"):
            channel_counts[mod["channel"]] += 1
        self.assertEqual(channel_counts, EXPECTED_AFFIX_VALUES_PER_CHANNEL)
        self.assertEqual(sum(channel_counts.values()), EXPECTED_AFFIX_MODS)
        # Tier/channel metadata must be present on every affix mod.
        for mod in mods_by_source(self.doc, "affix"):
            affix = mod["affix"]
            self.assertIsNotNone(affix, mod["mod_id"])
            self.assertTrue(affix["entity_id"], mod["mod_id"])
            self.assertIn(affix["channel"], AFFIX_CHANNELS)
            self.assertIsInstance(affix["source_tier"], int, mod["mod_id"])
            self.assertIsInstance(affix["tier_index"], int, mod["mod_id"])

    def test_mod_ids_unique_and_stable(self) -> None:
        ids = [mod["mod_id"] for mod in self.doc["mods"]]
        self.assertEqual(len(ids), len(set(ids)), "duplicate mod_id")
        # Concrete stable ids from the fixed sample.
        for expected in (
            "equipment.head#implicit#0",
            "equipment.head#affix#normal#0.0",
            "equipment.weapon1#affix#normal#0.1",
            "equipment.amulet#unique_mod#0",
            "equipment.head#unique_description#0",
        ):
            self.assertIn(expected, ids)
        rebuilt = build_calculation_input(self.spec)
        self.assertEqual(ids, [mod["mod_id"] for mod in rebuilt["mods"]])

    def test_every_mod_has_required_keys_and_valid_enums(self) -> None:
        for mod in self.doc["mods"]:
            missing = MOD_REQUIRED_KEYS - set(mod)
            self.assertEqual(missing, set(), f"{mod.get('mod_id')}: missing {sorted(missing)}")
            self.assertIn(mod["source"], VALID_SOURCES, mod["mod_id"])
            self.assertIn(mod["channel"], VALID_CHANNELS, mod["mod_id"])
            self.assertIn(mod["kind"], VALID_KINDS, mod["mod_id"])
            self.assertIn(mod["value_status"], VALUE_STATUSES, mod["mod_id"])
            self.assertIn(mod["value_source"], VALUE_SOURCES, mod["mod_id"])
            self.assertIsInstance(mod["stat"], dict, mod["mod_id"])
            self.assertIsInstance(mod["roll"], dict, mod["mod_id"])
            self.assertIsInstance(mod["range"], dict, mod["mod_id"])
            self.assertIsInstance(mod["provenance"], dict, mod["mod_id"])
            self.assertIsInstance(mod["hidden"], bool, mod["mod_id"])
            self.assertIn("stat_id", mod["stat"])
            self.assertIn("resolved", mod["stat"])
            self.assertIn("kind", mod["roll"])
            self.assertIn("min", mod["range"])
            self.assertIn("range_text", mod["range"])
            self.assertIn("source_path", mod["provenance"])
        # No numeric value is ever fabricated for a range/missing mod.
        for mod in self.doc["mods"]:
            if mod["value_status"] in ("range", "missing", "unresolved"):
                self.assertIsNone(mod["value_numeric"], mod["mod_id"])

    def test_hidden_unique_mods_included_and_marked(self) -> None:
        hidden = [
            mod
            for mod in mods_by_source(self.doc, "unique_mod")
            if mod["hidden"]
        ]
        self.assertEqual(len(hidden), EXPECTED_HIDDEN_UNIQUE_MODS)
        for mod in hidden:
            self.assertIsNotNone(mod["unique"], mod["mod_id"])
            self.assertIsNotNone(mod["unique"]["mod_index"], mod["mod_id"])
            self.assertIsNone(mod["readable_text"], mod["mod_id"])
            self.assertTrue(mod["provenance"]["source_path"].endswith(".ur"))
        non_rollable = [
            mod
            for mod in mods_by_source(self.doc, "unique_mod")
            if mod["unique"] and not mod["unique"]["can_roll"]
        ]
        self.assertEqual(len(non_rollable), EXPECTED_NON_ROLLABLE_UNIQUE_MODS)
        for mod in non_rollable:
            self.assertIsNone(mod["roll"]["kind"])
            self.assertIsNotNone(mod["value_numeric"])

    def test_unique_roll_ids_match_build_spec(self) -> None:
        for item in self.doc["items"]:
            instance = next(
                inst
                for _path, inst in _iter_spec_instances(self.spec)
                if inst.get("instance_id") == item["instance_id"]
            )
            spec_mods = {
                mod.get("mod_index"): mod
                for mod in (instance.get("unique_rolls") or {}).get("mods") or []
            }
            if not spec_mods:
                continue
            for mod in [
                candidate
                for candidate in item["mods"]
                if candidate["source"] == "unique_mod" and candidate["kind"] == "mod"
            ]:
                spec_mod = spec_mods[mod["unique"]["mod_index"]]
                self.assertEqual(mod["unique"]["roll_id"], spec_mod.get("roll_id"), mod["mod_id"])
                self.assertEqual(mod["roll"]["raw"], spec_mod.get("raw_roll"), mod["mod_id"])
                self.assertEqual(mod["roll"]["status"], spec_mod.get("roll_status"), mod["mod_id"])
        apex_first = find_mod(
            self.doc,
            lambda mod: mod["item_instance_id"] == "equipment.head"
            and mod["source"] == "unique_mod"
            and mod["unique"]
            and mod["unique"]["mod_index"] == 0,
        )
        self.assertIsNotNone(apex_first)
        self.assertEqual(apex_first["unique"]["roll_id"], 3)
        self.assertEqual(apex_first["roll"]["raw"], 234)
        self.assertEqual(apex_first["roll"]["kind"], "unique_roll")
        self.assertEqual(apex_first["value_numeric"], 87)
        self.assertEqual(apex_first["value_status"], "actual")

    def test_unique_absent_rolls_keep_definition_range(self) -> None:
        for instance_id in ("equipment.amulet", "equipment.ring2"):
            item = item_instance(self.doc, instance_id)
            range_mods = [
                mod
                for mod in item["mods"]
                if mod["source"] == "unique_mod" and mod["kind"] == "mod"
            ]
            self.assertTrue(range_mods, instance_id)
            for mod in range_mods:
                self.assertEqual(mod["value_status"], "range", mod["mod_id"])
                self.assertIsNone(mod["value_numeric"], mod["mod_id"])
                self.assertIsNone(mod["roll"]["raw"], mod["mod_id"])
                self.assertEqual(mod["roll"]["status"], "absent", mod["mod_id"])
                self.assertIsNotNone(mod["range"]["min"], mod["mod_id"])
                self.assertIsNotNone(mod["range"]["max"], mod["mod_id"])
                self.assertIsNotNone(mod["range"]["direction"], mod["mod_id"])
                # The planner's average branch is exported as a *derived, labelled*
                # display value only; it must never become value_numeric.
                self.assertIsNotNone(mod["derived_display_value"], mod["mod_id"])
                self.assertEqual(
                    mod["effective_value_policy"]["policy_id"], "letools-average-branch-v1"
                )
        low = find_mod(
            self.doc,
            lambda mod: mod["mod_id"] == "equipment.amulet#unique_mod#0",
        )
        self.assertIsNotNone(low)
        self.assertEqual((low["range"]["min"], low["range"]["max"]), (1, 2))

    def test_implicit_mods_range_and_index(self) -> None:
        implicit = mods_by_source(self.doc, "implicit")
        self.assertEqual(len(implicit), EXPECTED_IMPLICIT_MODS)
        self.assertEqual(
            len(implicit), 33 - EXPECTED_EXCLUDED_NON_DPS
        )
        for mod in implicit:
            self.assertEqual(mod["roll"]["kind"], "implicit_roll", mod["mod_id"])
            self.assertIsInstance(mod["roll"]["index"], int, mod["mod_id"])
            self.assertEqual(mod["channel"], "implicit")
            self.assertNotEqual(mod["property"], 104, mod["mod_id"])
            self.assertIsNotNone(mod["range"]["min"], mod["mod_id"])
            self.assertIsNotNone(mod["range"]["max"], mod["mod_id"])
            self.assertIn(mod["mod_id"].split("#implicit#")[1], {str(mod["roll"]["index"])})
        head_first = find_mod(
            self.doc,
            lambda mod: mod["mod_id"] == "equipment.head#implicit#0",
        )
        self.assertIsNotNone(head_first)
        self.assertEqual(head_first["value_numeric"], 77)
        self.assertEqual(head_first["stat"]["name"], "Armor")
        self.assertEqual(head_first["stat"]["unit"], "flat")
        self.assertTrue(head_first["stat"]["resolved"])
        self.assertEqual(head_first["readable_text"], "+77 Armor")

    def test_descriptions_exported(self) -> None:
        descriptions = [mod for mod in self.doc["mods"] if mod["kind"] == "description"]
        self.assertEqual(len(descriptions), EXPECTED_DESCRIPTIONS)
        for mod in descriptions:
            self.assertEqual(mod["source"], "unique_mod")
            self.assertEqual(mod["channel"], "unique")
            self.assertIsNone(mod["value_numeric"])
            self.assertEqual(mod["value_status"], "missing")
            self.assertIsNone(mod["unique"])
            self.assertTrue(mod["readable_text"], mod["mod_id"])
            self.assertIsInstance(mod["provenance"].get("description_index"), int)

    def test_readable_text_attached_for_emitted_lines(self) -> None:
        with_text = [mod for mod in self.doc["mods"] if mod["readable_text"]]
        self.assertGreater(len(with_text), 100)
        fire_res = find_mod(
            self.doc,
            lambda mod: mod["source"] == "affix" and mod["affix"] and mod["affix"]["name"] == "Fire Resistance",
        )
        self.assertIsNotNone(fire_res)
        self.assertIn("Fire Resistance", fire_res["readable_text"])
        self.assertEqual(fire_res["affix"]["source_tier"], 7)
        self.assertEqual(fire_res["affix"]["tier_index"], 6)
        self.assertEqual(fire_res["value_status"], "actual")

    def test_provenance_paths_point_into_the_spec(self) -> None:
        for mod in self.doc["mods"]:
            source_path = mod["provenance"]["source_path"]
            self.assertTrue(source_path, mod["mod_id"])
            if mod["source"] == "affix":
                # e.g. data.equipment.relic.affixes.1 or data.equipment.hands.sealedAffix
                self.assertIn("affix", source_path.lower(), mod["mod_id"])
            elif mod["source"] == "implicit":
                self.assertTrue(source_path.endswith(".ir"), mod["mod_id"])
            elif mod["kind"] == "mod":
                self.assertTrue(source_path.endswith(".ur"), mod["mod_id"])

    def test_notes_are_declarations_not_combat_claims(self) -> None:
        notes = self.doc["notes"]
        self.assertTrue(notes)
        joined = " ".join(str(note) for note in notes).lower()
        self.assertIn("dps", joined)
        self.assertIn("不声明", joined)

    # -- unresolved inputs --------------------------------------------------
    def test_unresolved_inputs_are_exactly_15_targets_and_17_variables(self) -> None:
        unresolved = self.doc["unresolved_inputs"]
        self.assertEqual(len(unresolved), EXPECTED_UNRESOLVED)
        self.assertEqual(
            sum(len(entry.get("variables") or []) for entry in unresolved),
            EXPECTED_VARIABLE_ROWS,
        )
        self.assertEqual(unresolved, unresolved_inputs(self.spec))
        affix_entries = [entry for entry in unresolved if entry["kind"] == "affix_roll"]
        unique_entries = [entry for entry in unresolved if entry["kind"] == "unique_mod_roll"]
        self.assertEqual(len(affix_entries), len(EXPECTED_MISSING_AFFIX_PATHS))
        self.assertEqual(
            {entry["instance_path"] for entry in affix_entries}, EXPECTED_MISSING_AFFIX_PATHS
        )
        self.assertEqual(len(unique_entries), len(OMNIS_ROLL_IDS) + len(UNSATED_ROLL_IDS))
        omnis = {
            entry["roll_id"] for entry in unique_entries if entry["instance_path"] == "data.equipment.amulet"
        }
        unsated = {
            entry["roll_id"] for entry in unique_entries if entry["instance_path"] == "data.equipment.ring2"
        }
        self.assertEqual(omnis, OMNIS_ROLL_IDS)
        self.assertEqual(unsated, UNSATED_ROLL_IDS)
        for entry in unresolved:
            self.assertTrue(entry["suggested_command"], entry)
            self.assertTrue(entry["item_label"], entry)
            self.assertEqual(entry["instance_path"], entry["instance_path"].strip())
            variables = entry["variables"]
            self.assertTrue(variables, entry)
            self.assertEqual(
                len(entry["suggested_variable_commands"]), len(variables), entry
            )
            for variable in variables:
                self.assertIn(variable["unit"], ("percent", "flat"), variable)
                self.assertIsInstance(variable["percent"], bool, variable)
                self.assertIn("range_text", variable)
            if entry["kind"] == "unique_mod_roll":
                self.assertTrue(entry["mod_indexes"], entry)
                self.assertIsInstance(entry["roll_id"], int)
            if entry["kind"] == "affix_roll":
                self.assertIn(entry["channel"], AFFIX_CHANNELS)
                self.assertIsNotNone(entry["affix_entity_id"])

    def test_multi_variable_roll_slots_expose_one_row_per_displayed_variable(self) -> None:
        by_key = {}
        for entry in self.doc["unresolved_inputs"]:
            if entry["kind"] != "unique_mod_roll":
                continue
            by_key[(entry["instance_path"], entry["roll_id"])] = entry
        for (instance_path, roll_id), expected_rows in EXPECTED_MULTI_VARIABLE_ROLLS.items():
            entry = by_key[(instance_path, roll_id)]
            rows = [(variable["unit"], variable["range_text"]) for variable in entry["variables"]]
            self.assertEqual(len(rows), len(expected_rows), entry)
            self.assertEqual(rows, expected_rows, entry)
            # Each row carries its own locator so the two values can be entered separately.
            self.assertEqual(
                len({variable["mod_index"] for variable in entry["variables"]}), len(rows)
            )
            commands = entry["suggested_variable_commands"]
            self.assertEqual(len(commands), len(rows))
            for command in commands:
                self.assertIn("--display-value", command)
                self.assertIn("explicit_value", command)
        # Relic primordial affix: only the missing Spell Damage variable (value index 1)
        # is a target; the constant +6 Level value is already resolvable.
        relic = next(
            entry
            for entry in self.doc["unresolved_inputs"]
            if entry["instance_path"] == "data.equipment.relic.primordialAffix"
        )
        self.assertEqual(len(relic["variables"]), 1)
        self.assertEqual(
            relic["variables"][0]["affix_value_index"], EXPECTED_RELIC_PRIMORDIAL_VALUE_INDEX
        )
        self.assertEqual(relic["variables"][0]["unit"], "percent")
        # Hands sealed/primordial constant-1% affixes are no longer input targets.
        self.assertNotIn("data.equipment.hands.sealedAffix", {entry["instance_path"] for entry in self.doc["unresolved_inputs"]})
        self.assertNotIn("data.equipment.hands.primordialAffix", {entry["instance_path"] for entry in self.doc["unresolved_inputs"]})

    def test_suggested_commands_are_parseable_cli_templates(self) -> None:
        build_id = self.doc["build_id"]
        for entry in self.doc["unresolved_inputs"]:
            tokens = shlex.split(entry["suggested_command"])
            self.assertEqual(tokens[:3], ["le_build", "input", "set"])
            self.assertIn("--build", tokens)
            self.assertEqual(tokens[tokens.index("--build") + 1], build_id)
            self.assertIn("--kind", tokens)
            self.assertEqual(tokens[tokens.index("--kind") + 1], entry["kind"])
            self.assertIn("--instance", tokens)
            self.assertEqual(tokens[tokens.index("--instance") + 1], entry["instance_path"])
            self.assertEqual(tokens[tokens.index("--roll") + 1], "<0-255>")
            if entry["kind"] == "unique_mod_roll":
                self.assertEqual(tokens[tokens.index("--roll-id") + 1], str(entry["roll_id"]))
            if entry["kind"] == "implicit_roll":
                self.assertEqual(tokens[tokens.index("--index") + 1], str(entry["index"]))
            # Per-variable explicit_value templates: copy-paste ready with a placeholder.
            for variable_command in entry["suggested_variable_commands"]:
                variable_tokens = shlex.split(variable_command)
                self.assertEqual(variable_tokens[:3], ["le_build", "input", "set"])
                self.assertEqual(
                    variable_tokens[variable_tokens.index("--kind") + 1], "explicit_value"
                )
                self.assertIn("--target", variable_tokens)
                self.assertIn("--display-value", variable_tokens)
                self.assertEqual(
                    variable_tokens[variable_tokens.index("--display-value") + 1],
                    "<displayed-value>",
                )
                if entry["kind"] == "unique_mod_roll":
                    self.assertIn("--mod-index", variable_tokens)
                if entry["kind"] == "affix_roll":
                    self.assertIn("--affix-value-index", variable_tokens)
        # There is exactly one variable command per variable row.
        self.assertEqual(
            sum(len(entry["suggested_variable_commands"]) for entry in self.doc["unresolved_inputs"]),
            EXPECTED_VARIABLE_ROWS,
        )

    def test_unresolved_inputs_match_input_interface_derivation(self) -> None:
        try:
            from le_build.inputs import describe_targets
        except Exception:  # pragma: no cover - input layer may land later
            self.skipTest("le_build.inputs is not available in this checkout")
        theirs = describe_targets(self.spec)
        ours = self.doc["unresolved_inputs"]

        def identity(entry: dict) -> tuple:
            return (
                entry.get("kind"),
                entry.get("instance_path"),
                entry.get("roll_id"),
                entry.get("index"),
            )

        self.assertEqual(sorted(map(identity, theirs)), sorted(map(identity, ours)))

    # -- determinism / schema ----------------------------------------------
    def test_export_is_deterministic(self) -> None:
        first = build_calculation_input(self.spec)
        second = build_calculation_input(self.spec)
        self.assertEqual(first, second)
        self.assertEqual(dumps(first), dumps(second))
        fixed_first = build_calculation_input(self.spec, generated_at=FIXED_GENERATED_AT)
        fixed_second = build_calculation_input(self.spec, generated_at=FIXED_GENERATED_AT)
        self.assertEqual(
            json.dumps(fixed_first, ensure_ascii=False, indent=2),
            json.dumps(fixed_second, ensure_ascii=False, indent=2),
        )
        self.assertEqual(fixed_first["generated_at"], FIXED_GENERATED_AT)
        # Default generated_at is the revision published_at: byte-stable re-export.
        self.assertEqual(first["generated_at"], self.spec["meta"]["revision"]["published_at"])

    def test_schema_loads_and_document_validates(self) -> None:
        schema = load_schema()
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["properties"]["schema_version"]["const"], "calculation-input-v1")
        self.assertEqual(schema_errors(self.doc), [])

    def test_iter_mods_matches_items_and_top_level_list(self) -> None:
        flat = [mod for item in self.doc["items"] for mod in item["mods"]]
        yield_list = list(iter_mods(self.spec))
        self.assertEqual(yield_list, flat)
        self.assertEqual(yield_list, self.doc["mods"])
        self.assertEqual([mod["mod_id"] for mod in yield_list], [mod["mod_id"] for mod in self.doc["mods"]])

    # -- artifacts (current delivered revision) -----------------------------
    def test_delivered_calculation_artifact_matches_current_revision(self) -> None:
        self.assertTrue(ARTIFACT.is_file(), f"missing calculator export artifact: {ARTIFACT}")
        artifact_bytes = ARTIFACT.read_bytes()
        self.assertEqual(len(artifact_bytes), CURRENT_ARTIFACT_SIZE)
        self.assertEqual(
            hashlib.sha256(artifact_bytes).hexdigest(), CURRENT_ARTIFACT_SHA256
        )
        stored = json.loads(artifact_bytes.decode("utf-8"))
        self.assertEqual(stored["schema_version"], "calculation-input-v1")
        self.assertEqual(stored["revision_id"], CURRENT_REVISION)
        self.assertEqual(stored["revision_id"], self.pointer["revision_id"])
        self.assertEqual(len(stored["mods"]), EXPECTED_MODS_TOTAL)
        self.assertEqual(stored["excluded_non_dps"], EXPECTED_EXCLUDED_NON_DPS)
        self.assertEqual(stored["excluded_non_dps_properties"], EXPECTED_NON_DPS_PROPERTIES)
        self.assertEqual(set(stored["excluded_non_dps_mod_ids"]), EXPECTED_NON_DPS_MOD_IDS)
        # Delivered current revision: all missing values were supplied by the user.
        self.assertEqual(stored["unresolved_inputs"], [])
        self.assertEqual(
            sum(1 for mod in stored["mods"] if mod["value_source"] == "local_value_override"),
            CURRENT_LOCAL_OVERRIDE_MODS,
        )
        self.assertEqual(schema_errors(stored), [])
        self.assertEqual(stored, build_calculation_input(self.current_spec))

    def test_current_revision_export_is_fully_resolved(self) -> None:
        doc = self.current_doc
        self.assertEqual(doc["revision_id"], CURRENT_REVISION)
        self.assertEqual(doc["content_sha256"], CURRENT_CONTENT_SHA256)
        self.assertEqual(len(doc["items"]), EXPECTED_ITEM_COUNT)
        self.assertEqual(len(doc["mods"]), EXPECTED_MODS_TOTAL)
        self.assertEqual(doc["excluded_non_dps"], EXPECTED_EXCLUDED_NON_DPS)
        self.assertEqual(doc["unresolved_inputs"], [])
        self.assertEqual(
            sum(1 for mod in doc["mods"] if mod["value_source"] == "local_value_override"),
            CURRENT_LOCAL_OVERRIDE_MODS,
        )
        self.assertEqual(
            sum(1 for mod in doc["mods"] if mod["value_status"] == "actual"),
            EXPECTED_MOD_KIND_TOTAL,
        )
        self.assertEqual(
            sum(1 for mod in doc["mods"] if mod["value_status"] == "missing"),
            EXPECTED_DESCRIPTIONS,
        )
        self.assertEqual(
            sum(1 for mod in doc["mods"] if mod["property"] == 104), 0
        )
        self.assertEqual(schema_errors(doc), [])

    def test_baseline_revision_is_still_source_only(self) -> None:
        self.assertEqual(self.spec["meta"]["revision"]["revision_id"], BASELINE_REVISION)
        self.assertEqual(
            self.spec["meta"]["revision"]["content_sha256"], BASELINE_CONTENT_SHA256
        )
        self.assertNotIn("local_inputs", self.spec.get("meta") or {})
        self.assertEqual(self.spec["statuses"]["value_resolution"], "partial")
        self.assertEqual(len(self.doc["mods"]), EXPECTED_MODS_TOTAL)
        self.assertEqual(self.doc["excluded_non_dps"], EXPECTED_EXCLUDED_NON_DPS)
        self.assertEqual(len(self.doc["unresolved_inputs"]), EXPECTED_UNRESOLVED)
        self.assertEqual(
            sum(len(entry.get("variables") or []) for entry in self.doc["unresolved_inputs"]),
            EXPECTED_VARIABLE_ROWS,
        )
        self.assertEqual(
            sum(1 for mod in self.doc["mods"] if mod["value_source"] == "local_value_override"), 0
        )


def _iter_spec_instances(spec: dict):
    for slot in (spec.get("equipment") or {}).get("slots") or []:
        instance = (slot or {}).get("instance") or {}
        if instance:
            yield instance.get("instance_id"), instance
    for entry in (spec.get("idols") or {}).get("entries") or []:
        instance = (entry or {}).get("instance") or {}
        if instance:
            yield instance.get("instance_id"), instance
    for entry in (spec.get("blessings") or {}).get("entries") or []:
        instance = (entry or {}).get("instance") or {}
        if instance:
            yield instance.get("instance_id"), instance


# ---------------------------------------------------------------------------
# Legacy revision degradation tests
# ---------------------------------------------------------------------------
class LegacyRevisionExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = load_revision_spec(LEGACY_REVISION_ID)
        cls.doc = build_calculation_input(cls.spec)

    def test_legacy_export_does_not_crash_and_keeps_structured_data(self) -> None:
        doc = self.doc
        self.assertEqual(doc["schema_version"], "calculation-input-v1")
        self.assertEqual(doc["revision_id"], LEGACY_REVISION_ID)
        self.assertEqual(len(doc["items"]), EXPECTED_ITEM_COUNT)
        self.assertTrue(doc["mods"])
        counts = {}
        for mod in doc["mods"]:
            counts[mod["source"]] = counts.get(mod["source"], 0) + 1
        self.assertEqual(counts, LEGACY_MOD_COUNTS)
        self.assertEqual({mod["kind"] for mod in doc["mods"]}, {"mod"})
        # Historical source has no drop-rate property in the BuildSpec, so the R1
        # exclusion is declared but applies to nothing (raw legacy data untouched).
        self.assertEqual(doc["excluded_non_dps"], 0)
        self.assertEqual(doc["excluded_non_dps_properties"], EXPECTED_NON_DPS_PROPERTIES)
        self.assertEqual(doc["excluded_non_dps_mod_ids"], [])
        self.assertEqual(schema_errors(doc), [])

    def test_legacy_readable_text_is_null_but_values_exported(self) -> None:
        self.assertTrue(all(mod["readable_text"] is None for mod in self.doc["mods"]))
        resolved = [
            mod
            for mod in self.doc["mods"]
            if mod["source"] == "unique_mod" and mod["value_numeric"] is not None
        ]
        self.assertTrue(resolved, "legacy unique values should still be exported")
        head_first = find_mod(self.doc, lambda mod: mod["mod_id"] == "equipment.head#unique_mod#0")
        self.assertIsNotNone(head_first)
        self.assertEqual(head_first["value_numeric"], 87)
        self.assertEqual(head_first["roll"]["raw"], 234)

    def test_legacy_scope_is_synthesized_and_notes_explain_degradation(self) -> None:
        self.assertIsNone(self.doc["scope"]["scope_version"])
        self.assertIn("equipment", self.doc["scope"]["included_sections"])
        joined = " ".join(self.doc["notes"]).lower()
        self.assertIn("历史 revision", joined)
        self.assertIn("readable_text 为 null", joined)

    def test_legacy_unresolved_inputs_are_affix_only(self) -> None:
        unresolved = self.doc["unresolved_inputs"]
        self.assertTrue(unresolved)
        self.assertEqual({entry["kind"] for entry in unresolved}, {"affix_roll"})
        self.assertEqual(
            {entry["instance_path"] for entry in unresolved}, EXPECTED_MISSING_AFFIX_PATHS
        )
        self.assertEqual(
            sum(len(entry.get("variables") or []) for entry in unresolved), 4
        )


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
