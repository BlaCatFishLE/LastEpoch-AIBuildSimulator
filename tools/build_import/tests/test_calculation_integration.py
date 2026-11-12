"""Integration tests: calculator export (``calculation-input-v1``) 与本地输入同步。

Contract: ``docs/phase-2-local-input-and-calc-export.zh-CN.md`` §4/§5.

Two layers:

1. **API layer** (read-only, no DB/network): ``build_calculation_input(spec)`` on the
   delivered published BuildSpec — stable ``mod_id``, hidden mods included, affix/
   implicit/unique coverage, per-variable ``unresolved_inputs`` == **15 targets /
   17 variable rows** (4 missing relic affix rolls; Omnis 7 rollIds + Unsated Rage 4
   rollIds; multi-variable roll slots expose one row per displayed variable),
   **148 mods (153 - 5 non-DPS drop-rate mods; re-review R1)**, deterministic output,
   schema validation against ``schemas/calculation-input-v1.json``, legacy revision
   does not crash.
2. **CLI integration layer**: in a throw-away copy of the *baseline* build tree, import
   the 3 per-variable ``explicit_value`` overrides (Omnis rollId 0 → Level of Skills +2
   flat + Physical Resistance 35%; relic primordial affix value 1 → Spell Damage 180%),
   run ``le_build sync`` (catalog via ``--db`` = real read-only SQLite), then
   ``le_build export`` — the exported mods must show ``value_source=local_value_override``
   / resolved stat names, sibling values must keep ``source`` provenance,
   ``unresolved_inputs`` must shrink to **13 targets / 14 variable rows**, the export must
   be deterministic and schema-valid, and the real ``data/builds`` must stay untouched.

These tests are red until the implementation lands; they intentionally consume the
contract's public API names (``build_calculation_input`` / ``CALCULATION_SCHEMA_VERSION``).

Run from ``tools/build_import``::

    $env:PYTHONPATH='src'; & $py -m unittest tests.test_calculation_integration -v
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from le_build.calculation import CALCULATION_SCHEMA_VERSION, build_calculation_input
from le_build.validate import _validate_schema_node

from _dataset_asset import require_dataset_db

REPO = Path(__file__).resolve().parents[3]
BUILD_IMPORT = REPO / "tools" / "build_import"
SCHEMA_PATH = BUILD_IMPORT / "schemas" / "calculation-input-v1.json"
DB = REPO / "data" / "releases" / "phase1-dataset-003" / "data.sqlite"
BUILD_REF = "letools:o3ZRm9my"
SHARE_ID = "o3ZRm9my"
SOURCE_BUILD_DIR = REPO / "data" / "builds" / "letools" / SHARE_ID
ARTIFACT = REPO / "reports" / "phase-2" / "o3ZRm9my" / "calculation-input.json"
LEGACY_REVISION_ID = "rev-8dc664d73737a8b9"
# 2026-11-04 (re-review R1/R2): delivered current = rev-be27 (seq 6, 17 user inputs);
# source-only baseline = rev-dbcf (seq 5).  Old rev-5e0e/rev-b0f2 stay historical.
BASELINE_REVISION = "rev-dbcf35ba60b0dd95"
BASELINE_CONTENT_SHA256 = (
    "dbcf35ba60b0dd956d4f08b0faf26b91e888f3762c1e67ec210bd2c89b17d354"
)
CURRENT_REVISION = "rev-c746411ccebee9c0"
CURRENT_CONTENT_SHA256 = (
    "c746411ccebee9c04b67da4a40cfc21d1e6770530e284bd2de8160ed0199932b"
)
CURRENT_LOCAL_OVERRIDE_MODS = 17
# Re-review R1: the 5 drop-rate blessing implicits (propertyList 104) are excluded from
# the calculator export; 153 -> 148 flat mods (28 implicit / 61 affix / 59 unique + 11 desc).
EXPECTED_FILTERED_MODS = 148
EXPECTED_IMPLICIT_MODS = 28
EXPECTED_EXCLUDED_NON_DPS = 5

MISSING_AFFIX_PATHS = [
    "data.equipment.relic.affixes.1",
    "data.equipment.relic.affixes.2",
    "data.equipment.relic.affixes.3",
    "data.equipment.relic.primordialAffix",
]
OMNIS_ROLL_IDS = set(range(7))
UNSATED_ROLL_IDS = set(range(4))
# 2026-11-03 per-variable fix: 15 target groups / 17 displayed variable rows on the
# source-only BASELINE revision (delivered current has 0 missing values after the user sync).
EXPECTED_UNRESOLVED = 15
EXPECTED_VARIABLE_ROWS = 17
EXPECTED_UNRESOLVED_AFTER_OVERRIDES = 13
EXPECTED_VARIABLE_ROWS_AFTER_OVERRIDES = 14
EXPECTED_ITEM_COUNT = 29
EXPECTED_MODS_TOTAL = EXPECTED_FILTERED_MODS
EXPECTED_MOD_MODS = 137
EXPECTED_UNIQUE_MODS = 48
EXPECTED_DESCRIPTIONS = 11
EXPECTED_CALC_ARTIFACT_SIZE = 524731
EXPECTED_CALC_ARTIFACT_SHA256 = (
    "531f7a50ec338cf0765bf0008a05098f044e38f5e134420241f149ef4f9aa561"
)
EXPECTED_MODS_AFTER_SYNC = EXPECTED_FILTERED_MODS
RELIC_PRIMORDIAL_VALUE_INDEX = 1

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
VALID_VALUE_STATUS = {"actual", "range", "constant", "missing", "unresolved"}
VALID_VALUE_SOURCE = {"source", "local_input", "local_value_override"}
VALID_SOURCES = {"affix", "implicit", "unique_mod"}
VALID_CHANNELS = {"unique", "implicit", "normal", "sealed", "primordial", "corrupted"}
VALID_KINDS = {"mod", "description"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def load_current_spec() -> dict:
    pointer = json.loads((SOURCE_BUILD_DIR / "current.json").read_text(encoding="utf-8"))
    revision = pointer["revision_id"]
    return json.loads(
        (SOURCE_BUILD_DIR / "revisions" / revision / "build.json").read_text(encoding="utf-8")
    )


def load_revision_spec(revision_id: str) -> dict:
    return json.loads(
        (SOURCE_BUILD_DIR / "revisions" / revision_id / "build.json").read_text(encoding="utf-8")
    )


def _baseline_pointer() -> dict:
    pointer = json.loads((SOURCE_BUILD_DIR / "current.json").read_text(encoding="utf-8"))
    baseline = load_revision_spec(BASELINE_REVISION)
    meta = baseline["meta"]["revision"]
    pointer.update(
        {
            "revision_id": meta["revision_id"],
            "content_sha256": meta["content_sha256"],
            "revision_seq": meta["revision_seq"],
            "published_at": meta["published_at"],
            "previous_revision_id": meta.get("previous_revision_id"),
            "statuses": baseline.get("statuses", {}),
            "updated_at": meta["published_at"],
        }
    )
    return pointer


def seed_baseline_tree(root: Path) -> Path:
    """Copy the delivered tree into ``root`` and restore the source-only baseline state."""
    target = root / "data" / "builds" / "letools" / SHARE_ID
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE_BUILD_DIR, target)
    inputs_dir = target / "inputs"
    if inputs_dir.is_dir():
        shutil.rmtree(inputs_dir)
    current_revision = json.loads(
        (SOURCE_BUILD_DIR / "current.json").read_text(encoding="utf-8")
    )["revision_id"]
    if current_revision != BASELINE_REVISION:
        stale = target / "revisions" / current_revision
        if stale.is_dir():
            shutil.rmtree(stale)
    (target / "current.json").write_text(
        json.dumps(_baseline_pointer(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def drop_rate_mods(document: dict) -> list[dict]:
    """Mods whose source property identity is Drop Rate (re-review R1 filtering)."""
    return [
        mod
        for mod in document.get("mods") or []
        if mod.get("property") == 104
        or str((mod.get("stat") or {}).get("stat_id") or "").endswith("propertyList:104")
    ]


def schema_errors(document) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors: list[str] = []
    _validate_schema_node(document, schema, schema, "$", errors)
    return errors


def _run_cli(root: Path, *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    command = [sys.executable, "-m", "le_build", "--root", str(root), *args]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BUILD_IMPORT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("LE_REPO_ROOT", None)
    return subprocess.run(
        command,
        cwd=str(BUILD_IMPORT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def make_multi_variable_sync_inputs() -> dict:
    """The 3 per-variable explicit overrides: one multi-variable roll slot + one affix.

    * Omnis rollId 0 drives two displayed variables: Level of Skills (+2 flat) and
      Physical Resistance (35% -> 0.35);
    * the relic primordial affix value 1 is Spell Damage (180% -> 1.8).
    """
    return {
        "version": "build-local-inputs-v1",
        "build_id": BUILD_REF,
        "updated_at": "2026-11-03T23:30:00+08:00",
        "entries": [
            {
                "input_id": "in-20261103T233000-9101",
                "kind": "explicit_value",
                "instance_path": "data.equipment.amulet",
                "target": "unique_mod",
                "mod_index": 0,
                "value_numeric": 2.0,
                "display_value": 2.0,
                "display_unit": "flat",
                "created_at": "2026-11-03T23:30:00+08:00",
            },
            {
                "input_id": "in-20261103T233000-9102",
                "kind": "explicit_value",
                "instance_path": "data.equipment.amulet",
                "target": "unique_mod",
                "mod_index": 1,
                "value_numeric": 0.35,
                "display_value": 35.0,
                "display_unit": "percent",
                "created_at": "2026-11-03T23:30:00+08:00",
            },
            {
                "input_id": "in-20261103T233000-9103",
                "kind": "explicit_value",
                "instance_path": "data.equipment.relic.primordialAffix",
                "target": "affix",
                "affix_value_index": RELIC_PRIMORDIAL_VALUE_INDEX,
                "value_numeric": 1.8,
                "display_value": 180.0,
                "display_unit": "percent",
                "created_at": "2026-11-03T23:30:00+08:00",
            },
        ],
    }


def _find_mod(doc: dict, predicate) -> dict | None:
    for mod in doc.get("mods") or []:
        if predicate(mod):
            return mod
    return None


class CalculationExportApiTest(unittest.TestCase):
    """Read-only API tests against the delivered published revision."""

    @classmethod
    def setUpClass(cls) -> None:
        # Source-only expectations use the pinned BASELINE revision; the delivered
        # current revision (user-synced) is exported separately in the artifact test.
        cls.spec = load_revision_spec(BASELINE_REVISION)
        cls.pointer = json.loads((SOURCE_BUILD_DIR / "current.json").read_text(encoding="utf-8"))
        cls.current_spec = load_current_spec()
        cls.doc = build_calculation_input(cls.spec)

    def test_schema_version_and_top_level_fields(self) -> None:
        doc = self.doc
        self.assertEqual(CALCULATION_SCHEMA_VERSION, "calculation-input-v1")
        self.assertEqual(doc.get("schema_version"), "calculation-input-v1")
        for key in (
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
            "unresolved_inputs",
            "notes",
        ):
            self.assertIn(key, doc, f"missing top-level key {key!r}")
        self.assertEqual(doc["build_id"], BUILD_REF)
        self.assertEqual(doc["revision_id"], BASELINE_REVISION)
        self.assertEqual(doc["source"], "letools")
        self.assertEqual(doc["game_version"], "1.5.2")
        self.assertEqual(doc["dataset_id"], "phase1-dataset-002")
        self.assertEqual(doc["character"]["level"], 100)
        self.assertEqual(doc["character"]["class"]["name"], "Mage")
        self.assertEqual(doc["character"]["mastery"]["name"], "Sorcerer")
        self.assertEqual(doc["passive_tree"]["points_total"], 113)
        self.assertEqual(len(doc["skills"]["specializations"]), 5)
        self.assertEqual(len(doc["skills"]["action_bar"]), 5)
        self.assertTrue(doc.get("notes"))
        joined_notes = " ".join(str(note) for note in doc["notes"]).lower()
        self.assertIn("dps", joined_notes)

    def test_schema_validation(self) -> None:
        self.assertTrue(SCHEMA_PATH.is_file(), "schemas/calculation-input-v1.json is missing")
        errors = schema_errors(self.doc)
        self.assertEqual(errors, [], f"calculation-input-v1 schema violations: {errors[:10]}")

    def test_items_wrapper(self) -> None:
        items = self.doc["items"]
        self.assertEqual(len(items), 29)
        for item in items:
            self.assertIsInstance(item.get("mods"), list, item.get("instance_id"))
            self.assertTrue(item.get("instance_id"))
            self.assertIn("display_name", item)
        # Flat top-level list is the same stable order/spelling.
        flat = [mod for item in items for mod in item["mods"]]
        self.assertEqual(len(flat), len(self.doc["mods"]))
        self.assertEqual(
            [mod["mod_id"] for mod in flat], [mod["mod_id"] for mod in self.doc["mods"]]
        )

    def test_every_mod_has_a_unique_id_and_required_fields(self) -> None:
        mods = self.doc["mods"]
        self.assertGreater(len(mods), 100)
        ids = [mod["mod_id"] for mod in mods]
        self.assertEqual(len(ids), len(set(ids)), "duplicate mod_id in export")
        for mod in mods:
            missing = MOD_REQUIRED_KEYS - set(mod)
            self.assertEqual(missing, set(), f"{mod.get('mod_id')}: missing {sorted(missing)}")
            self.assertIn(mod["source"], VALID_SOURCES, mod["mod_id"])
            self.assertIn(mod["channel"], VALID_CHANNELS, mod["mod_id"])
            self.assertIn(mod["kind"], VALID_KINDS, mod["mod_id"])
            self.assertIn(mod["value_status"], VALID_VALUE_STATUS, mod["mod_id"])
            self.assertIn(mod["value_source"], VALID_VALUE_SOURCE, mod["mod_id"])
            self.assertIsInstance(mod["stat"], dict, mod["mod_id"])
            self.assertIsInstance(mod["roll"], dict, mod["mod_id"])
            self.assertIsInstance(mod["range"], dict, mod["mod_id"])
            self.assertIsInstance(mod["provenance"], dict, mod["mod_id"])

    def test_affix_implicit_and_unique_mods_are_all_represented(self) -> None:
        mods = self.doc["mods"]
        counts = {"affix": 0, "implicit": 0, "unique_mod": 0}
        for mod in mods:
            counts[mod["source"]] += 1
        # 47 affix instances (multi-value affix may yield >1 mod each), exactly 47
        # distinct affix source paths; the flat list must cover every channel.
        affix_paths = set()
        for item in self.doc["items"]:
            for mod in item["mods"]:
                if mod["source"] == "affix":
                    affix_paths.add(mod["provenance"].get("source_path"))
        self.assertEqual(len(affix_paths), 47)
        self.assertIn("normal", {mod["channel"] for mod in mods if mod["source"] == "affix"})
        self.assertIn("sealed", {mod["channel"] for mod in mods if mod["source"] == "affix"})
        self.assertIn("primordial", {mod["channel"] for mod in mods if mod["source"] == "affix"})
        self.assertIn("corrupted", {mod["channel"] for mod in mods if mod["source"] == "affix"})
        self.assertGreater(counts["implicit"], 0)
        self.assertGreater(counts["unique_mod"], 0)
        # Hidden internal unique mods (12 in the fixed sample) must be exported.
        hidden = [mod for mod in mods if mod["source"] == "unique_mod" and mod["hidden"]]
        self.assertGreaterEqual(len(hidden), 12)
        # Unique tooltip descriptions are exported as kind=description entries.
        descriptions = [mod for mod in mods if mod["kind"] == "description"]
        self.assertEqual(len(descriptions), 11)
        # Apex hidden property=42 mods carry unique.mod_index + can_roll.
        apex_hidden = [
            mod
            for mod in mods
            if mod["item_instance_id"] == "equipment.head"
            and mod["source"] == "unique_mod"
            and mod["hidden"]
        ]
        self.assertGreaterEqual(len(apex_hidden), 7)
        for mod in apex_hidden:
            self.assertIn("unique", mod)
            self.assertIsNotNone(mod["unique"]["mod_index"])

    def test_affix_tier_and_channel_information_is_exported(self) -> None:
        fire_res = _find_mod(
            self.doc,
            lambda mod: mod["source"] == "affix"
            and mod.get("affix")
            and mod["affix"].get("name") == "Fire Resistance"
            and mod["channel"] == "normal",
        )
        self.assertIsNotNone(fire_res)
        self.assertEqual(fire_res["affix"]["source_tier"], 7)
        self.assertEqual(fire_res["affix"]["tier_index"], 6)
        self.assertEqual(fire_res["value_status"], "actual")

    def test_per_mod_roll_ids_match_the_build_spec(self) -> None:
        # Apex first unique mod: rollId=3, ur[3]=234, value 87.
        apex_first = _find_mod(
            self.doc,
            lambda mod: mod["item_instance_id"] == "equipment.head"
            and mod["source"] == "unique_mod"
            and mod["unique"]
            and mod["unique"].get("mod_index") == 0,
        )
        self.assertIsNotNone(apex_first)
        self.assertEqual(apex_first["unique"]["roll_id"], 3)
        self.assertEqual(apex_first["roll"]["raw"], 234)
        self.assertEqual(apex_first["roll"]["kind"], "unique_roll")
        self.assertIsNotNone(apex_first["value_numeric"])
        self.assertEqual(apex_first["value_status"], "actual")

    def test_readable_text_is_attached_for_non_hidden_lines(self) -> None:
        with_text = [mod for mod in self.doc["mods"] if mod.get("readable_text")]
        self.assertGreater(len(with_text), 100)
        fire_res = _find_mod(
            self.doc,
            lambda mod: mod["source"] == "affix" and mod["affix"].get("name") == "Fire Resistance",
        )
        self.assertIn("Fire Resistance", fire_res["readable_text"])
        # Hidden description-covered mods keep structured data; they are not required to
        # have their own readable text (the combined description carries it).
        hidden = [mod for mod in self.doc["mods"] if mod["source"] == "unique_mod" and mod["hidden"]]
        self.assertTrue(hidden)

    def test_unresolved_inputs_are_exactly_15_targets_and_17_variables(self) -> None:
        unresolved = self.doc["unresolved_inputs"]
        self.assertEqual(len(unresolved), EXPECTED_UNRESOLVED, unresolved)
        self.assertEqual(
            sum(len(entry.get("variables") or []) for entry in unresolved),
            EXPECTED_VARIABLE_ROWS,
        )
        affix_entries = [entry for entry in unresolved if entry["kind"] == "affix_roll"]
        unique_entries = [entry for entry in unresolved if entry["kind"] == "unique_mod_roll"]
        self.assertEqual(len(affix_entries), 4)
        self.assertEqual(len(unique_entries), 11)
        self.assertEqual(
            sorted(entry["instance_path"] for entry in affix_entries), sorted(MISSING_AFFIX_PATHS)
        )
        omnis = {
            entry.get("roll_id")
            for entry in unique_entries
            if entry["instance_path"] == "data.equipment.amulet"
        }
        unsated = {
            entry.get("roll_id")
            for entry in unique_entries
            if entry["instance_path"] == "data.equipment.ring2"
        }
        self.assertEqual(omnis, OMNIS_ROLL_IDS)
        self.assertEqual(unsated, UNSATED_ROLL_IDS)
        for entry in unresolved:
            self.assertTrue(entry.get("suggested_command"), entry)
            self.assertIn("input", entry["suggested_command"])
            self.assertIn("set", entry["suggested_command"])
            self.assertTrue(entry.get("variables"), entry)
            self.assertEqual(
                len(entry["suggested_variable_commands"]), len(entry["variables"]), entry
            )
            for variable in entry["variables"]:
                self.assertIn(variable["unit"], ("percent", "flat"))
                self.assertIn("range_text", variable)
        # Multi-variable roll slots expose both displayed variables separately.
        amulet_roll_0 = next(
            entry
            for entry in unresolved
            if entry["instance_path"] == "data.equipment.amulet" and entry.get("roll_id") == 0
        )
        self.assertEqual(len(amulet_roll_0["variables"]), 2)
        self.assertEqual(
            [(variable["unit"], variable["range_text"]) for variable in amulet_roll_0["variables"]],
            [("flat", "1–2"), ("percent", "1%–45%")],
        )
        # Hands constant-1% affixes are not targets any more.
        self.assertNotIn("data.equipment.hands.sealedAffix", {entry["instance_path"] for entry in unresolved})
        self.assertNotIn("data.equipment.hands.primordialAffix", {entry["instance_path"] for entry in unresolved})

    def test_export_is_deterministic(self) -> None:
        first = build_calculation_input(self.spec)
        second = build_calculation_input(self.spec)
        self.assertEqual(
            json.dumps(first, sort_keys=True, ensure_ascii=False),
            json.dumps(second, sort_keys=True, ensure_ascii=False),
            "repeat export of the same revision must be byte-identical",
        )

    def test_legacy_revision_export_does_not_crash(self) -> None:
        legacy = load_revision_spec(LEGACY_REVISION_ID)
        doc = build_calculation_input(legacy)
        self.assertEqual(doc["revision_id"], LEGACY_REVISION_ID)
        self.assertEqual(doc["schema_version"], "calculation-input-v1")
        self.assertTrue(doc["mods"])
        # Legacy revision has no readable layer: structured values are exported with
        # readable_text=null instead of crashing.
        self.assertTrue(any(mod["readable_text"] is None for mod in doc["mods"]))

    def test_delivered_calculation_artifact_matches_current_revision(self) -> None:
        self.assertTrue(
            ARTIFACT.is_file(),
            f"calculator export artifact missing: {ARTIFACT.relative_to(REPO)}",
        )
        artifact_bytes = ARTIFACT.read_bytes()
        self.assertEqual(len(artifact_bytes), EXPECTED_CALC_ARTIFACT_SIZE)
        self.assertEqual(
            hashlib.sha256(artifact_bytes).hexdigest(),
            EXPECTED_CALC_ARTIFACT_SHA256,
        )
        stored = json.loads(artifact_bytes.decode("utf-8"))
        self.assertEqual(stored.get("schema_version"), "calculation-input-v1")
        # Delivered current revision = user-synced local-input revision, fully resolved.
        self.assertEqual(stored.get("revision_id"), CURRENT_REVISION)
        self.assertEqual(stored.get("revision_id"), self.pointer["revision_id"])
        self.assertEqual(stored.get("unresolved_inputs"), [])
        self.assertEqual(
            sum(1 for mod in stored["mods"] if mod["value_source"] == "local_value_override"),
            CURRENT_LOCAL_OVERRIDE_MODS,
        )
        # Re-review R1: drop-rate mods are filtered from the calculator export.
        self.assertEqual(len(stored["mods"]), EXPECTED_MODS_TOTAL)
        self.assertEqual(len(stored["mods"]), 148)
        self.assertIn("excluded_non_dps", stored)
        self.assertEqual(stored["excluded_non_dps"], EXPECTED_EXCLUDED_NON_DPS)
        self.assertEqual(stored["excluded_non_dps_properties"], [104])
        self.assertEqual(len(stored["excluded_non_dps_mod_ids"]), 5)
        self.assertEqual(drop_rate_mods(stored), [])
        self.assertEqual(
            sum(1 for mod in stored["mods"] if mod["source"] == "implicit"),
            EXPECTED_IMPLICIT_MODS,
        )
        self.assertEqual(schema_errors(stored), [])
        # Content must be the real export of the delivered current revision.
        self.assertEqual(stored, build_calculation_input(self.current_spec))

    def test_current_revision_export_has_no_unresolved_inputs(self) -> None:
        self.assertEqual(self.pointer["revision_id"], CURRENT_REVISION)
        self.assertEqual(self.pointer["content_sha256"], CURRENT_CONTENT_SHA256)
        doc = build_calculation_input(self.current_spec)
        self.assertEqual(doc["revision_id"], CURRENT_REVISION)
        self.assertEqual(doc["unresolved_inputs"], [])
        self.assertEqual(len(doc["items"]), EXPECTED_ITEM_COUNT)
        self.assertEqual(len(doc["mods"]), EXPECTED_MODS_TOTAL)
        self.assertEqual(drop_rate_mods(doc), [])
        self.assertEqual(
            sum(1 for mod in doc["mods"] if mod["source"] == "implicit"),
            EXPECTED_IMPLICIT_MODS,
        )
        self.assertEqual(doc["excluded_non_dps"], EXPECTED_EXCLUDED_NON_DPS)
        self.assertEqual(doc["excluded_non_dps_properties"], [104])
        self.assertEqual(len(doc["excluded_non_dps_mod_ids"]), 5)
        self.assertEqual(
            sum(1 for mod in doc["mods"] if mod["kind"] == "mod"), EXPECTED_MOD_MODS
        )
        self.assertEqual(
            sum(1 for mod in doc["mods"] if mod["kind"] == "description"),
            EXPECTED_DESCRIPTIONS,
        )
        self.assertEqual(
            sum(
                1
                for mod in doc["mods"]
                if mod["source"] == "unique_mod" and mod["kind"] == "mod"
            ),
            EXPECTED_UNIQUE_MODS,
        )
        self.assertEqual(schema_errors(doc), [])

    def test_baseline_revision_remains_source_only(self) -> None:
        self.assertEqual(self.spec["meta"]["revision"]["revision_id"], BASELINE_REVISION)
        self.assertEqual(self.spec["meta"]["revision"]["revision_seq"], 5)
        self.assertEqual(
            self.spec["meta"]["revision"]["content_sha256"], BASELINE_CONTENT_SHA256
        )
        self.assertNotIn("local_inputs", self.spec.get("meta") or {})
        self.assertEqual(self.spec["statuses"]["value_resolution"], "partial")
        self.assertEqual(len(self.doc["unresolved_inputs"]), EXPECTED_UNRESOLVED)
        self.assertEqual(
            sum(len(entry.get("variables") or []) for entry in self.doc["unresolved_inputs"]),
            EXPECTED_VARIABLE_ROWS,
        )
        # Drop-rate filtering applies to the baseline export too.
        self.assertEqual(len(self.doc["mods"]), EXPECTED_MODS_TOTAL)
        self.assertEqual(drop_rate_mods(self.doc), [])


class CalculationExportCliIntegrationTest(unittest.TestCase):
    """`le_build export` after a temp-root `le_build sync` with local inputs."""

    @classmethod
    def setUpClass(cls) -> None:
        require_dataset_db()
        cls._temp = tempfile.TemporaryDirectory(prefix="le_calc_integration_")
        cls.root = Path(cls._temp.name)
        target = seed_baseline_tree(cls.root)
        cls.build_dir = target
        cls.inputs_path = target / "inputs" / "local-inputs.json"
        cls.old_revision = json.loads(
            (target / "current.json").read_text(encoding="utf-8")
        )["revision_id"]
        cls.old_build_bytes = (
            target / "revisions" / cls.old_revision / "build.json"
        ).read_bytes()

        inputs_document = make_multi_variable_sync_inputs()
        path = cls.root / "tmp-inputs" / "multi-var.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(inputs_document, ensure_ascii=False, indent=2), encoding="utf-8")
        cls.import_proc = _run_cli(
            cls.root, "input", "import", "--build", BUILD_REF, "--file", str(path)
        )
        cls.sync_proc = _run_cli(
            cls.root, "sync", "--build", BUILD_REF, "--db", str(DB)
        )
        cls.synced_revision = json.loads(
            (target / "current.json").read_text(encoding="utf-8")
        )["revision_id"]
        cls.synced_spec = json.loads(
            (target / "revisions" / cls.synced_revision / "build.json").read_text(encoding="utf-8")
        )
        cls.export_proc = _run_cli(
            cls.root,
            "export",
            "--build",
            BUILD_REF,
            "--out",
            str(cls.root / "calc-1.json"),
        )
        cls.export_proc_2 = _run_cli(
            cls.root,
            "export",
            "--build",
            BUILD_REF,
            "--out",
            str(cls.root / "calc-2.json"),
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def test_sync_published_a_new_revision_with_provenance(self) -> None:
        self.assertEqual(self.import_proc.returncode, 0, self.import_proc.stderr)
        self.assertEqual(self.sync_proc.returncode, 0, self.sync_proc.stderr)
        self.assertNotEqual(self.synced_revision, self.old_revision)
        meta_local = (self.synced_spec.get("meta") or {}).get("local_inputs") or {}
        self.assertEqual(meta_local.get("entries"), 3)
        self.assertEqual(meta_local.get("applied"), 3)
        self.assertTrue(meta_local.get("sha256"))
        # Old revision stays byte-identical.
        self.assertEqual(
            (self.build_dir / "revisions" / self.old_revision / "build.json").read_bytes(),
            self.old_build_bytes,
        )
        # Per-variable overrides resolve stat names on the ur=[] unique mods and make the
        # player-readable lines actual with the exact displayed texts.
        amulet = next(
            entry["instance"] for entry in self.synced_spec["equipment"]["slots"]
            if entry["slot"] == "amulet"
        )
        self.assertEqual(amulet["unique_rolls"].get("local_value_overrides"), 2)
        self.assertEqual(
            amulet["unique_rolls"]["mods"][0]["restored"]["stat_name"], "Level of Skills"
        )
        self.assertEqual(
            amulet["unique_rolls"]["mods"][1]["restored"]["stat_name"], "Physical Resistance"
        )
        amulet_lines = {
            line["diagnostics"]["mod_index"]: line
            for line in amulet["readable"]["lines"]
            if (line.get("diagnostics") or {}).get("mod_index") in (0, 1)
        }
        self.assertEqual(amulet_lines[0]["text"], "+2 to Level of Skills")
        self.assertEqual(amulet_lines[1]["text"], "+35% Physical Resistance")
        self.assertEqual(amulet_lines[0]["value_status"], "actual")
        self.assertEqual(amulet_lines[1]["value_status"], "actual")
        # Sibling provenance: the constant +6 Level value of the primordial affix keeps
        # source provenance; only the supplied Spell Damage value is a local override.
        relic = next(
            entry["instance"] for entry in self.synced_spec["equipment"]["slots"]
            if entry["slot"] == "relic"
        )
        primordial = relic["affixes"]["primordial"][0]
        self.assertIsNone((primordial.get("value") or {}).get("value_source"))
        self.assertEqual((primordial.get("value") or {}).get("local_value_overrides"), 1)
        self.assertIsNone(primordial["value"]["values"][0].get("value_source"))
        self.assertEqual(
            primordial["value"]["values"][1].get("value_source"), "local_value_override"
        )

    def test_export_after_sync_marks_local_values(self) -> None:
        self.assertEqual(self.export_proc.returncode, 0, self.export_proc.stderr)
        doc = json.loads((self.root / "calc-1.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["revision_id"], self.synced_revision)
        self.assertEqual(len(doc["unresolved_inputs"]), EXPECTED_UNRESOLVED_AFTER_OVERRIDES)
        self.assertEqual(
            sum(len(entry.get("variables") or []) for entry in doc["unresolved_inputs"]),
            EXPECTED_VARIABLE_ROWS_AFTER_OVERRIDES,
        )
        self.assertEqual(schema_errors(doc), [])

        # The two Omnis rollId-0 variables are now actual values with resolved stats.
        level = _find_mod(
            doc,
            lambda mod: mod["item_instance_id"] == "equipment.amulet"
            and mod["source"] == "unique_mod"
            and mod["unique"]
            and mod["unique"].get("mod_index") == 0,
        )
        self.assertIsNotNone(level)
        self.assertEqual(level["value_source"], "local_value_override")
        self.assertEqual(level["value_status"], "actual")
        self.assertEqual(level["value_numeric"], 2.0)
        self.assertEqual(level["display_value"], "2")
        self.assertTrue(level["stat"]["resolved"])
        self.assertEqual(level["stat"]["name"], "Level of Skills")
        self.assertEqual(level["stat"]["unit"], "flat")

        resistance = _find_mod(
            doc,
            lambda mod: mod["item_instance_id"] == "equipment.amulet"
            and mod["source"] == "unique_mod"
            and mod["unique"]
            and mod["unique"].get("mod_index") == 1,
        )
        self.assertIsNotNone(resistance)
        self.assertEqual(resistance["value_source"], "local_value_override")
        self.assertAlmostEqual(resistance["value_numeric"], 0.35, places=9)
        self.assertEqual(resistance["display_value"], "35%")
        self.assertTrue(resistance["stat"]["resolved"])
        self.assertEqual(resistance["stat"]["name"], "Physical Resistance")
        self.assertEqual(resistance["stat"]["unit"], "percent")

        # Relic primordial affix: only value 1 is overridden; sibling value 0 is source.
        relic_mods = {
            mod["mod_id"]: mod
            for mod in doc["mods"]
            if mod["item_instance_id"] == "equipment.relic"
            and mod["source"] == "affix"
            and (mod.get("affix") or {}).get("channel") == "primordial"
        }
        self.assertEqual(
            relic_mods["equipment.relic#affix#primordial#0.0"]["value_source"], "source"
        )
        self.assertEqual(
            relic_mods["equipment.relic#affix#primordial#0.1"]["value_source"],
            "local_value_override",
        )
        self.assertAlmostEqual(
            relic_mods["equipment.relic#affix#primordial#0.1"]["value_numeric"], 1.8, places=9
        )

    def test_export_is_deterministic_across_cli_runs(self) -> None:
        self.assertEqual(self.export_proc_2.returncode, 0, self.export_proc_2.stderr)
        first = (self.root / "calc-1.json").read_bytes()
        second = (self.root / "calc-2.json").read_bytes()
        self.assertEqual(first, second, "two exports of the same revision must be byte-identical")

    def test_delivered_build_data_was_not_touched_by_tests(self) -> None:
        # 2026-11-04: the user explicitly asked the Lead to apply the 17 panel values, so
        # the DELIVERED current revision is intentionally the local-input revision.  These
        # temp-root tests must not modify the delivered tree/files.
        pointer = json.loads((SOURCE_BUILD_DIR / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["revision_id"], CURRENT_REVISION)
        self.assertEqual(pointer["revision_seq"], 8)
        self.assertEqual(pointer["content_sha256"], CURRENT_CONTENT_SHA256)
        real_inputs = SOURCE_BUILD_DIR / "inputs" / "local-inputs.json"
        self.assertTrue(real_inputs.is_file(), "delivered build must keep the user inputs sidecar")
        from le_build import inputs as inputs_module

        document = json.loads(real_inputs.read_text(encoding="utf-8"))
        self.assertEqual(len(document["entries"]), 17)
        delivered = load_current_spec()
        meta_local = delivered["meta"]["local_inputs"]
        self.assertEqual(meta_local["entries"], 17)
        self.assertEqual(meta_local["applied"], 17)
        self.assertEqual(meta_local["sha256"], inputs_module.document_sha256(document))
        self.assertEqual(delivered["statuses"]["value_resolution"], "resolved")
        # The pinned source-only baseline is still readable and unmodified.
        baseline = load_revision_spec(BASELINE_REVISION)
        self.assertNotIn("local_inputs", baseline["meta"])
        self.assertEqual(baseline["statuses"]["value_resolution"], "partial")
        # Earlier historical revisions remain untouched/readable.
        for revision_id in ("rev-04a9b21cf2436df9", LEGACY_REVISION_ID):
            revision = load_revision_spec(revision_id)
            self.assertEqual(revision["meta"]["revision"]["revision_id"], revision_id)


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
