"""End-to-end CLI tests on a throw-away project root (P2-5).

Every command runs as a subprocess with ``PYTHONPATH=src`` and ``cwd=tools/build_import``
exactly like the documented entry point.  The project root is a temporary directory and
the catalog is passed with ``--db`` (absolute path, read-only), so the published
``data/builds`` tree is never touched: imports write under the temporary root.

Run from ``tools/build_import``::

    & $py -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from le_build import inputs as inputs_module

from _dataset_asset import require_dataset_db

REPO = Path(__file__).resolve().parents[3]
BUILD_IMPORT = REPO / "tools" / "build_import"
FIXTURE = REPO / "reports" / "discovery" / "phase-2-o3ZRm9my" / "build-response.json"
DB = REPO / "data" / "releases" / "phase1-dataset-003" / "data.sqlite"
BUILD_REF = "letools:o3ZRm9my"
SHARE_ID = "o3ZRm9my"
SOURCE_BUILD_DIR = REPO / "data" / "builds" / "letools" / SHARE_ID

# 2026-11-04: the delivered current revision is the user-synced local-input revision
# (seq 4, 17 explicit_value inputs applied).  Source-only expectations and any
# input-mutating tests are pinned to the baseline revision and seeded into temp roots,
# so the delivered inputs sidecar is never modified by tests.
BASELINE_REVISION = "rev-dbcf35ba60b0dd95"
BASELINE_CONTENT_SHA256 = (
    "dbcf35ba60b0dd956d4f08b0faf26b91e888f3762c1e67ec210bd2c89b17d354"
)
CURRENT_REVISION = "rev-c746411ccebee9c0"
CURRENT_CONTENT_SHA256 = (
    "c746411ccebee9c04b67da4a40cfc21d1e6770530e284bd2de8160ed0199932b"
)
CURRENT_INPUTS_DOC_SHA256 = (
    "5abdf254c77fc40656b382e10d1b510596f517a287a87668d8c0f52165606e48"
)
CURRENT_INPUTS_FILE_SHA256 = (
    "102a31f1c38b9c23bc24125e13c7476d32c5d950dcbaa629b934911552123fa3"
)
EXPECTED_CALC_MODS = 148  # 153 - 5 non-DPS drop-rate mods (re-review R1)
EXPECTED_CALC_MOD_MODS = 137
EXPECTED_CALC_UNIQUE_MODS = 48
EXPECTED_CALC_ARTIFACT_SIZE = 524731
EXPECTED_CALC_ARTIFACT_SHA256 = (
    "531f7a50ec338cf0765bf0008a05098f044e38f5e134420241f149ef4f9aa561"
)
EXPECTED_EXCLUDED_NON_DPS = 5
EXPECTED_CALC_DESCRIPTIONS = 11
EXPECTED_CALC_IMPLICIT_MODS = 28  # 33 - 5 drop-rate blessing implicits
DEFAULT_CURRENT_REVISION_SEQ = 8
# Historical read-only revisions that must stay readable after the 2026-11-04 re-import.
HISTORICAL_REVISIONS = (
    "rev-dbcf35ba60b0dd95",
    "rev-5e0e70bf7a26b3a5",
    "rev-b0f2782d178b73dd",
    "rev-04a9b21cf2436df9",
    "rev-8dc664d73737a8b9",
)



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _json_file(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _baseline_pointer() -> dict:
    pointer = _json_file(SOURCE_BUILD_DIR / "current.json")
    baseline = _json_file(SOURCE_BUILD_DIR / "revisions" / BASELINE_REVISION / "build.json")
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
    current_revision = _json_file(SOURCE_BUILD_DIR / "current.json")["revision_id"]
    if current_revision != BASELINE_REVISION:
        stale = target / "revisions" / current_revision
        if stale.is_dir():
            shutil.rmtree(stale)
    (target / "current.json").write_text(
        json.dumps(_baseline_pointer(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def _run_cli(*args: str, timeout: int = 240) -> subprocess.CompletedProcess:
    command = [sys.executable, "-m", "le_build", *args]
    env = os.environ.copy()
    # Isolate from any developer PYTHONPATH; only the package under test is importable.
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


def _json_stdout(proc: subprocess.CompletedProcess):
    return json.loads(proc.stdout)


class CliOfflineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="le_cli_test_")
        cls.root = Path(cls._tmp.name)
        require_dataset_db()
        cls.import_args = (
            "import",
            "--file",
            str(FIXTURE),
            "--share-id",
            SHARE_ID,
            "--offline",
            "--db",
            str(DB),
        )
        cls.first = _run_cli("--root", str(cls.root), *cls.import_args)
        cls.build_dir = cls.root / "data" / "builds" / "letools" / SHARE_ID
        cls.pointer_path = cls.build_dir / "current.json"
        cls.pointer_after_first = (
            cls.pointer_path.read_bytes() if cls.pointer_path.is_file() else b""
        )
        cls.revisions_after_first = cls._revision_ids()
        cls.first_build_bytes = None
        cls.first_revision_id = None
        if cls.revisions_after_first:
            cls.first_revision_id = cls.revisions_after_first[0]
            cls.first_build_bytes = (
                cls.build_dir / "revisions" / cls.first_revision_id / "build.json"
            ).read_bytes()
        cls.second = _run_cli("--root", str(cls.root), *cls.import_args)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    @classmethod
    def _revision_ids(cls) -> list[str]:
        revisions_dir = cls.build_dir / "revisions"
        if not revisions_dir.is_dir():
            return []
        return sorted(
            entry.name for entry in revisions_dir.iterdir() if entry.is_dir()
        )

    def _failure_reports(self, share_id: str) -> list[Path]:
        imports_dir = self.root / "data" / "builds" / "letools" / share_id / "imports"
        if not imports_dir.is_dir():
            return []
        return sorted(imports_dir.glob("*/failure-report.json"))

    # --------------------------------------------------------------- publishing
    def test_offline_import_publishes_a_revision(self) -> None:
        self.assertEqual(self.first.returncode, 0, self.first.stderr)
        self.assertEqual(self.first.stderr.strip(), "")
        summary = _json_stdout(self.first)
        self.assertEqual(summary["build_id"], BUILD_REF)
        self.assertEqual(summary["state"], "published")
        self.assertEqual(summary["validation"], "ok")
        self.assertEqual(summary["acceptance_failed"], [])
        self.assertRegex(summary["revision_id"], r"^rev-[0-9a-f]{16}$")
        self.assertEqual(summary["counts"]["item_instances"], 29)
        self.assertEqual(summary["counts"]["affix_instances"], 47)
        self.assertEqual(summary["counts"]["equipment_slots"], 11)
        self.assertEqual(summary["counts"]["idols"], 8)
        self.assertEqual(summary["counts"]["blessings"], 10)
        # The weaver tree/containers are excluded by user scope (2026-11-03).
        self.assertNotIn("weaver_containers", summary["counts"])
        self.assertNotIn("weaver_nodes", summary["counts"])
        self.assertEqual(
            summary["counts"]["affix_channels"],
            {"normal": 37, "sealed": 2, "primordial": 2, "corrupted": 6},
        )
        self.assertEqual(summary["counts"]["completed_quests"], 20)
        self.assertEqual(summary["statuses"]["identity_resolution"], "resolved")

        # Revision directory: build.json + the four generated reports.
        revision_dir = self.build_dir / "revisions" / summary["revision_id"]
        self.assertTrue(revision_dir.is_dir())
        for name in (
            "build.json",
            "import-report.json",
            "import-report.zh-CN.md",
            "calculation-gap-report.json",
            "calculation-gap-report.zh-CN.md",
        ):
            self.assertTrue((revision_dir / name).is_file(), name)
        report = json.loads((revision_dir / "import-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["validation"]["status"], "ok")
        self.assertEqual(report["validation"]["fail"], 0)
        self.assertEqual(report["validation"]["warn"], 0)
        self.assertGreaterEqual(len(report["acceptance"]), 20)
        self.assertTrue(all(check["status"] == "pass" for check in report["acceptance"]))
        self.assertEqual(report["counts"]["item_instances"], 29)
        report_counts = json.dumps(report["counts"], ensure_ascii=False).lower()
        self.assertNotIn("weaver", report_counts)
        self.assertNotIn("container", report_counts)

        # Raw response archive + fetch metadata + human-readable report mirror.
        raw_files = list((self.build_dir / "raw").glob("*.json"))
        self.assertEqual(len(raw_files), 1)
        self.assertEqual(
            raw_files[0].name,
            "3275ac08b5d9ba582b66e789783238f108e4ab5745a471f83bdbaf267d8d0af5.json",
        )
        self.assertTrue(list((self.build_dir / "fetches").glob("*.json")))
        mirror = self.root / "reports" / "phase-2" / SHARE_ID
        self.assertTrue((mirror / "import-report.json").is_file())
        self.assertTrue((mirror / "calculation-gap-report.json").is_file())

        pointer = json.loads(self.pointer_path.read_text(encoding="utf-8"))
        self.assertEqual(pointer["revision_id"], summary["revision_id"])
        self.assertEqual(pointer["revision_seq"], 1)
        self.assertEqual(pointer["dataset_id"], "phase1-dataset-003")
        self.assertEqual(pointer["build_id"], BUILD_REF)
        stored = json.loads((revision_dir / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["meta"]["revision"]["revision_id"], summary["revision_id"])
        self.assertEqual(stored["equipment"]["slot_order"][:3], ["head", "chest", "weapon1"])

    def test_second_identical_import_is_idempotent(self) -> None:
        self.assertEqual(self.second.returncode, 0, self.second.stderr)
        summary = _json_stdout(self.second)
        self.assertEqual(summary["state"], "exists")
        self.assertEqual(summary["revision_id"], self.first_revision_id)
        self.assertEqual(len(self.revisions_after_first), 1)
        # No second revision directory appeared and the pointer is byte-identical.
        self.assertEqual(self._revision_ids(), [self.first_revision_id])
        self.assertEqual(self.pointer_path.read_bytes(), self.pointer_after_first)
        build_path = self.build_dir / "revisions" / self.first_revision_id / "build.json"
        self.assertEqual(build_path.read_bytes(), self.first_build_bytes)

    # ------------------------------------------------------------ read commands
    def test_validate_offline_succeeds(self) -> None:
        proc = _run_cli(
            "--root", str(self.root), "validate", "--build", BUILD_REF, "--offline", "--db", str(DB)
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("validation: ok", proc.stdout)
        self.assertIn("warn=0", proc.stdout)
        self.assertIn("fail=0", proc.stdout)
        match = re.search(r"pass=(\d+)", proc.stdout)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(int(match.group(1)), 34)
        self.assertIn("coverage: items=", proc.stdout)
        # 29 in-scope item instances (11 equipment + 8 idols + 10 blessings) and the
        # unchanged 47 affix instances; the weaver tree is excluded by scope.
        self.assertIn("'total': 29", proc.stdout)
        self.assertIn("'total': 47", proc.stdout)
        self.assertNotIn("weaver", proc.stdout.lower())

    def test_show_reports_the_stored_build(self) -> None:
        proc = _run_cli("--root", str(self.root), "show", "--build", BUILD_REF)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["build_id"], BUILD_REF)
        self.assertEqual(summary["character"], {"level": 100, "class": "Mage", "mastery": "Sorcerer"})
        self.assertEqual(summary["counts"]["equipment"], 11)
        self.assertEqual(summary["counts"]["idols"], 8)
        self.assertEqual(summary["counts"]["blessings"], 10)
        self.assertNotIn("containers", summary["counts"])
        self.assertNotIn("weaver_nodes", summary["counts"])
        self.assertEqual(summary["counts"]["specializations"], 5)
        self.assertEqual(summary["counts"]["action_bar"], 5)
        self.assertEqual(summary["counts"]["passive_nodes"], 19)
        self.assertEqual(summary["storage"]["revision_id"], self.first_revision_id)
        self.assertIs(summary["storage"]["is_current"], True)
        self.assertEqual(len(summary["specializations"]), 5)
        self.assertEqual(summary["specializations"][0]["tree"], "Enchant Weapon")
        self.assertEqual(summary["specializations"][2]["tree"], "Flame Ward")
        self.assertIn("Teleport", [slot["name"] for slot in summary["action_bar"]])

    def test_explain_equipment_hands(self) -> None:
        proc = _run_cli(
            "--root", str(self.root), "explain", "--build", BUILD_REF,
            "--path", "equipment.hands", "--json",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        explained = json.loads(proc.stdout)
        self.assertTrue(explained["found"])
        self.assertEqual(explained["path"], "equipment.hands")
        self.assertEqual(explained["source_path"], "data.equipment.hands")
        instance = explained["value"]["instance"]
        self.assertEqual(instance["encoded_id"], "UAzDMBYHYE4g")
        self.assertEqual(instance["slot"], "hands")
        self.assertEqual(instance["definition"]["ref"]["name"], "Withstand the Elements")
        self.assertEqual(explained["raw_source"]["raw_sha256"],
                         "3275ac08b5d9ba582b66e789783238f108e4ab5745a471f83bdbaf267d8d0af5")
        self.assertTrue(explained["related_issues"])
        for issue in explained["related_issues"]:
            self.assertTrue(issue["path"].startswith("data.equipment.hands"), issue)

    def test_explain_unknown_path_fails_cleanly(self) -> None:
        proc = _run_cli(
            "--root", str(self.root), "explain", "--build", BUILD_REF, "--path", "equipment.wings"
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no equipment slot named", proc.stderr)

    # ------------------------------------------------------------ failure modes
    def test_version_mismatch_is_rejected_and_reported(self) -> None:
        inputs = self.root / "inputs"
        inputs.mkdir(exist_ok=True)
        document = json.loads(FIXTURE.read_text(encoding="utf-8"))
        document["data"]["dataVersion"] = "Version 1.4.0"
        document["data_version"] = "Version 1.4.0"
        payload = inputs / "build-response-1.4.0.json"
        payload.write_text(json.dumps(document), encoding="utf-8")

        share_id = "p2badver1"
        proc = _run_cli(
            "--root", str(self.root), "import", "--file", str(payload),
            "--share-id", share_id, "--offline", "--db", str(DB),
        )
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("incompatible source version", proc.stderr)
        reports = self._failure_reports(share_id)
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "incompatible")
        self.assertEqual(report["stages"]["schema"], "incompatible")
        self.assertEqual(report["version_compatibility"]["status"], "mismatched")
        build_dir = self.root / "data" / "builds" / "letools" / share_id
        self.assertFalse((build_dir / "current.json").is_file())
        revisions = build_dir / "revisions"
        self.assertFalse(revisions.is_dir() and list(revisions.iterdir()))

    def test_broken_json_is_a_fetch_failure_and_publishes_nothing(self) -> None:
        inputs = self.root / "inputs"
        inputs.mkdir(exist_ok=True)
        payload = inputs / "broken.json"
        payload.write_text("{not valid json", encoding="utf-8")
        share_id = "p2broken1"
        proc = _run_cli(
            "--root", str(self.root), "import", "--file", str(payload),
            "--share-id", share_id, "--offline", "--db", str(DB),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("fetch failed", proc.stderr)
        reports = self._failure_reports(share_id)
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "fetch_failed")
        self.assertEqual(report["stages"]["fetch"], "json_error")
        build_dir = self.root / "data" / "builds" / "letools" / share_id
        self.assertFalse((build_dir / "current.json").is_file())

    def test_invalid_share_url_is_an_argument_error(self) -> None:
        cases = [
            "https://evil.example/planner/o3ZRm9my",
            "https://www.lastepochtools.com/build/o3ZRm9my",
            "ftp://www.lastepochtools.com/planner/o3ZRm9my",
        ]
        for url in cases:
            proc = _run_cli("--root", str(self.root), "import", "--url", url, "--db", str(DB))
            self.assertEqual(proc.returncode, 2, f"{url}: {proc.stderr}")
            self.assertIn("error:", proc.stderr)
        self.assertFalse(
            (self.root / "data" / "builds" / "letools" / "o3ZRm9my" / "imports" / "bad-url").exists()
        )

    def test_missing_offline_flag_is_an_argument_error(self) -> None:
        proc = _run_cli(
            "--root", str(self.root), "import", "--file", str(FIXTURE),
            "--share-id", SHARE_ID, "--db", str(DB),
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("requires --offline", proc.stderr)

    def test_url_with_offline_flag_is_an_argument_error(self) -> None:
        proc = _run_cli(
            "--root", str(self.root), "import", "--url",
            "https://www.lastepochtools.com/planner/o3ZRm9my", "--offline", "--db", str(DB),
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("drop --offline", proc.stderr)


# ---------------------------------------------------------------------------
# Local manual inputs + sync (2026-11-03 per-variable interface)
# ---------------------------------------------------------------------------
# 2026-11-03 per-variable fix: 15 target groups / 17 displayed variable rows.
# Missing affix rolls now target only the 4 relic affixes; the hands sealed/primordial
# affixes are constant-1% definitions with no missing variable (not targets).
MISSING_AFFIX_PATHS = [
    "data.equipment.relic.affixes.1",
    "data.equipment.relic.affixes.2",
    "data.equipment.relic.affixes.3",
    "data.equipment.relic.primordialAffix",
]
OMNIS_ROLL_IDS = list(range(7))
UNSATED_ROLL_IDS = list(range(4))
EXPECTED_TARGET_GROUPS = 15
EXPECTED_VARIABLE_ROWS = 17
RELIC_PRIMORDIAL_VALUE_INDEX = 1


def _missing_value_inputs_document() -> dict:
    """One roll-based input per target group (15): 4 affix + 11 unique rollIds."""
    entries: list[dict] = []
    sequence = 0
    for path in MISSING_AFFIX_PATHS:
        sequence += 1
        entries.append(
            {
                "input_id": f"in-20261103T233000-{sequence:04d}",
                "kind": "affix_roll",
                "instance_path": path,
                "roll": 128 + sequence,
                "created_at": "2026-11-03T23:30:00+08:00",
            }
        )
    for roll_id in OMNIS_ROLL_IDS:
        sequence += 1
        entries.append(
            {
                "input_id": f"in-20261103T233000-{sequence:04d}",
                "kind": "unique_mod_roll",
                "instance_path": "data.equipment.amulet",
                "roll_id": roll_id,
                "roll": 140 + roll_id,
                "created_at": "2026-11-03T23:30:00+08:00",
            }
        )
    for roll_id in UNSATED_ROLL_IDS:
        sequence += 1
        entries.append(
            {
                "input_id": f"in-20261103T233000-{sequence:04d}",
                "kind": "unique_mod_roll",
                "instance_path": "data.equipment.ring2",
                "roll_id": roll_id,
                "roll": 160 + roll_id,
                "created_at": "2026-11-03T23:30:00+08:00",
            }
        )
    return {
        "version": "build-local-inputs-v1",
        "build_id": BUILD_REF,
        "updated_at": "2026-11-03T23:30:00+08:00",
        "entries": entries,
    }


def _explicit_variable_inputs_document() -> dict:
    """The 3 per-variable explicit overrides used by the docs/tests.

    Omnis rollId 0 drives two displayed variables (Level of Skills +2 flat,
    Physical Resistance 35% -> 0.35); the relic primordial affix value 1 is
    Spell Damage 180% -> 1.8.
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


class LocalInputSyncCliTest(unittest.TestCase):
    """`input` / `sync` / `import --ignore-local-inputs` in temp-root copies.

    Every test copies the delivered build tree into a temp root; the real
    ``data/builds`` tree is only read.  ``sync`` passes ``--db`` pointing at the real
    read-only catalog so the source payload is re-imported with the local inputs.
    """

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        require_dataset_db()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="le_cli_inputs_")
        self.root = Path(self._tmp.name)
        self.build_dir = seed_baseline_tree(self.root)
        target = self.build_dir
        self.inputs_path = target / "inputs" / "local-inputs.json"
        self.pointer_path = target / "current.json"
        self.source_pointer = json.loads(self.pointer_path.read_text(encoding="utf-8"))
        self.source_revision = self.source_pointer["revision_id"]
        self.source_pointer_bytes = self.pointer_path.read_bytes()
        self.source_build_bytes = (
            target / "revisions" / self.source_revision / "build.json"
        ).read_bytes()
        self.revisions_before = self.revision_ids()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---------------------------------------------------------------- helpers
    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return _run_cli("--root", str(self.root), *args)

    def write_inputs(self, document: dict, name: str = "inputs.json") -> Path:
        path = self.root / "tmp-inputs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def revision_ids(self) -> list[str]:
        return sorted(
            entry.name for entry in (self.build_dir / "revisions").iterdir() if entry.is_dir()
        )

    def load_pointer(self) -> dict:
        return json.loads(self.pointer_path.read_text(encoding="utf-8"))

    def load_build(self, revision_id: str | None = None) -> dict:
        revision_id = revision_id or self.load_pointer()["revision_id"]
        return json.loads(
            (self.build_dir / "revisions" / revision_id / "build.json").read_text(encoding="utf-8")
        )

    def slot_instance(self, spec: dict, slot: str) -> dict:
        for entry in spec["equipment"]["slots"]:
            if entry.get("slot") == slot:
                return entry.get("instance") or {}
        raise AssertionError(f"no slot {slot!r}")

    def assert_no_publish(self) -> None:
        self.assertEqual(self.pointer_path.read_bytes(), self.source_pointer_bytes)
        self.assertEqual(self.revision_ids(), self.revisions_before)

    # ------------------------------------------------------- full sync path
    def test_sync_with_all_missing_value_inputs_publishes_new_revision(self) -> None:
        path = self.write_inputs(_missing_value_inputs_document())
        proc = self.cli("input", "import", "--build", BUILD_REF, "--file", str(path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.load_pointer()["revision_id"], self.source_revision)

        proc = self.cli("sync", "--build", BUILD_REF, "--db", str(DB))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = _json_stdout(proc)
        self.assertEqual(summary["state"], "published")
        new_revision = summary["revision_id"]
        self.assertNotEqual(new_revision, self.source_revision)
        self.assertEqual(summary["revision_seq"], self.source_pointer["revision_seq"] + 1)

        # Raw payload identity is unchanged; only the local inputs changed the content.
        pointer = self.load_pointer()
        self.assertEqual(
            pointer["source_raw_sha256"], self.source_pointer["source_raw_sha256"]
        )
        spec = self.load_build(new_revision)
        self.assertEqual(
            spec["meta"]["source_payload"]["raw_sha256"],
            self.source_pointer["source_raw_sha256"],
        )

        # 15 target groups (one roll-based input each) applied and recorded in provenance.
        meta_local = spec["meta"]["local_inputs"]
        self.assertEqual(meta_local["version"], "build-local-inputs-v1")
        self.assertEqual(meta_local["entries"], EXPECTED_TARGET_GROUPS)
        self.assertEqual(meta_local["applied"], EXPECTED_TARGET_GROUPS)
        self.assertEqual(
            meta_local["sha256"],
            inputs_module.document_sha256(
                json.loads(self.inputs_path.read_text(encoding="utf-8"))
            ),
        )
        self.assertEqual(
            len(meta_local.get("inputs_manifest") or []), EXPECTED_TARGET_GROUPS
        )
        self.assertTrue(all(entry.get("applied") for entry in meta_local["inputs_manifest"]))
        # All missing source values were supplied, so value resolution is now resolved.
        self.assertEqual(spec["statuses"]["value_resolution"], "resolved")
        self.assertGreaterEqual(
            (spec.get("readable_summary") or {}).get("local_input_lines") or 0,
            EXPECTED_VARIABLE_ROWS,
        )

        # 4 missing-variable affixes are now local inputs and restore actual values.
        slots = spec["equipment"]["slots"]
        by_slot = {entry["slot"]: entry["instance"] for entry in slots}
        relic = by_slot["relic"]["affixes"]["normal"]
        relic_rolls = {entry["source_path"]: entry for entry in relic}
        for path_suffix in (
            "data.equipment.relic.affixes.1",
            "data.equipment.relic.affixes.2",
            "data.equipment.relic.affixes.3",
        ):
            entry = relic_rolls[path_suffix]
            self.assertEqual(entry["roll"]["status"], "local_input", path_suffix)
            self.assertEqual(entry["roll"]["value_source"], "local_input", path_suffix)
            for value in entry["value"]["values"]:
                self.assertEqual(value.get("value_source"), "local_input", path_suffix)
        primordial = by_slot["relic"]["affixes"]["primordial"][0]
        self.assertEqual(primordial["roll"]["status"], "local_input")
        self.assertEqual(primordial["roll"]["raw"], 132)  # 128 + sequence 4
        # The roll drives both displayed variables of the multi-variable affix.
        self.assertEqual(
            [value.get("value_source") for value in primordial["value"]["values"]],
            ["local_input", "local_input"],
        )
        # Readable affix lines for those inputs are actual, not range.
        relic_readable = [
            line
            for line in by_slot["relic"]["readable"]["lines"]
            if line["section"] == "affix_normal"
        ]
        self.assertEqual(len(relic_readable), 4)
        self.assertEqual(
            [line["value_status"] for line in relic_readable],
            ["actual", "actual", "actual", "actual"],
        )
        self.assertFalse(
            [
                line
                for line in by_slot["relic"]["readable"]["lines"]
                if line["value_status"] == "range"
            ]
        )

        # ur=[] uniques: Omnis/Unsated Rage rollIds supplied -> local inputs only.
        omnis = by_slot["amulet"]
        self.assertIn(
            omnis["unique_rolls"]["status"],
            ("local_inputs_only", "present_with_local_inputs"),
        )
        local_rolls = omnis["unique_rolls"].get("local_rolls") or {}
        if isinstance(local_rolls, dict):
            recorded_roll_ids = {str(key) for key in local_rolls}
        else:  # defensive: allow a list of {roll_id, roll, input_id}
            recorded_roll_ids = {str(entry.get("roll_id")) for entry in local_rolls}
        self.assertTrue(
            {str(roll_id) for roll_id in OMNIS_ROLL_IDS} <= recorded_roll_ids,
            f"local_rolls must record every supplied rollId, got {sorted(recorded_roll_ids)}",
        )
        for mod in omnis["unique_rolls"]["mods"]:
            if not mod.get("can_roll"):
                continue
            self.assertEqual(mod["value_source"], "local_input", mod["mod_index"])
            self.assertEqual(mod["roll_status"], "local_input", mod["mod_index"])
            self.assertIsNotNone(mod["raw_roll"], mod["mod_index"])
        omnis_unique_lines = [
            line for line in omnis["readable"]["lines"] if line["section"] == "unique_mod"
        ]
        self.assertTrue(omnis_unique_lines)
        self.assertTrue(
            all(line["value_status"] == "actual" for line in omnis_unique_lines),
            [line["value_status"] for line in omnis_unique_lines],
        )
        unsated = by_slot["ring2"]
        self.assertIn(
            unsated["unique_rolls"]["status"],
            ("local_inputs_only", "present_with_local_inputs"),
        )

        # Previous revision remains byte-identical and read-only.
        self.assertEqual(
            (self.build_dir / "revisions" / self.source_revision / "build.json").read_bytes(),
            self.source_build_bytes,
        )

    def test_second_sync_is_idempotent(self) -> None:
        path = self.write_inputs(_missing_value_inputs_document())
        self.assertEqual(
            self.cli("input", "import", "--build", BUILD_REF, "--file", str(path)).returncode, 0
        )
        first = self.cli("sync", "--build", BUILD_REF, "--db", str(DB))
        self.assertEqual(first.returncode, 0, first.stderr)
        first_revision = _json_stdout(first)["revision_id"]
        pointer_after_first = self.pointer_path.read_bytes()
        build_after_first = (
            self.build_dir / "revisions" / first_revision / "build.json"
        ).read_bytes()

        second = self.cli("sync", "--build", BUILD_REF, "--db", str(DB))
        self.assertEqual(second.returncode, 0, second.stderr)
        summary = _json_stdout(second)
        self.assertEqual(summary["state"], "exists")
        self.assertEqual(summary["revision_id"], first_revision)
        self.assertEqual(self.pointer_path.read_bytes(), pointer_after_first)
        self.assertEqual(
            (self.build_dir / "revisions" / first_revision / "build.json").read_bytes(),
            build_after_first,
        )

    def test_remove_input_then_sync_returns_to_source_state(self) -> None:
        # First establish the source-only baseline built by the CURRENT code (the
        # delivered revision predates the local-input fields, so a no-input rebuild has
        # a different content hash by design).
        baseline_proc = self.cli("sync", "--build", BUILD_REF, "--db", str(DB))
        self.assertEqual(baseline_proc.returncode, 0, baseline_proc.stderr)
        baseline_summary = _json_stdout(baseline_proc)
        baseline_revision = baseline_summary["revision_id"]
        baseline_content = self.load_pointer()["content_sha256"]
        baseline_spec = self.load_build(baseline_revision)
        self.assertNotIn("local_inputs", baseline_spec.get("meta") or {})

        # Add one affix input, sync, then remove it and sync back to the source values.
        path = self.write_inputs(
            {
                "version": "build-local-inputs-v1",
                "build_id": BUILD_REF,
                "updated_at": "2026-11-03T23:30:00+08:00",
                "entries": [
                    {
                        "input_id": "in-20261103T233000-9001",
                        "kind": "affix_roll",
                        "instance_path": "data.equipment.relic.affixes.1",
                        "roll": 200,
                        "created_at": "2026-11-03T23:30:00+08:00",
                    }
                ],
            }
        )
        self.assertEqual(
            self.cli("input", "import", "--build", BUILD_REF, "--file", str(path)).returncode, 0
        )
        first = self.cli("sync", "--build", BUILD_REF, "--db", str(DB))
        self.assertEqual(first.returncode, 0, first.stderr)
        with_inputs_revision = _json_stdout(first)["revision_id"]
        spec = self.load_build(with_inputs_revision)
        relic_entry = {
            entry["source_path"]: entry
            for entry in self.slot_instance(spec, "relic")["affixes"]["normal"]
        }["data.equipment.relic.affixes.1"]
        self.assertEqual(relic_entry["roll"]["status"], "local_input")

        remove = self.cli(
            "input", "remove", "--build", BUILD_REF, "--input-id", "in-20261103T233000-9001"
        )
        self.assertEqual(remove.returncode, 0, remove.stderr)
        second = self.cli("sync", "--build", BUILD_REF, "--db", str(DB))
        self.assertEqual(second.returncode, 0, second.stderr)
        pointer = self.load_pointer()
        # The source-only content is back (same content hash as the no-input baseline);
        # the with-inputs revision remains read-only.
        self.assertEqual(pointer["content_sha256"], baseline_content)
        current_spec = self.load_build(pointer["revision_id"])
        relic_entry = {
            entry["source_path"]: entry
            for entry in self.slot_instance(current_spec, "relic")["affixes"]["normal"]
        }["data.equipment.relic.affixes.1"]
        self.assertEqual(relic_entry["roll"]["status"], "absent")
        self.assertNotEqual(relic_entry["value"].get("value_source"), "local_input")
        self.assertIn(
            with_inputs_revision,
            self.revision_ids(),
            "the with-inputs revision must stay readable",
        )
        self.assertIn(baseline_revision, self.revision_ids())

    def test_invalid_sync_does_not_publish(self) -> None:
        # Corrupt the local inputs file directly, then run sync: it must fail cleanly
        # without publishing a revision.
        self.inputs_path.parent.mkdir(parents=True, exist_ok=True)
        self.inputs_path.write_text(
            '{"version": "build-local-inputs-v1", "build_id": "letools:o3ZRm9my", '
            '"entries": [{"kind": "affix_roll", "instance_path": '
            '"data.equipment.relic.affixes.1", "roll": 256}]}',
            encoding="utf-8",
        )
        pointer_before = self.pointer_path.read_bytes()
        revisions_before = self.revision_ids()
        proc = self.cli("sync", "--build", BUILD_REF, "--db", str(DB))
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("error", (proc.stderr or "").lower())
        self.assertNotIn("Traceback", proc.stderr or "")
        self.assertEqual(self.pointer_path.read_bytes(), pointer_before)
        self.assertEqual(self.revision_ids(), revisions_before)

    def test_explicit_value_bypasses_formula_and_formats_by_unit(self) -> None:
        path = self.write_inputs(_explicit_variable_inputs_document(), "explicit.json")
        self.assertEqual(
            self.cli("input", "import", "--build", BUILD_REF, "--file", str(path), "--replace").returncode, 0
        )
        # Keep only the Percent Physical Resistance override for this focused test.
        entries = json.loads(self.inputs_path.read_text(encoding="utf-8"))["entries"]
        for entry in entries:
            if not (
                entry.get("target") == "unique_mod"
                and entry.get("mod_index") == 1
                and entry.get("instance_path") == "data.equipment.amulet"
            ):
                self.cli("input", "remove", "--build", BUILD_REF, "--input-id", entry["input_id"])
        proc = self.cli("sync", "--build", BUILD_REF, "--db", str(DB))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        spec = self.load_build()
        amulet = self.slot_instance(spec, "amulet")
        mod = amulet["unique_rolls"]["mods"][1]
        self.assertEqual(mod["value_source"], "local_value_override")
        self.assertIsNone(mod["raw_roll"], "explicit_value must not fabricate a roll")
        restored = mod["restored"]
        self.assertAlmostEqual(restored["value_numeric"], 0.35, places=9)
        # 2026-11-03 fix: explicit ur=[] overrides resolve the stat and format by unit.
        self.assertEqual(restored["stat_name"], "Physical Resistance")
        self.assertIn("35%", restored["display_value"] or "")
        self.assertEqual(restored["value_source"], "local_value_override")
        line = next(
            line
            for line in amulet["readable"]["lines"]
            if (line.get("diagnostics") or {}).get("mod_index") == 1
        )
        self.assertEqual(line["text"], "+35% Physical Resistance")
        self.assertEqual(line["value_status"], "actual")
        self.assertEqual(line["diagnostics"]["value_source"], "local_value_override")
        self.assertTrue(line["diagnostics"]["input_id"])

    def test_sync_dry_run_does_not_publish(self) -> None:
        path = self.write_inputs(
            {
                "version": "build-local-inputs-v1",
                "build_id": BUILD_REF,
                "updated_at": "2026-11-03T23:30:00+08:00",
                "entries": [
                    {
                        "input_id": "in-20261103T233000-9301",
                        "kind": "affix_roll",
                        "instance_path": "data.equipment.relic.affixes.1",
                        "roll": 200,
                        "created_at": "2026-11-03T23:30:00+08:00",
                    }
                ],
            }
        )
        self.assertEqual(
            self.cli("input", "import", "--build", BUILD_REF, "--file", str(path)).returncode, 0
        )
        pointer_before = self.pointer_path.read_bytes()
        revisions_before = self.revision_ids()
        proc = self.cli("sync", "--build", BUILD_REF, "--db", str(DB), "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = _json_stdout(proc)
        self.assertIs(summary.get("dry_run"), True)
        self.assertIn("revision_preview", summary)
        self.assertEqual(self.pointer_path.read_bytes(), pointer_before)
        self.assertEqual(self.revision_ids(), revisions_before)

    def test_import_ignore_local_inputs_flag(self) -> None:
        # Baseline: a plain no-input import built by the current code (the delivered
        # revision predates the local-input fields, so this publishes a new revision).
        baseline = self.cli(
            "import", "--file", str(FIXTURE), "--share-id", SHARE_ID, "--offline", "--db", str(DB)
        )
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        baseline_summary = _json_stdout(baseline)
        baseline_revision = baseline_summary["revision_id"]
        pointer_after_baseline = self.pointer_path.read_bytes()

        path = self.write_inputs(
            {
                "version": "build-local-inputs-v1",
                "build_id": BUILD_REF,
                "updated_at": "2026-11-03T23:30:00+08:00",
                "entries": [
                    {
                        "input_id": "in-20261103T233000-9201",
                        "kind": "affix_roll",
                        "instance_path": "data.equipment.relic.affixes.1",
                        "roll": 200,
                        "created_at": "2026-11-03T23:30:00+08:00",
                    }
                ],
            }
        )
        self.assertEqual(
            self.cli("input", "import", "--build", BUILD_REF, "--file", str(path)).returncode, 0
        )
        # --ignore-local-inputs reproduces the source-only baseline (state=exists, the
        # pointer and the baseline build.json stay byte-identical).
        ignored = self.cli(
            "import",
            "--file",
            str(FIXTURE),
            "--share-id",
            SHARE_ID,
            "--offline",
            "--db",
            str(DB),
            "--ignore-local-inputs",
        )
        self.assertEqual(ignored.returncode, 0, ignored.stderr)
        self.assertEqual(_json_stdout(ignored)["state"], "exists")
        self.assertEqual(_json_stdout(ignored)["revision_id"], baseline_revision)
        self.assertEqual(self.pointer_path.read_bytes(), pointer_after_baseline)

        # A normal import applies the inputs and publishes a new revision.
        applied = self.cli(
            "import", "--file", str(FIXTURE), "--share-id", SHARE_ID, "--offline", "--db", str(DB)
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        summary = _json_stdout(applied)
        self.assertNotEqual(summary["revision_id"], baseline_revision)
        current = self.load_build()
        relic_entry = {
            entry["source_path"]: entry
            for entry in self.slot_instance(current, "relic")["affixes"]["normal"]
        }["data.equipment.relic.affixes.1"]
        self.assertEqual(relic_entry["roll"]["status"], "local_input")
        self.assertEqual(
            relic_entry["roll"]["value_source"], "local_input"
        )


    # --------------------------------------- display-value conversions (new)
    def test_display_value_conversions_flat_and_percent(self) -> None:
        cases = [
            (
                ("--target", "unique_mod", "--instance", "data.equipment.amulet", "--mod-index", "0", "--display-value", "2"),
                2.0,
                "flat",
            ),
            (
                ("--target", "unique_mod", "--instance", "data.equipment.amulet", "--mod-index", "1", "--display-value", "35"),
                0.35,
                "percent",
            ),
            (
                ("--target", "affix", "--instance", "data.equipment.relic.primordialAffix", "--affix-value-index", "1", "--display-value", "180"),
                1.8,
                "percent",
            ),
        ]
        for args, expected_numeric, expected_unit in cases:
            proc = self.cli(
                "input", "set", "--build", BUILD_REF, "--kind", "explicit_value", *args
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
        entries = {
            (entry.get("target"), entry.get("instance_path"), entry.get("mod_index"), entry.get("affix_value_index")): entry
            for entry in json.loads(self.inputs_path.read_text(encoding="utf-8"))["entries"]
        }
        level = entries[("unique_mod", "data.equipment.amulet", 0, None)]
        resistance = entries[("unique_mod", "data.equipment.amulet", 1, None)]
        spell = entries[("affix", "data.equipment.relic.primordialAffix", None, 1)]
        # auto unit: flat target stays as typed, percent target is /100.
        self.assertAlmostEqual(level["value_numeric"], 2.0, places=9)
        self.assertEqual(level["display_value"], 2.0)
        self.assertEqual(level["display_unit"], "flat")
        self.assertAlmostEqual(resistance["value_numeric"], 0.35, places=9)
        self.assertEqual(resistance["display_value"], 35.0)
        self.assertEqual(resistance["display_unit"], "percent")
        self.assertAlmostEqual(spell["value_numeric"], 1.8, places=9)
        self.assertEqual(spell["display_value"], 180.0)
        self.assertEqual(spell["display_unit"], "percent")
        self.assert_no_publish()

    def test_unit_override_conversions(self) -> None:
        # Flat target (Level of Skills, 1–2): 200 typed as percent -> normalized 2.0.
        flat_target = self.cli(
            "input", "set", "--build", BUILD_REF, "--kind", "explicit_value",
            "--target", "unique_mod", "--instance", "data.equipment.amulet",
            "--mod-index", "0", "--display-value", "200", "--unit", "percent",
        )
        self.assertEqual(flat_target.returncode, 0, flat_target.stderr)
        # Percent target (Physical Resistance, 1%–45%): 0.35 typed as flat -> 0.35.
        percent_target = self.cli(
            "input", "set", "--build", BUILD_REF, "--kind", "explicit_value",
            "--target", "unique_mod", "--instance", "data.equipment.amulet",
            "--mod-index", "1", "--display-value", "0.35", "--unit", "flat",
        )
        self.assertEqual(percent_target.returncode, 0, percent_target.stderr)
        entries = {
            entry["mod_index"]: entry
            for entry in json.loads(self.inputs_path.read_text(encoding="utf-8"))["entries"]
        }
        self.assertAlmostEqual(entries[0]["value_numeric"], 2.0, places=9)
        self.assertEqual(entries[0]["display_unit"], "percent")
        self.assertAlmostEqual(entries[1]["value_numeric"], 0.35, places=9)
        self.assertEqual(entries[1]["display_unit"], "flat")

    def test_display_value_bounds_and_malformed_rejected(self) -> None:
        out_of_range = [
            ("--mod-index", "3", "--display-value", "-5"),  # negative outside positive range
            ("--mod-index", "3", "--display-value", "90"),  # >45% on 1%–45%
            ("--mod-index", "0", "--display-value", "0.5"),  # <1 on 1–2 level
            ("--mod-index", "0", "--display-value", "3"),  # >2 on 1–2 level
        ]
        for args in out_of_range:
            proc = self.cli(
                "input", "set", "--build", BUILD_REF, "--kind", "explicit_value",
                "--target", "unique_mod", "--instance", "data.equipment.amulet", *args,
            )
            self.assertNotEqual(proc.returncode, 0, f"{args}: expected rejection")
            self.assertIn("outside the declared range", proc.stderr, f"{args}: {proc.stderr}")
        malformed = [
            ("--mod-index", "1", "--display-value", "abc"),
            ("--mod-index", "1", "--display-value", "35", "--unit", "bogus"),
        ]
        for args in malformed:
            proc = self.cli(
                "input", "set", "--build", BUILD_REF, "--kind", "explicit_value",
                "--target", "unique_mod", "--instance", "data.equipment.amulet", *args,
            )
            self.assertEqual(proc.returncode, 2, f"{args}: {proc.stderr}")
        self.assertFalse(self.inputs_path.is_file(), "rejected inputs must not be saved")
        self.assert_no_publish()

    def test_input_list_reports_15_targets_17_variables(self) -> None:
        proc = self.cli("input", "list", "--build", BUILD_REF, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = _json_stdout(proc)
        self.assertEqual(payload["unresolved_targets_total"], EXPECTED_TARGET_GROUPS)
        self.assertEqual(payload["pending_targets"], EXPECTED_TARGET_GROUPS)
        self.assertEqual(
            sum(len(target.get("variables") or []) for target in payload["targets"]),
            EXPECTED_VARIABLE_ROWS,
        )
        paths = {target["instance_path"] for target in payload["targets"]}
        self.assertNotIn("data.equipment.hands.sealedAffix", paths)
        self.assertNotIn("data.equipment.hands.primordialAffix", paths)
        human = self.cli("input", "list", "--build", BUILD_REF)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn(
            "pending: 15/15 target group(s), 17/17 variable row(s)", human.stdout
        )

    # ---------------------------------- multi-variable sync / export (new)
    def test_multi_variable_sync_readable_provenance_and_export(self) -> None:
        path = self.write_inputs(_explicit_variable_inputs_document(), "multi-var.json")
        proc = self.cli(
            "input", "import", "--build", BUILD_REF, "--file", str(path), "--replace"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sync = self.cli("sync", "--build", BUILD_REF, "--db", str(DB))
        self.assertEqual(sync.returncode, 0, sync.stderr)
        summary = _json_stdout(sync)
        self.assertEqual(summary["state"], "published")
        revision = summary["revision_id"]
        self.assertEqual(summary["revision_seq"], self.source_pointer["revision_seq"] + 1)
        spec = self.load_build(revision)
        report = json.loads(
            (self.build_dir / "revisions" / revision / "import-report.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {key: report["validation"][key] for key in ("status", "pass", "warn", "fail")},
            {"status": "ok", "pass": 44, "warn": 0, "fail": 0},
        )
        self.assertEqual(len(report["acceptance"]), 26)
        self.assertEqual(
            [check["check"] for check in report["acceptance"] if check["status"] != "pass"], []
        )

        amulet = self.slot_instance(spec, "amulet")
        self.assertEqual(amulet["unique_rolls"].get("local_value_overrides"), 2)
        level_mod, resistance_mod = amulet["unique_rolls"]["mods"][0], amulet["unique_rolls"]["mods"][1]
        self.assertEqual(level_mod["value_source"], "local_value_override")
        self.assertEqual(resistance_mod["value_source"], "local_value_override")
        self.assertEqual(level_mod["restored"]["stat_name"], "Level of Skills")
        self.assertEqual(level_mod["restored"]["display_value"], "2")
        self.assertEqual(resistance_mod["restored"]["stat_name"], "Physical Resistance")
        self.assertEqual(resistance_mod["restored"]["display_value"], "35%")
        for mod in (level_mod, resistance_mod):
            self.assertIsNone(mod["raw_roll"])
            self.assertTrue(mod["restored"]["input_id"])

        amulet_lines = {
            line["diagnostics"]["mod_index"]: line for line in amulet["readable"]["lines"]
            if (line.get("diagnostics") or {}).get("mod_index") in (0, 1)
        }
        self.assertEqual(amulet_lines[0]["text"], "+2 to Level of Skills")
        self.assertEqual(amulet_lines[1]["text"], "+35% Physical Resistance")
        for line in amulet_lines.values():
            self.assertEqual(line["value_status"], "actual")
            self.assertEqual(line["diagnostics"]["value_source"], "local_value_override")

        relic = self.slot_instance(spec, "relic")
        primordial = relic["affixes"]["primordial"][0]
        # Sibling provenance fix: the block-level source stays untouched and the
        # non-overridden +6 Level value must NOT be marked as a local override.
        self.assertIsNone((primordial.get("value") or {}).get("value_source"))
        self.assertEqual((primordial.get("value") or {}).get("local_value_overrides"), 1)
        values = primordial["value"]["values"]
        self.assertIsNone(values[0].get("value_source"))
        self.assertEqual(values[0]["value_numeric"], 6)
        self.assertEqual(values[1].get("value_source"), "local_value_override")
        self.assertAlmostEqual(values[1]["value_numeric"], 1.8, places=9)
        primordial_lines = [
            line for line in relic["readable"]["lines"] if line["section"] == "affix_primordial"
        ]
        self.assertEqual(len(primordial_lines), 2)
        self.assertIn("+6 to Level of", primordial_lines[0]["text"])
        self.assertIsNone(primordial_lines[0]["diagnostics"].get("value_source"))
        self.assertEqual(primordial_lines[1]["text"], "180% increased Spell Damage")
        self.assertEqual(primordial_lines[1]["value_status"], "actual")
        self.assertEqual(
            primordial_lines[1]["diagnostics"].get("value_source"), "local_value_override"
        )

        # Previous revision remains byte-identical; a second sync is idempotent.
        self.assertEqual(
            (self.build_dir / "revisions" / self.source_revision / "build.json").read_bytes(),
            self.source_build_bytes,
        )
        pointer_bytes = self.pointer_path.read_bytes()
        second = self.cli("sync", "--build", BUILD_REF, "--db", str(DB))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(_json_stdout(second)["state"], "exists")
        self.assertEqual(_json_stdout(second)["revision_id"], revision)
        self.assertEqual(self.pointer_path.read_bytes(), pointer_bytes)

        # Calculator export after the overrides: 13 targets / 14 variable rows.
        export_path = self.root / "calc-after.json"
        export = self.cli("export", "--build", BUILD_REF, "--out", str(export_path))
        self.assertEqual(export.returncode, 0, export.stderr)
        doc = json.loads(export_path.read_text(encoding="utf-8"))
        self.assertEqual(len(doc["unresolved_inputs"]), 13)
        self.assertEqual(
            sum(len(target.get("variables") or []) for target in doc["unresolved_inputs"]), 14
        )
        explicit_mods = {
            mod["mod_id"]: mod
            for mod in doc["mods"]
            if mod["item_instance_id"] == "equipment.amulet"
            and mod["source"] == "unique_mod"
            and (mod.get("unique") or {}).get("mod_index") in (0, 1)
        }
        level = explicit_mods["equipment.amulet#unique_mod#0"]
        resistance = explicit_mods["equipment.amulet#unique_mod#1"]
        self.assertTrue(level["stat"]["resolved"])
        self.assertEqual(level["stat"]["name"], "Level of Skills")
        self.assertEqual(level["stat"]["unit"], "flat")
        self.assertEqual(level["value_numeric"], 2.0)
        self.assertEqual(level["display_value"], "2")
        self.assertEqual(level["value_source"], "local_value_override")
        self.assertTrue(resistance["stat"]["resolved"])
        self.assertEqual(resistance["stat"]["name"], "Physical Resistance")
        self.assertEqual(resistance["stat"]["unit"], "percent")
        self.assertAlmostEqual(resistance["value_numeric"], 0.35, places=9)
        self.assertEqual(resistance["display_value"], "35%")
        relic_mods = {
            mod["mod_id"]: mod
            for mod in doc["mods"]
            if mod["item_instance_id"] == "equipment.relic"
            and mod["source"] == "affix"
            and (mod.get("affix") or {}).get("channel") == "primordial"
        }
        self.assertEqual(relic_mods["equipment.relic#affix#primordial#0.0"]["value_source"], "source")
        self.assertEqual(
            relic_mods["equipment.relic#affix#primordial#0.1"]["value_source"],
            "local_value_override",
        )
        # Deterministic export.
        export_path_2 = self.root / "calc-after-2.json"
        export_2 = self.cli("export", "--build", BUILD_REF, "--out", str(export_path_2))
        self.assertEqual(export_2.returncode, 0, export_2.stderr)
        self.assertEqual(export_path.read_bytes(), export_path_2.read_bytes())


class DeliveredBuildStateTest(unittest.TestCase):
    """Read-only assertions on the real delivered build (2026-11-04 user sync).

    The delivered ``current`` revision is the local-input revision (seq 4, 17 applied
    explicit_value entries).  The baseline source-only revision stays pinned and readable
    (15 target groups / 17 variable rows).  Nothing here writes ``data/builds``.
    """

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        require_dataset_db()

    def test_delivered_current_revision_records_local_inputs(self) -> None:
        pointer = _json_file(SOURCE_BUILD_DIR / "current.json")
        self.assertEqual(pointer["revision_id"], CURRENT_REVISION)
        self.assertEqual(pointer["revision_seq"], 8)
        self.assertEqual(pointer["content_sha256"], CURRENT_CONTENT_SHA256)
        current = _json_file(
            SOURCE_BUILD_DIR / "revisions" / CURRENT_REVISION / "build.json"
        )
        meta_local = current["meta"]["local_inputs"]
        self.assertEqual(meta_local["version"], "build-local-inputs-v1")
        self.assertEqual(meta_local["entries"], 17)
        self.assertEqual(meta_local["applied"], 17)
        self.assertEqual(len(meta_local["inputs_manifest"]), 17)
        self.assertTrue(all(entry.get("applied") for entry in meta_local["inputs_manifest"]))
        inputs_path = SOURCE_BUILD_DIR / "inputs" / "local-inputs.json"
        self.assertTrue(inputs_path.is_file())
        self.assertEqual(
            hashlib.sha256(inputs_path.read_bytes()).hexdigest(),
            CURRENT_INPUTS_FILE_SHA256,
        )
        document = _json_file(inputs_path)
        self.assertEqual(len(document["entries"]), 17)
        from le_build import inputs as inputs_module

        self.assertEqual(
            meta_local["sha256"], inputs_module.document_sha256(document)
        )
        self.assertEqual(meta_local["sha256"], CURRENT_INPUTS_DOC_SHA256)
        # Synced build: fully resolved, 17 manual readable lines, 0 pending targets.
        self.assertEqual(current["statuses"]["value_resolution"], "resolved")
        self.assertEqual(current["statuses"]["text_readiness"], "complete")
        self.assertEqual(current["statuses"]["readable_completeness"], {"complete": 29})
        self.assertEqual(current["readable_summary"]["local_input_lines"], 17)
        self.assertEqual(inputs_module.describe_targets(current), [])
        proc = _run_cli("--root", str(REPO), "input", "list", "--build", BUILD_REF, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["entries"], 17)
        self.assertEqual(payload["pending_targets"], 0)
        self.assertEqual(payload["unresolved_targets_total"], 0)
        proc = _run_cli("--root", str(REPO), "validate", "--build", BUILD_REF, "--offline")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("pass=44", combined)
        self.assertIn("warn=0", combined)
        self.assertIn("fail=0", combined)

    def test_delivered_calculation_artifact_matches_current_revision(self) -> None:
        from le_build.calculation import build_calculation_input

        current = _json_file(
            SOURCE_BUILD_DIR / "revisions" / CURRENT_REVISION / "build.json"
        )
        artifact = REPO / "reports" / "phase-2" / "o3ZRm9my" / "calculation-input.json"
        artifact_bytes = artifact.read_bytes()
        self.assertEqual(len(artifact_bytes), EXPECTED_CALC_ARTIFACT_SIZE)
        self.assertEqual(
            hashlib.sha256(artifact_bytes).hexdigest(),
            EXPECTED_CALC_ARTIFACT_SHA256,
        )
        stored = json.loads(artifact_bytes.decode("utf-8"))
        self.assertEqual(stored["revision_id"], CURRENT_REVISION)
        self.assertEqual(len(stored["unresolved_inputs"]), 0)
        # Re-review R1: 5 drop-rate/non-DPS mods are excluded (148 = 137 mod + 11 description).
        self.assertEqual(len(stored["mods"]), EXPECTED_CALC_MODS)
        self.assertEqual(
            sum(1 for mod in stored["mods"] if mod["kind"] == "mod"),
            EXPECTED_CALC_MOD_MODS,
        )
        self.assertEqual(
            sum(1 for mod in stored["mods"] if mod["kind"] == "description"),
            EXPECTED_CALC_DESCRIPTIONS,
        )
        self.assertEqual(
            sum(1 for mod in stored["mods"] if mod["source"] == "implicit"),
            EXPECTED_CALC_IMPLICIT_MODS,
        )
        self.assertEqual(
            sum(
                1
                for mod in stored["mods"]
                if mod["source"] == "unique_mod" and mod["kind"] == "mod"
            ),
            EXPECTED_CALC_UNIQUE_MODS,
        )
        self.assertEqual(stored["excluded_non_dps"], EXPECTED_EXCLUDED_NON_DPS)
        self.assertEqual(stored["excluded_non_dps_properties"], [104])
        self.assertEqual(
            sorted(stored["excluded_non_dps_mod_ids"]),
            [
                "blessings.1#implicit#0",
                "blessings.2#implicit#0",
                "blessings.4#implicit#0",
                "blessings.6#implicit#0",
                "blessings.8#implicit#0",
            ],
        )
        self.assertFalse(
            [
                mod
                for mod in stored["mods"]
                if mod.get("property") == 104
                or str((mod.get("stat") or {}).get("stat_id") or "").endswith(
                    "propertyList:104"
                )
            ],
            "drop-rate mods must be filtered from the calculator export",
        )
        # The committed artifact is the real export of the delivered current revision.
        self.assertEqual(stored, build_calculation_input(current))
        # CLI export for current is byte-identical to the committed artifact.
        with tempfile.TemporaryDirectory(prefix="le_cli_artifact_") as tmp:
            out = Path(tmp) / "calc.json"
            proc = _run_cli(
                "--root", str(REPO), "export", "--build", BUILD_REF, "--out", str(out)
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(out.read_bytes(), artifact_bytes)

    def test_baseline_revision_remains_source_only(self) -> None:
        from le_build.calculation import unresolved_inputs
        from le_build.inputs import describe_targets

        baseline = _json_file(
            SOURCE_BUILD_DIR / "revisions" / BASELINE_REVISION / "build.json"
        )
        self.assertNotIn("local_inputs", baseline.get("meta") or {})
        self.assertEqual(baseline["meta"]["revision"]["revision_seq"], 5)
        self.assertEqual(
            baseline["meta"]["revision"]["content_sha256"], BASELINE_CONTENT_SHA256
        )
        self.assertEqual(baseline["statuses"]["value_resolution"], "partial")
        self.assertEqual(
            baseline["statuses"]["readable_completeness"], {"complete": 26, "partial": 3}
        )
        self.assertIsNone(baseline["readable_summary"].get("local_input_lines"))
        targets = describe_targets(baseline)
        self.assertEqual(len(targets), 15)
        self.assertEqual(
            sum(len(target.get("variables") or []) for target in targets), 17
        )
        unresolved = unresolved_inputs(baseline)
        self.assertEqual(len(unresolved), 15)
        self.assertEqual(
            sum(len(entry.get("variables") or []) for entry in unresolved), 17
        )
        with tempfile.TemporaryDirectory(prefix="le_cli_baseline_") as tmp:
            out = Path(tmp) / "baseline.json"
            proc = _run_cli(
                "--root",
                str(REPO),
                "export",
                "--build",
                BUILD_REF,
                "--revision",
                BASELINE_REVISION,
                "--out",
                str(out),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            exported = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(exported["revision_id"], BASELINE_REVISION)
            self.assertEqual(len(exported["unresolved_inputs"]), 15)
            self.assertEqual(
                sum(len(entry.get("variables") or []) for entry in exported["unresolved_inputs"]),
                17,
            )

    def test_current_pointer_guard_and_historical_readability(self) -> None:
        """Read-only guard: current must be the user-synced rev-c746, old revisions readable.

        Lead note (2026-11-04): an idempotent re-publish of older same-content content can
        repoint ``current.json``; tests must use explicit revisions and never run
        import/sync in the real root (temp roots only).
        """
        pointer = _json_file(SOURCE_BUILD_DIR / "current.json")
        self.assertEqual(pointer["revision_id"], CURRENT_REVISION)
        self.assertEqual(pointer["revision_seq"], DEFAULT_CURRENT_REVISION_SEQ)
        self.assertEqual(pointer["content_sha256"], CURRENT_CONTENT_SHA256)
        self.assertEqual(pointer["previous_revision_id"], "rev-28c4b4efac800bed")
        for revision_id in HISTORICAL_REVISIONS:
            revision = _json_file(
                SOURCE_BUILD_DIR / "revisions" / revision_id / "build.json"
            )
            self.assertEqual(revision["meta"]["revision"]["revision_id"], revision_id)
        # Old current (rev-5e0e) keeps its 17 user inputs; old baseline (b0f2) stays source-only.
        old_current = _json_file(
            SOURCE_BUILD_DIR / "revisions" / "rev-5e0e70bf7a26b3a5" / "build.json"
        )
        self.assertEqual(old_current["meta"]["local_inputs"]["entries"], 17)
        old_baseline = _json_file(
            SOURCE_BUILD_DIR / "revisions" / "rev-b0f2782d178b73dd" / "build.json"
        )
        self.assertNotIn("local_inputs", old_baseline["meta"])

    def test_historical_revision_still_validates_warn_exit_3(self) -> None:
        proc = _run_cli(
            "--root",
            str(REPO),
            "validate",
            "--build",
            BUILD_REF,
            "--revision",
            "rev-8dc664d73737a8b9",
            "--offline",
        )
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("warn", combined)
        self.assertIn("pass=35", combined)
        self.assertIn("warn=9", combined)
        self.assertIn("fail=0", combined)



if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
