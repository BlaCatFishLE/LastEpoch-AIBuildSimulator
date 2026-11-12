"""Local manual input interface tests (``build-local-inputs-v1``, 2026-11-03).

Covers the local-input document lifecycle and validation rules from
``docs/phase-2-local-input-and-calc-export.zh-CN.md`` §1/§2/§5:

* ``input set`` / ``input import`` / ``input export`` / ``input remove`` / ``input list``
  on a **throw-away project root** (a copy of the delivered ``data/builds/letools/
  o3ZRm9my`` tree, so the real published data is never touched);
* all four ``kind`` values incl. ``roll=0`` (a valid roll, never "missing");
* rejection (non-zero exit, clear error, no publish) of unknown kind, missing locator,
  unknown instance_path / roll_id / mod_index / implicit index, duplicate targets,
  out-of-range or non-integer rolls, non-finite ``explicit_value`` and legacy/旧范围
  paths;
* roundtrip: imported inputs can be exported and re-imported.

The tests are written against the contract command names and file layout. They are
expected to be red until the local-input implementation lands; the command surface is
asserted by exit code + files + revisions rather than by internal function names.

Run from ``tools/build_import``::

    $env:PYTHONPATH='src'; & $py -m unittest tests.test_inputs -v
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

from _dataset_asset import require_dataset_db

REPO = Path(__file__).resolve().parents[3]
BUILD_IMPORT = REPO / "tools" / "build_import"
DB = REPO / "data" / "releases" / "phase1-dataset-003" / "data.sqlite"
FIXTURE = REPO / "reports" / "discovery" / "phase-2-o3ZRm9my" / "build-response.json"
BUILD_REF = "letools:o3ZRm9my"
SHARE_ID = "o3ZRm9my"
SOURCE_BUILD_DIR = REPO / "data" / "builds" / "letools" / SHARE_ID

# 2026-11-03 per-variable fix: the input interface is variable-level.
# 15 target groups / 17 displayed variable rows on the source-only fixed sample.
# Missing affix rolls now target only the 4 relic affixes (the hands sealed/primordial
# affixes are constant-1% definitions with no missing variable, so they are not targets).
MISSING_AFFIX_PATHS = [
    "data.equipment.relic.affixes.1",
    "data.equipment.relic.affixes.2",
    "data.equipment.relic.affixes.3",
    "data.equipment.relic.primordialAffix",
]
# Omnis (amulet) is ur=[] with rollable rollIds 0..6; Unsated Rage (ring2) 0..3.
OMNIS_ROLL_IDS = list(range(7))
UNSATED_ROLL_IDS = list(range(4))
EXPECTED_TARGET_GROUPS = 15
EXPECTED_VARIABLE_ROWS = 17
EXPECTED_UNIQUE_GROUPS = 11
EXPECTED_UNIQUE_ROWS = 13
EXPECTED_AFFIX_GROUPS = 4
EXPECTED_AFFIX_ROWS = 4
# Multi-variable roll slots: (instance_path, roll_id) -> [(unit, range_text), ...].
EXPECTED_MULTI_VARIABLE_ROLLS = {
    ("data.equipment.amulet", 0): [("flat", "1–2"), ("percent", "1%–45%")],
    ("data.equipment.ring2", 3): [("flat", "4–8"), ("flat", "4–8")],
}
RELIC_PRIMORDIAL_VALUE_INDEX = 1

# 2026-11-04: the delivered build is now the user-synced local-input revision
# (``current.json`` -> rev-5e0e70bf7a26b3a5, seq 4).  Source-only facts are pinned to
# the baseline revision, never derived from the moving current pointer.
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
CURRENT_INPUTS_ENTRIES = 17
INPUTS_FILE_SHA256 = (
    "102a31f1c38b9c23bc24125e13c7476d32c5d950dcbaa629b934911552123fa3"
)
# Historical read-only revisions that must stay readable after the 2026-11-04 re-import.
HISTORICAL_REVISIONS = (
    "rev-5e0e70bf7a26b3a5",
    "rev-b0f2782d178b73dd",
    "rev-04a9b21cf2436df9",
    "rev-8dc664d73737a8b9",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _baseline_pointer() -> dict:
    """Synthesize the source-only ``current.json`` pointer for the pinned baseline."""
    pointer = _read_json(SOURCE_BUILD_DIR / "current.json")
    baseline = _read_json(SOURCE_BUILD_DIR / "revisions" / BASELINE_REVISION / "build.json")
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
    """Copy the delivered build tree into ``root`` and restore the source-only baseline.

    The delivered tree now carries the user's 17 local inputs (current = seq 4).  Tests
    that assert source-only facts or mutate inputs must start from the pinned baseline
    revision; the real tree is only read and never modified.
    """
    target = root / "data" / "builds" / "letools" / SHARE_ID
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE_BUILD_DIR, target)
    inputs_dir = target / "inputs"
    if inputs_dir.is_dir():
        shutil.rmtree(inputs_dir)
    current_revision = _read_json(SOURCE_BUILD_DIR / "current.json")["revision_id"]
    if current_revision != BASELINE_REVISION:
        stale = target / "revisions" / current_revision
        if stale.is_dir():
            shutil.rmtree(stale)
    (target / "current.json").write_text(
        json.dumps(_baseline_pointer(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def _run_cli(root: Path, *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    """Run ``python -m le_build --root <root> <args…>`` exactly like the CLI docs."""
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


def make_input_entry(
    input_id: str,
    kind: str,
    instance_path: str,
    *,
    roll: int | None = None,
    roll_id: int | None = None,
    index: int | None = None,
    target: str | None = None,
    mod_index: int | None = None,
    affix_value_index: int | None = None,
    value_numeric: float | None = None,
    display_value: float | None = None,
    display_unit: str | None = None,
    note: str | None = None,
) -> dict:
    entry = {
        "input_id": input_id,
        "kind": kind,
        "instance_path": instance_path,
        "created_at": "2026-11-03T23:30:00+08:00",
    }
    if roll is not None:
        entry["roll"] = roll
    if roll_id is not None:
        entry["roll_id"] = roll_id
    if index is not None:
        entry["index"] = index
    if target is not None:
        entry["target"] = target
    if mod_index is not None:
        entry["mod_index"] = mod_index
    if affix_value_index is not None:
        entry["affix_value_index"] = affix_value_index
    if value_numeric is not None:
        entry["value_numeric"] = value_numeric
    if display_value is not None:
        entry["display_value"] = display_value
    if display_unit is not None:
        entry["display_unit"] = display_unit
    if note is not None:
        entry["note"] = note
    return entry


def valid_inputs_document(entries: list[dict] | None = None) -> dict:
    """A valid four-kind document (one entry per kind)."""
    if entries is None:
        entries = [
            make_input_entry(
                "in-20261103T233000-0001",
                "affix_roll",
                "data.equipment.relic.affixes.1",
                roll=200,
                note="游戏内显示 62%",
            ),
            make_input_entry(
                "in-20261103T233000-0002",
                "unique_mod_roll",
                "data.equipment.amulet",
                roll_id=3,
                roll=150,
                note="Omnis 第 4 个 ur 槽",
            ),
            make_input_entry(
                "in-20261103T233000-0003",
                "implicit_roll",
                "data.equipment.hands",
                index=0,
                roll=255,
            ),
            make_input_entry(
                "in-20261103T233000-0004",
                "explicit_value",
                "data.equipment.amulet",
                target="unique_mod",
                mod_index=1,
                value_numeric=0.42,
                note="直接按面板显示值 42%",
            ),
        ]
    return {
        "version": "build-local-inputs-v1",
        "build_id": BUILD_REF,
        "updated_at": "2026-11-03T23:30:00+08:00",
        "entries": entries,
    }


def all_variable_target_inputs() -> list[dict]:
    """One roll-based input per target group (15): 4 relic affixes + 11 unique rollIds.

    For multi-variable roll slots a ``unique_mod_roll`` input supplies the underlying
    roll, which drives every displayed variable of that roll slot at once.
    """
    entries: list[dict] = []
    sequence = 0
    for path in MISSING_AFFIX_PATHS:
        sequence += 1
        entries.append(
            make_input_entry(
                f"in-20261103T233000-{sequence:04d}",
                "affix_roll",
                path,
                roll=128 + sequence,
            )
        )
    for roll_id in OMNIS_ROLL_IDS:
        sequence += 1
        entries.append(
            make_input_entry(
                f"in-20261103T233000-{sequence:04d}",
                "unique_mod_roll",
                "data.equipment.amulet",
                roll_id=roll_id,
                roll=140 + roll_id,
            )
        )
    for roll_id in UNSATED_ROLL_IDS:
        sequence += 1
        entries.append(
            make_input_entry(
                f"in-20261103T233000-{sequence:04d}",
                "unique_mod_roll",
                "data.equipment.ring2",
                roll_id=roll_id,
                roll=160 + roll_id,
            )
        )
    return entries


def explicit_variable_overrides() -> list[dict]:
    """The 3 per-variable explicit entries used by the docs/tests.

    One roll slot (Omnis rollId 0) drives two displayed variables: Level of Skills
    (+2 flat) and Physical Resistance (35% -> 0.35); the relic primordial affix value 1
    is Spell Damage (180% -> 1.8).
    """
    return [
        make_input_entry(
            "in-20261103T233000-9101",
            "explicit_value",
            "data.equipment.amulet",
            target="unique_mod",
            mod_index=0,
            value_numeric=2.0,
            display_value=2.0,
            display_unit="flat",
        ),
        make_input_entry(
            "in-20261103T233000-9102",
            "explicit_value",
            "data.equipment.amulet",
            target="unique_mod",
            mod_index=1,
            value_numeric=0.35,
            display_value=35.0,
            display_unit="percent",
        ),
        make_input_entry(
            "in-20261103T233000-9103",
            "explicit_value",
            "data.equipment.relic.primordialAffix",
            target="affix",
            affix_value_index=RELIC_PRIMORDIAL_VALUE_INDEX,
            value_numeric=1.8,
            display_value=180.0,
            display_unit="percent",
        ),
    ]


def _write_json(path: Path, document) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class LocalInputsCommandTest(unittest.TestCase):
    """Each test works in a fresh copy of the *baseline* (source-only) build tree."""

    maxDiff = None

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="le_inputs_")
        self.root = Path(self._temp.name)
        self.build_dir = seed_baseline_tree(self.root)
        target = self.build_dir
        self.inputs_path = target / "inputs" / "local-inputs.json"
        self.pointer_path = target / "current.json"
        self.pointer_before = self.pointer_path.read_text(encoding="utf-8")
        self.revisions_before = sorted(
            entry.name for entry in (target / "revisions").iterdir() if entry.is_dir()
        )

    def tearDown(self) -> None:
        self._temp.cleanup()

    # ---------------------------------------------------------------- helpers
    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return _run_cli(self.root, *args)

    def set_input(self, *args: str) -> subprocess.CompletedProcess:
        return self.cli("input", "set", "--build", BUILD_REF, *args)

    def import_inputs(self, document: dict, *, name: str = "inputs.json") -> subprocess.CompletedProcess:
        path = _write_json(self.root / "tmp-inputs" / name, document)
        return self.cli("input", "import", "--build", BUILD_REF, "--file", str(path))

    def revision_ids(self) -> list[str]:
        return sorted(
            entry.name for entry in (self.build_dir / "revisions").iterdir() if entry.is_dir()
        )

    def entry_count(self) -> int:
        if not self.inputs_path.is_file():
            return 0
        return len((_read_json(self.inputs_path) or {}).get("entries") or [])

    def assert_no_publish(self) -> None:
        self.assertEqual(
            self.pointer_path.read_text(encoding="utf-8"), self.pointer_before
        )
        self.assertEqual(self.revision_ids(), self.revisions_before)

    def assert_error(self, proc: subprocess.CompletedProcess) -> None:
        self.assertNotEqual(proc.returncode, 0, f"expected failure, got:\n{proc.stdout}")
        self.assertIn("error", (proc.stderr or "").lower())
        self.assertNotIn("Traceback", proc.stderr or "")
        self.assert_no_publish()

    # ------------------------------------------------------ set / file shape
    def test_input_set_writes_local_inputs_without_publishing(self) -> None:
        proc = self.set_input(
            "--kind",
            "affix_roll",
            "--instance",
            "data.equipment.relic.affixes.1",
            "--roll",
            "200",
            "--note",
            "游戏内 62%",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.inputs_path.is_file(), "input set must write the local inputs file")
        document = _read_json(self.inputs_path)
        self.assertEqual(document.get("version"), "build-local-inputs-v1")
        self.assertEqual(document.get("build_id"), BUILD_REF)
        entries = document.get("entries") or []
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.get("kind"), "affix_roll")
        self.assertEqual(entry.get("instance_path"), "data.equipment.relic.affixes.1")
        self.assertEqual(entry.get("roll"), 200)
        self.assertEqual(entry.get("note"), "游戏内 62%")
        self.assertTrue(str(entry.get("input_id") or "").startswith("in-"))
        self.assertTrue(entry.get("created_at"))
        # Only the inputs file changed: no revision is published by `input set`.
        self.assert_no_publish()

    def test_all_four_kinds_are_accepted(self) -> None:
        document = valid_inputs_document()
        proc = self.import_inputs(document)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        written = _read_json(self.inputs_path)
        self.assertEqual(
            sorted(entry["kind"] for entry in written["entries"]),
            ["affix_roll", "explicit_value", "implicit_roll", "unique_mod_roll"],
        )
        self.assert_no_publish()

    def test_input_import_export_roundtrip(self) -> None:
        document = valid_inputs_document()
        proc = self.import_inputs(document)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out_path = self.root / "tmp-inputs" / "exported.json"
        proc = self.cli(
            "input", "export", "--build", BUILD_REF, "--out", str(out_path)
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(out_path.is_file())
        exported = _read_json(out_path)
        self.assertEqual(exported.get("version"), "build-local-inputs-v1")
        self.assertEqual(exported.get("build_id"), BUILD_REF)
        by_target = {
            (entry["kind"], entry["instance_path"], entry.get("roll_id"), entry.get("index"), entry.get("mod_index")): entry
            for entry in exported["entries"]
        }
        self.assertEqual(len(by_target), 4)
        self.assertIn(("affix_roll", "data.equipment.relic.affixes.1", None, None, None), by_target)
        self.assertIn(("unique_mod_roll", "data.equipment.amulet", 3, None, None), by_target)
        self.assertIn(("implicit_roll", "data.equipment.hands", None, 0, None), by_target)
        self.assertIn(("explicit_value", "data.equipment.amulet", None, None, 1), by_target)
        # Exported document can be imported again with --replace (merge would reject the
        # already-present targets as duplicates).
        proc = self.cli(
            "input", "import", "--build", BUILD_REF, "--file", str(out_path), "--replace"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.entry_count(), 4)
        # A merge import of the same targets is rejected as duplicate instead of silently
        # overwriting existing values.
        proc = self.cli("input", "import", "--build", BUILD_REF, "--file", str(out_path))
        self.assertNotEqual(proc.returncode, 0)

    def test_input_remove_deletes_one_entry(self) -> None:
        proc = self.import_inputs(valid_inputs_document())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entries = _read_json(self.inputs_path)["entries"]
        target_id = entries[0]["input_id"]
        proc = self.cli("input", "remove", "--build", BUILD_REF, "--input-id", target_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        remaining = _read_json(self.inputs_path)["entries"]
        self.assertEqual(len(remaining), 3)
        self.assertNotIn(target_id, {entry["input_id"] for entry in remaining})

    def test_input_list_reports_missing_targets(self) -> None:
        proc = self.cli("input", "list", "--build", BUILD_REF, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload.get("build_id"), BUILD_REF)
        # 2026-11-03 per-variable interface: 15 target groups / 17 variable rows.
        self.assertEqual(payload.get("unresolved_targets_total"), EXPECTED_TARGET_GROUPS)
        self.assertEqual(payload.get("pending_targets"), EXPECTED_TARGET_GROUPS)
        self.assertEqual(payload.get("entries"), 0)
        targets = payload.get("targets") or []
        self.assertEqual(len(targets), EXPECTED_TARGET_GROUPS)
        self.assertEqual(
            sum(len(target.get("variables") or []) for target in targets),
            EXPECTED_VARIABLE_ROWS,
        )
        kinds: dict[str, int] = {}
        rows_by_kind: dict[str, int] = {}
        for target in targets:
            kinds[target["kind"]] = kinds.get(target["kind"], 0) + 1
            rows_by_kind[target["kind"]] = rows_by_kind.get(target["kind"], 0) + len(
                target.get("variables") or []
            )
            self.assertTrue(target.get("instance_path"))
            variables = target.get("variables") or []
            self.assertTrue(variables, target)
            self.assertEqual(len(target.get("suggested_variable_commands") or []), len(variables))
        self.assertEqual(
            kinds, {"affix_roll": EXPECTED_AFFIX_GROUPS, "unique_mod_roll": EXPECTED_UNIQUE_GROUPS}
        )
        self.assertEqual(
            rows_by_kind,
            {"affix_roll": EXPECTED_AFFIX_ROWS, "unique_mod_roll": EXPECTED_UNIQUE_ROWS},
        )
        instance_paths = {target["instance_path"] for target in targets}
        # The 4 relic missing-variable affixes and the two ur=[] items are listed.
        for path in MISSING_AFFIX_PATHS:
            self.assertIn(path, instance_paths)
        self.assertIn("data.equipment.amulet", instance_paths)
        self.assertIn("data.equipment.ring2", instance_paths)
        # Constant-1% hands affixes have no missing variable -> no longer targets.
        self.assertNotIn("data.equipment.hands.sealedAffix", instance_paths)
        self.assertNotIn("data.equipment.hands.primordialAffix", instance_paths)
        # Human-readable mode reports both target groups and variable rows.
        human = self.cli("input", "list", "--build", BUILD_REF)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("pending: 15/15 target group(s), 17/17 variable row(s)", human.stdout)

    # ------------------------------------------------------ variable-level API
    def test_describe_targets_are_variable_level_with_display_metadata(self) -> None:
        from le_build.inputs import describe_targets

        spec = _read_json(
            SOURCE_BUILD_DIR
            / "revisions"
            / BASELINE_REVISION
            / "build.json"
        )
        targets = describe_targets(spec)
        self.assertEqual(len(targets), EXPECTED_TARGET_GROUPS)
        self.assertEqual(
            sum(len(target.get("variables") or []) for target in targets),
            EXPECTED_VARIABLE_ROWS,
        )
        by_key = {
            (target["kind"], target["instance_path"], target.get("roll_id")): target
            for target in targets
        }
        for (instance_path, roll_id), expected_rows in EXPECTED_MULTI_VARIABLE_ROLLS.items():
            target = by_key[("unique_mod_roll", instance_path, roll_id)]
            self.assertEqual(
                [(variable["unit"], variable["range_text"]) for variable in target["variables"]],
                expected_rows,
            )
            # Every variable row has its own locator + explicit-value command.
            self.assertEqual(
                len({variable["mod_index"] for variable in target["variables"]}),
                len(expected_rows),
            )
            commands = target["suggested_variable_commands"]
            self.assertEqual(len(commands), len(expected_rows))
            for command in commands:
                self.assertIn("--display-value", command)
                self.assertIn("--kind explicit_value", command)
        # Relic primordial affix exposes only the missing variable (value index 1).
        relic = by_key[("affix_roll", "data.equipment.relic.primordialAffix", None)]
        self.assertEqual(len(relic["variables"]), 1)
        relic_var = relic["variables"][0]
        self.assertEqual(relic_var["affix_value_index"], RELIC_PRIMORDIAL_VALUE_INDEX)
        self.assertEqual(relic_var["unit"], "percent")
        self.assertEqual(relic_var["range_text"], "160%–200%")
        # Every variable carries unit/percent/range_text metadata.
        for target in targets:
            for variable in target["variables"]:
                self.assertIn(variable["unit"], ("percent", "flat"))
                self.assertIsInstance(variable["percent"], bool)
                self.assertIn("range_text", variable)

    def test_build_target_index_percent_flags(self) -> None:
        from le_build.inputs import build_target_index

        spec = _read_json(
            SOURCE_BUILD_DIR
            / "revisions"
            / BASELINE_REVISION
            / "build.json"
        )
        index = build_target_index(spec)
        # Omnis rollId 0 drives one flat variable (Level of Skills, range 1–2) and one
        # percent variable (Physical Resistance, range 1%–45%).
        level = index["explicit_value"][("unique_mod", "data.equipment.amulet", 0)]
        resistance = index["explicit_value"][("unique_mod", "data.equipment.amulet", 1)]
        self.assertFalse(level["percent"])
        self.assertEqual(level["unit"], "flat")
        self.assertTrue(resistance["percent"])
        self.assertEqual(resistance["unit"], "percent")
        # Relic primordial affix: value 0 (+6 Level) is constant/flat, value 1 is the
        # percent Spell Damage variable (already-resolvable value 0 is not a target row).
        primordial = index["explicit_value"][("affix", "data.equipment.relic.primordialAffix")]
        self.assertEqual(len(primordial["values"]), 2)
        self.assertFalse(primordial["values"][0]["percent"])
        self.assertTrue(primordial["values"][1]["percent"])
        self.assertEqual(primordial["values"][1]["range_text"], "160%–200%")

    def test_full_variable_target_document_is_accepted(self) -> None:
        document = valid_inputs_document(all_variable_target_inputs())
        proc = self.import_inputs(document, name="all-targets.json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.entry_count(), EXPECTED_TARGET_GROUPS)
        self.assert_no_publish()

    def test_import_out_of_range_explicit_value_is_rejected(self) -> None:
        # Bounds come from the declared range: 1%–45% target, normalized 0.9 > 0.45.
        document = valid_inputs_document(
            [
                make_input_entry(
                    "in-oob-1",
                    "explicit_value",
                    "data.equipment.amulet",
                    target="unique_mod",
                    mod_index=1,
                    value_numeric=0.9,
                )
            ]
        )
        proc = self.import_inputs(document, name="out-of-range.json")
        self.assert_error(proc)
        self.assertIn("outside the declared range", proc.stderr)
        self.assertEqual(self.entry_count(), 0)

    def test_import_display_value_keys_are_accepted(self) -> None:
        document = valid_inputs_document(
            [
                make_input_entry(
                    "in-display-1",
                    "explicit_value",
                    "data.equipment.amulet",
                    target="unique_mod",
                    mod_index=1,
                    value_numeric=0.35,
                    display_value=35.0,
                    display_unit="percent",
                )
            ]
        )
        proc = self.import_inputs(document, name="display-value.json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = _read_json(self.inputs_path)["entries"][0]
        self.assertEqual(entry["display_value"], 35.0)
        self.assertEqual(entry["display_unit"], "percent")

    def test_duplicate_explicit_targets_in_file_are_rejected(self) -> None:
        document = valid_inputs_document(
            [
                make_input_entry(
                    "in-dup-1", "explicit_value", "data.equipment.amulet",
                    target="unique_mod", mod_index=1, value_numeric=0.35,
                ),
                make_input_entry(
                    "in-dup-2", "explicit_value", "data.equipment.amulet",
                    target="unique_mod", mod_index=1, value_numeric=0.30,
                ),
            ]
        )
        proc = self.import_inputs(document, name="dup-explicit.json")
        self.assert_error(proc)
        self.assertIn("duplicate", proc.stderr.lower())
        self.assertEqual(self.entry_count(), 0)

    def test_malformed_display_value_and_unit_are_rejected(self) -> None:
        bad_unit = valid_inputs_document(
            [
                make_input_entry(
                    "in-unit-1", "explicit_value", "data.equipment.amulet",
                    target="unique_mod", mod_index=1, value_numeric=0.35,
                    display_value=35.0, display_unit="bogus",
                )
            ]
        )
        proc = self.import_inputs(bad_unit, name="bad-unit.json")
        self.assert_error(proc)
        self.assertIn("display_unit", proc.stderr)
        bad_display = valid_inputs_document(
            [
                make_input_entry(
                    "in-display-2", "explicit_value", "data.equipment.amulet",
                    target="unique_mod", mod_index=1, value_numeric=0.35,
                    display_value="thirty-five", display_unit="percent",
                )
            ]
        )
        proc = self.import_inputs(bad_display, name="bad-display.json")
        self.assert_error(proc)
        self.assertIn("display_value", proc.stderr)

    # -------------------------------------------------------- roll boundaries
    def test_roll_boundaries_zero_is_valid(self) -> None:
        # r=0 and r=255 are both valid; `input set` is append-only per target, so use
        # two distinct missing affix instances.
        for path, roll in (
            ("data.equipment.relic.affixes.1", 0),
            ("data.equipment.relic.affixes.2", 255),
        ):
            proc = self.set_input(
                "--kind",
                "affix_roll",
                "--instance",
                path,
                "--roll",
                str(roll),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
        entries = {
            entry["instance_path"]: entry["roll"]
            for entry in _read_json(self.inputs_path)["entries"]
        }
        self.assertEqual(entries["data.equipment.relic.affixes.1"], 0)
        self.assertEqual(entries["data.equipment.relic.affixes.2"], 255)
        # r=0 must never be collapsed into "missing".
        self.assertIn(0, entries.values())
        self.assert_no_publish()

    def test_out_of_range_and_non_integer_rolls_are_rejected(self) -> None:
        for bad_roll in ("256", "-1", "1.5", "abc"):
            proc = self.set_input(
                "--kind",
                "affix_roll",
                "--instance",
                "data.equipment.relic.affixes.1",
                "--roll",
                bad_roll,
            )
            self.assert_error(proc)
            self.assertEqual(self.entry_count(), 0, f"roll {bad_roll} must not be saved")

    def test_unknown_kind_and_missing_locators_are_rejected(self) -> None:
        cases = [
            ("--kind", "bogus", "--instance", "data.equipment.hands", "--roll", "1"),
            ("--kind", "unique_mod_roll", "--instance", "data.equipment.amulet", "--roll", "1"),
            ("--kind", "implicit_roll", "--instance", "data.equipment.hands", "--roll", "1"),
            ("--kind", "explicit_value", "--instance", "data.equipment.amulet", "--value", "0.5"),
        ]
        for args in cases:
            with self.subTest(args=args):
                proc = self.set_input(*args)
                self.assert_error(proc)

    def test_unknown_targets_are_rejected(self) -> None:
        cases = [
            # unknown instance path
            ("--kind", "affix_roll", "--instance", "data.equipment.wings", "--roll", "10"),
            # unique rollId beyond rollable rollIds (Omnis rollable = 0..6)
            ("--kind", "unique_mod_roll", "--instance", "data.equipment.amulet", "--roll-id", "99", "--roll", "10"),
            # unknown unique mod_index (Apex has 10 mods)
            ("--kind", "explicit_value", "--instance", "data.equipment.head", "--target", "unique_mod", "--mod-index", "99", "--value", "0.1"),
            # implicit index out of range (hands has 3 ir slots)
            ("--kind", "implicit_roll", "--instance", "data.equipment.hands", "--index", "9", "--roll", "10"),
        ]
        for args in cases:
            with self.subTest(args=args):
                proc = self.set_input(*args)
                self.assert_error(proc)

    # ------------------------------------------------------- import validation
    def test_duplicate_target_entries_are_rejected(self) -> None:
        duplicate = [
            make_input_entry("in-1", "unique_mod_roll", "data.equipment.amulet", roll_id=3, roll=10),
            make_input_entry("in-2", "unique_mod_roll", "data.equipment.amulet", roll_id=3, roll=20),
        ]
        proc = self.import_inputs(valid_inputs_document(duplicate))
        self.assert_error(proc)
        self.assertEqual(self.entry_count(), 0)

    def test_malformed_document_metadata_is_rejected(self) -> None:
        cases = {
            "bad_version": {**valid_inputs_document(), "version": "build-local-inputs-v0"},
            "bad_build_id": {**valid_inputs_document(), "build_id": "letools:other"},
            "entries_not_list": {**valid_inputs_document(), "entries": {"a": 1}},
        }
        for name, document in cases.items():
            with self.subTest(name=name):
                proc = self.import_inputs(document, name=f"{name}.json")
                self.assert_error(proc)

    def test_malformed_entry_fields_are_rejected(self) -> None:
        cases = {
            "missing_kind": [
                {
                    "input_id": "in-1",
                    "instance_path": "data.equipment.hands.sealedAffix",
                    "roll": 1,
                }
            ],
            "missing_instance": [{"input_id": "in-1", "kind": "affix_roll", "roll": 1}],
            "roll_float": [
                {
                    **make_input_entry("in-1", "affix_roll", "data.equipment.hands.sealedAffix", roll=1),
                    "roll": 1.5,
                }
            ],
        }
        # The first two cases are also "missing required field" re-checks; the third
        # exercises a float roll in a file (the CLI flag parser already rejects floats).
        for name, entries in cases.items():
            with self.subTest(name=name):
                proc = self.import_inputs(valid_inputs_document(entries), name=f"{name}.json")
                self.assert_error(proc)

    def test_non_finite_explicit_values_are_rejected(self) -> None:
        document = valid_inputs_document()
        raw = json.dumps(document, ensure_ascii=False, indent=2)
        # json.dumps writes NaN/Infinity as bare tokens; the loader must reject them.
        for token in ("NaN", "Infinity"):
            with self.subTest(token=token):
                broken = raw.replace('"value_numeric": 0.42', f'"value_numeric": {token}')
                self.assertIn(token, broken)
                path = self.root / "tmp-inputs" / f"nonfinite_{token}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(broken, encoding="utf-8")
                proc = self.cli("input", "import", "--build", BUILD_REF, "--file", str(path))
                self.assert_error(proc)

    def test_legacy_or_unknown_instance_paths_are_rejected_gracefully(self) -> None:
        entries = [
            make_input_entry(
                "in-legacy", "implicit_roll", "data.weaverTree.selected.0", index=0, roll=1
            )
        ]
        proc = self.import_inputs(valid_inputs_document(entries), name="legacy.json")
        self.assert_error(proc)
        self.assertNotIn("Traceback", proc.stderr or "")

    # NOTE: `input set --sync` needs a project root with its own catalog pointer
    # (data/current.json); the set→sync equivalence is covered by
    # tests/test_cli_offline.py::LocalInputSyncCliTest with an explicit `--db` sync.


class DeliveredBuildStateTest(unittest.TestCase):
    """Read-only assertions on the real delivered build (2026-11-04 user sync).

    The delivered ``current`` revision is now the user-synced local-input revision; the
    source-only baseline stays readable and remains the pinned reference for source-only
    facts (15 target groups / 17 variable rows).  These tests never write ``data/builds``.
    """

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        require_dataset_db()

    def test_current_delivered_revision_records_user_inputs(self) -> None:
        from le_build import inputs as inputs_module
        from le_build.inputs import describe_targets

        pointer = _read_json(SOURCE_BUILD_DIR / "current.json")
        self.assertEqual(pointer["revision_id"], CURRENT_REVISION)
        self.assertEqual(pointer["revision_seq"], 8)
        self.assertEqual(pointer["content_sha256"], CURRENT_CONTENT_SHA256)
        current = _read_json(
            SOURCE_BUILD_DIR / "revisions" / CURRENT_REVISION / "build.json"
        )
        meta_local = current["meta"]["local_inputs"]
        self.assertEqual(meta_local["entries"], CURRENT_INPUTS_ENTRIES)
        self.assertEqual(meta_local["applied"], CURRENT_INPUTS_ENTRIES)
        self.assertEqual(len(meta_local["inputs_manifest"]), CURRENT_INPUTS_ENTRIES)
        self.assertTrue(
            all(entry.get("applied") for entry in meta_local["inputs_manifest"])
        )
        inputs_path = SOURCE_BUILD_DIR / "inputs" / "local-inputs.json"
        self.assertTrue(inputs_path.is_file(), "delivered build must carry the user inputs sidecar")
        self.assertEqual(
            hashlib.sha256(inputs_path.read_bytes()).hexdigest(), INPUTS_FILE_SHA256
        )
        document = _read_json(inputs_path)
        self.assertEqual(len(document["entries"]), CURRENT_INPUTS_ENTRIES)
        self.assertEqual({entry["kind"] for entry in document["entries"]}, {"explicit_value"})
        # meta.local_inputs.sha256 is the canonical document hash, not the file bytes hash.
        self.assertEqual(meta_local["sha256"], inputs_module.document_sha256(document))
        self.assertEqual(meta_local["sha256"], CURRENT_INPUTS_DOC_SHA256)
        # The synced build is fully resolved and readable.
        self.assertEqual(current["statuses"]["value_resolution"], "resolved")
        self.assertEqual(current["statuses"]["text_readiness"], "complete")
        self.assertEqual(current["statuses"]["readable_completeness"], {"complete": 29})
        self.assertEqual(current["readable_summary"]["local_input_lines"], 17)
        self.assertEqual(describe_targets(current), [])
        # CLI read-only view of the delivered current build: 17 entries, 0 pending.
        proc = _run_cli(REPO, "input", "list", "--build", BUILD_REF, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["entries"], CURRENT_INPUTS_ENTRIES)
        self.assertEqual(payload["pending_targets"], 0)
        self.assertEqual(payload["unresolved_targets_total"], 0)
        # Delivered build validates cleanly.
        proc = _run_cli(REPO, "validate", "--build", BUILD_REF, "--offline")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("pass=44", combined)
        self.assertIn("warn=0", combined)
        self.assertIn("fail=0", combined)

    def test_baseline_revision_remains_source_only(self) -> None:
        from le_build.inputs import describe_targets

        baseline = _read_json(
            SOURCE_BUILD_DIR / "revisions" / BASELINE_REVISION / "build.json"
        )
        self.assertNotIn("local_inputs", baseline.get("meta") or {})
        self.assertEqual(baseline["statuses"]["value_resolution"], "partial")
        self.assertEqual(baseline["statuses"]["readable_completeness"], {"complete": 26, "partial": 3})
        summary = baseline["readable_summary"]
        self.assertEqual(summary["line_value_status"], {"actual": 124, "range": 17})
        self.assertIsNone(summary.get("local_input_lines"))
        targets = describe_targets(baseline)
        self.assertEqual(len(targets), EXPECTED_TARGET_GROUPS)
        self.assertEqual(
            sum(len(target.get("variables") or []) for target in targets),
            EXPECTED_VARIABLE_ROWS,
        )
        # Historical revisions (old current/old baseline/intermediate/legacy) stay readable.
        for revision_id in HISTORICAL_REVISIONS:
            revision = _read_json(
                SOURCE_BUILD_DIR / "revisions" / revision_id / "build.json"
            )
            self.assertEqual(revision["meta"]["revision"]["revision_id"], revision_id)
        self.assertNotIn(
            "local_inputs",
            _read_json(
                SOURCE_BUILD_DIR / "revisions" / "rev-b0f2782d178b73dd" / "build.json"
            )["meta"],
        )
        self.assertEqual(
            _read_json(
                SOURCE_BUILD_DIR / "revisions" / "rev-5e0e70bf7a26b3a5" / "build.json"
            )["meta"]["local_inputs"]["entries"],
            17,
        )
        # Baseline revision is still readable through the store (meta points at seq 5).
        self.assertEqual(baseline["meta"]["revision"]["revision_id"], BASELINE_REVISION)
        self.assertEqual(baseline["meta"]["revision"]["revision_seq"], 5)
        self.assertEqual(
            baseline["meta"]["revision"]["content_sha256"], BASELINE_CONTENT_SHA256
        )

    def test_historical_revision_still_validates_warn_exit_3(self) -> None:
        proc = _run_cli(
            REPO,
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


def _collect_input_targets(stdout: str) -> list[dict]:
    """Best-effort extraction of input-target dicts from JSON-ish CLI output."""
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return []
    collected: list[dict] = []

    def walk(value):
        if isinstance(value, dict):
            if value.get("instance_path") and (
                value.get("kind") or value.get("roll_id") is not None or value.get("index") is not None
            ):
                collected.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return collected


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
