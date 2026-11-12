"""Tests for the frozen-input adapter (``le_calc.inputs``).

Covers: no-argument ``load_bundle()`` contract, revision pinning to the
calculator export (not the moving ``current.json`` pointer), fingerprint chain,
dataset consistency, calculator-export completeness (incl. the re-review
``excluded_non_dps`` fields), and fatal mismatch detection on a temporary root
(never writes the real ``data/`` tree).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from le_calc.catalog import REPO_ROOT, file_sha256
from le_calc.inputs import (
    EXPECTED_ITEM_COUNT,
    EXPECTED_MOD_COUNT,
    EXPECTED_PASSIVE_NODES,
    InputBundle,
    InputError,
    load_bundle,
)

from _dataset_asset import require_dataset_db

BUILD_DIR = Path("data") / "builds" / "letools" / "o3ZRm9my"
CALC_INPUT = Path("reports") / "phase-2" / "o3ZRm9my" / "calculation-input.json"
REVISION_ID = "rev-c746411ccebee9c0"
REVISION_SEQ = 8
PREVIOUS_REVISION_ID = "rev-28c4b4efac800bed"
CONTENT_SHA = "c746411ccebee9c04b67da4a40cfc21d1e6770530e284bd2de8160ed0199932b"
CALC_SHA = "531f7a50ec338cf0765bf0008a05098f044e38f5e134420241f149ef4f9aa561"
BUILD_JSON_SHA = "2eeb744544d338ee2691cc1b9b3d12bb4505ace22d66de8242489d8c9c5ef0a2"
DATABASE_SHA = "afa79e9fda0023b2ed09f4cbe8e07da436e0c74c03f34f80772da471c0adb8ab"
DATASET_LOGICAL_SHA = "979d18204631746e183dd533d647a8e7e8138ac96ded3b0f19324669b9f80527"
EXPECTED_DESCRIPTION_COUNT = 11
EXPECTED_EXCLUDED_NON_DPS = 5



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _copy_temp_root(tmp: Path) -> Path:
    """Build a temporary root that reuses the real (read-only) database."""
    require_dataset_db()
    root = tmp
    real_current = json.loads((REPO_ROOT / "data" / "current.json").read_text(encoding="utf-8"))
    database = (REPO_ROOT / "data" / "releases" / "phase1-dataset-003" / "data.sqlite").resolve()
    manifest = (REPO_ROOT / "data" / "releases" / "phase1-dataset-003" / "manifest.json").resolve()
    dataset_current = dict(real_current)
    dataset_current["database"] = str(database)
    dataset_current["manifest"] = str(manifest)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "current.json").write_text(json.dumps(dataset_current), encoding="utf-8")
    build_dir = root / BUILD_DIR
    (build_dir / "revisions" / REVISION_ID).mkdir(parents=True)
    for name in ("current.json",):
        shutil.copy2(REPO_ROOT / BUILD_DIR / name, build_dir / name)
    shutil.copy2(
        REPO_ROOT / BUILD_DIR / "revisions" / REVISION_ID / "build.json",
        build_dir / "revisions" / REVISION_ID / "build.json",
    )
    calc_path = root / CALC_INPUT
    calc_path.parent.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / CALC_INPUT, calc_path)
    return root


class FrozenInputBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_dataset_db()
        cls.bundle = load_bundle(verify_database_file=False)

    def test_no_argument_contract_loads_expected_fixpoint(self) -> None:
        self.assertIsInstance(self.bundle, InputBundle)
        # the revision pin comes from the calculation export, not current.json
        self.assertEqual(self.bundle.calculation_input["revision_id"], REVISION_ID)
        self.assertEqual(self.bundle.current["revision_id"], REVISION_ID)
        self.assertEqual(self.bundle.calculation_input["content_sha256"], CONTENT_SHA)
        self.assertEqual(self.bundle.calculation_input["revision_seq"], REVISION_SEQ)
        self.assertEqual(self.bundle.calculation_input["schema_version"], "calculation-input-v1")
        self.assertEqual(self.bundle.calculation_input["game_version"], "1.5.2")
        self.assertEqual(self.bundle.dataset_current["dataset_id"], "phase1-dataset-003")
        self.assertFalse(self.bundle.fingerprints["current_pointer_drift"])
        self.assertEqual(self.bundle.fingerprints["revision_id_source"], "calculation-input")

    def test_calculator_export_completeness(self) -> None:
        self.assertEqual(len(self.bundle.items), EXPECTED_ITEM_COUNT)
        self.assertEqual(len(self.bundle.mods), EXPECTED_MOD_COUNT)
        item_mod_count = sum(len(item.get("mods") or []) for item in self.bundle.items)
        self.assertEqual(item_mod_count, EXPECTED_MOD_COUNT)
        self.assertEqual(len({mod["mod_id"] for mod in self.bundle.mods}), EXPECTED_MOD_COUNT)
        kinds = [mod.get("kind") for mod in self.bundle.mods]
        self.assertEqual(kinds.count("mod"), EXPECTED_MOD_COUNT - EXPECTED_DESCRIPTION_COUNT)
        self.assertEqual(kinds.count("description"), EXPECTED_DESCRIPTION_COUNT)
        self.assertEqual(self.bundle.calculation_input["unresolved_inputs"], [])
        # re-review export: out-of-DPS rows are listed separately, not emitted
        self.assertEqual(
            self.bundle.calculation_input["excluded_non_dps"], EXPECTED_EXCLUDED_NON_DPS
        )
        excluded_ids = self.bundle.calculation_input["excluded_non_dps_mod_ids"]
        self.assertEqual(len(excluded_ids), EXPECTED_EXCLUDED_NON_DPS)
        self.assertFalse(set(excluded_ids) & {mod["mod_id"] for mod in self.bundle.mods})
        self.assertEqual(
            self.bundle.calculation_input["excluded_non_dps_properties"], [104]
        )
        self.assertEqual(len(self.bundle.passive_nodes), EXPECTED_PASSIVE_NODES)
        self.assertEqual(self.bundle.passive_tree["points_total"], 113)
        self.assertEqual(len(self.bundle.specializations), 5)
        bh2 = self.bundle.specialization_for_tree("bh2")
        self.assertIsNotNone(bh2)
        self.assertEqual(bh2["points_total"], 36)

    def test_sha256_fingerprints(self) -> None:
        fingerprints = self.bundle.fingerprints
        self.assertEqual(fingerprints["calc_input_sha256"], CALC_SHA)
        self.assertEqual(fingerprints["build_json_sha256"], BUILD_JSON_SHA)
        self.assertEqual(fingerprints["content_sha256"], CONTENT_SHA)
        self.assertEqual(fingerprints["revision_id"], REVISION_ID)
        self.assertEqual(fingerprints["revision_seq"], REVISION_SEQ)
        self.assertEqual(fingerprints["database_sha256"], DATABASE_SHA)
        self.assertEqual(fingerprints["dataset_logical_content_sha256"], DATASET_LOGICAL_SHA)
        self.assertEqual(
            fingerprints["source_raw_sha256"],
            "3275ac08b5d9ba582b66e789783238f108e4ab5745a471f83bdbaf267d8d0af5",
        )
        self.assertEqual(fingerprints["excluded_non_dps_count"], EXPECTED_EXCLUDED_NON_DPS)
        # fingerprints really are the file hashes
        self.assertEqual(file_sha256(self.bundle.calc_input_path), CALC_SHA)
        self.assertEqual(file_sha256(self.bundle.revision_dir / "build.json"), BUILD_JSON_SHA)
        self.assertEqual(file_sha256(self.bundle.build_dir / "current.json"), fingerprints["current_json_sha256"])

    def test_all_contract_checks_pass(self) -> None:
        failures = [check for check in self.bundle.checks if check["status"] == "fail"]
        self.assertEqual(failures, [])
        info = [check for check in self.bundle.checks if check["status"] == "info"]
        self.assertEqual(info, [])
        names = {check["check"] for check in self.bundle.checks}
        for expected in (
            "revision_pin_source",
            "calc_input_schema",
            "build_meta_revision_id",
            "build_meta_content_sha256",
            "revision_dir_matches_content_hash",
            "dataset_logical_sha256_chain",
            "dataset_file_sha256_chain",
            "mod_count",
            "excluded_non_dps_count",
            "excluded_non_dps_not_emitted",
            "passive_node_count",
            "bh2_specialization_present",
            "build_current_pointer_matches_calc",
        ):
            self.assertIn(expected, names)

    def test_build_meta_shape_matches_report_contract(self) -> None:
        meta = self.bundle.build_meta()
        for key in ("build", "dataset", "calc_input"):
            self.assertIn(key, meta)
            self.assertIsInstance(meta[key], dict)
        self.assertEqual(meta["build"]["revision_id"], REVISION_ID)
        self.assertEqual(meta["build"]["content_sha256"], CONTENT_SHA)
        self.assertEqual(meta["build"]["revision_seq"], REVISION_SEQ)
        self.assertEqual(meta["build"]["previous_revision_id"], PREVIOUS_REVISION_ID)
        self.assertEqual(meta["dataset"]["dataset_id"], "phase1-dataset-003")
        self.assertEqual(meta["dataset"]["target_game_version"], "1.5.2")
        self.assertEqual(meta["dataset"]["logical_content_sha256"], DATASET_LOGICAL_SHA)
        self.assertEqual(meta["calc_input"]["mod_count"], EXPECTED_MOD_COUNT)
        self.assertEqual(meta["calc_input"]["unresolved_inputs"], 0)
        self.assertEqual(meta["calc_input"]["sha256"], CALC_SHA)
        self.assertEqual(
            meta["revision_selection"],
            {
                "pinned_from": "calculation-input.revision_id",
                "revision_id": REVISION_ID,
                "current_pointer_revision_id": REVISION_ID,
                "current_pointer_drift": False,
            },
        )
        # flat fingerprint keys read by le_calc.report
        self.assertEqual(meta["build_spec_sha256"], BUILD_JSON_SHA)
        self.assertEqual(meta["calc_input_sha256"], CALC_SHA)
        self.assertEqual(meta["database_sha256"], DATABASE_SHA)
        self.assertEqual(meta["dataset_logical_content_sha256"], DATASET_LOGICAL_SHA)
        # the export/revision timestamps agree for this delivery
        self.assertEqual(
            meta["calc_input"]["generated_at"], "2026-11-04T14:37:59.117920+00:00"
        )
        self.assertEqual(meta["revision_published_at"], "2026-11-04T14:37:59.117920+00:00")

    def test_database_file_hash_is_verified(self) -> None:
        verified = load_bundle()  # default verifies the 263 MB SQLite file
        self.assertEqual(verified.fingerprints["database_sha256"], DATABASE_SHA)

    def test_to_dict_is_json_safe_and_has_no_volatile_timestamp(self) -> None:
        payload = json.loads(json.dumps(self.bundle.to_dict()))
        self.assertEqual(payload["counts"]["mods"], EXPECTED_MOD_COUNT)
        self.assertEqual(payload["fingerprints"]["revision_id"], REVISION_ID)
        self.assertNotIn("loaded_at", payload)
        self.assertNotIn("now", payload)


class MismatchedInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="le_calc_inputs_")
        self.root = _copy_temp_root(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _load(self) -> InputBundle:
        return load_bundle(root=self.root, verify_database_file=False)

    def _mutate_json(self, relative: Path, mutate) -> None:
        path = self.root / relative
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_temp_root_still_loads(self) -> None:
        bundle = self._load()
        self.assertEqual(bundle.fingerprints["calc_input_sha256"], CALC_SHA)
        self.assertFalse(bundle.fingerprints["current_pointer_drift"])

    def test_wrong_calc_schema_is_fatal(self) -> None:
        self._mutate_json(
            CALC_INPUT, lambda data: data.__setitem__("schema_version", "calculation-input-v2")
        )
        with self.assertRaisesRegex(InputError, "calc_input_schema"):
            self._load()

    def test_content_hash_mismatch_is_fatal(self) -> None:
        self._mutate_json(CALC_INPUT, lambda data: data.__setitem__("content_sha256", "0" * 64))
        with self.assertRaisesRegex(InputError, "build_meta_content_sha256"):
            self._load()

    def test_missing_pinned_revision_is_fatal(self) -> None:
        self._mutate_json(
            CALC_INPUT, lambda data: data.__setitem__("revision_id", "rev-deadbeefdeadbeef")
        )
        with self.assertRaisesRegex(InputError, "published revision build.json"):
            self._load()

    def test_current_pointer_drift_is_non_fatal(self) -> None:
        """A concurrent workflow upgrading current.json must not break a pinned run."""
        new_revision = "rev-aaaaaaaaaaaaaaaa"

        def mutate_current(data: dict) -> None:
            data["revision_id"] = new_revision
            data["content_sha256"] = "a" * 64
            data["revision_seq"] = 99

        self._mutate_json(BUILD_DIR / "current.json", mutate_current)
        bundle = self._load()
        # revision still comes from the calculation export
        self.assertEqual(bundle.fingerprints["revision_id"], REVISION_ID)
        self.assertEqual(bundle.fingerprints["content_sha256"], CONTENT_SHA)
        self.assertTrue(bundle.fingerprints["current_pointer_drift"])
        self.assertEqual(bundle.fingerprints["current_pointer_revision_id"], new_revision)
        drift_checks = [
            check for check in bundle.checks if check["check"] == "build_current_pointer_matches_calc"
        ]
        self.assertEqual(len(drift_checks), 1)
        self.assertEqual(drift_checks[0]["status"], "info")
        meta = bundle.build_meta()
        self.assertEqual(meta["build"]["revision_id"], REVISION_ID)
        self.assertTrue(meta["revision_selection"]["current_pointer_drift"])

    def test_upgraded_export_with_stale_pointer_still_loads(self) -> None:
        """The calc export can advance to a new revision before current.json is updated."""
        new_revision = "rev-bbbbbbbbbbbbbbbb"
        new_content = "b" * 64
        new_dir = self.root / BUILD_DIR / "revisions" / new_revision
        new_dir.mkdir(parents=True)

        def mutate_build(data: dict) -> None:
            data["meta"]["revision"]["revision_id"] = new_revision
            data["meta"]["revision"]["content_sha256"] = new_content
            data["meta"]["revision"]["revision_seq"] = 7

        build_data = json.loads(
            (self.root / BUILD_DIR / "revisions" / REVISION_ID / "build.json").read_text(
                encoding="utf-8"
            )
        )
        mutate_build(build_data)
        (new_dir / "build.json").write_text(json.dumps(build_data), encoding="utf-8")

        def mutate_calc(data: dict) -> None:
            data["revision_id"] = new_revision
            data["content_sha256"] = new_content
            data["revision_seq"] = 7

        self._mutate_json(CALC_INPUT, mutate_calc)
        bundle = self._load()
        self.assertEqual(bundle.fingerprints["revision_id"], new_revision)
        self.assertEqual(bundle.fingerprints["content_sha256"], new_content)
        self.assertTrue(bundle.fingerprints["current_pointer_drift"])
        self.assertEqual(bundle.build_meta()["build"]["revision_seq"], 7)

    def test_unresolved_inputs_are_fatal(self) -> None:
        self._mutate_json(
            CALC_INPUT,
            lambda data: data.__setitem__(
                "unresolved_inputs", [{"kind": "affix_roll", "instance_path": "x"}]
            ),
        )
        with self.assertRaisesRegex(InputError, "unresolved_inputs_empty"):
            self._load()

    def test_missing_mod_is_fatal(self) -> None:
        self._mutate_json(CALC_INPUT, lambda data: data["mods"].pop())
        with self.assertRaisesRegex(InputError, "mod_count"):
            self._load()

    def test_excluded_non_dps_rows_must_not_be_emitted(self) -> None:
        def mutate(data: dict) -> None:
            data["excluded_non_dps_mod_ids"] = list(data["excluded_non_dps_mod_ids"]) + [
                data["mods"][0]["mod_id"]
            ]
            data["excluded_non_dps"] = len(data["excluded_non_dps_mod_ids"])

        self._mutate_json(CALC_INPUT, mutate)
        with self.assertRaisesRegex(InputError, "excluded_non_dps_not_emitted"):
            self._load()


if __name__ == "__main__":
    unittest.main()
