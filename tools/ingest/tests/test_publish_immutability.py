"""P1-5 immutable publish tests + P2-3 partial release contract.

``publish_dataset`` must stage the database and manifest completely, verify
them, then atomically activate the release.  A release with an existing
``dataset_id`` is immutable: identical content is idempotent, different content
is rejected.  ``current.json`` is switched last, so an injected failure at the
database-write, manifest-write or current-switch step leaves the previous
current pointer and every previous release byte-for-byte unchanged.

All data/release/current paths are patched to a temporary directory; the real
``data/current.json`` is never written.
"""
from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gate_support as gates  # noqa: E402
import le_data.paths as paths  # noqa: E402
import le_data.report.manifest as manifest  # noqa: E402
from le_data.store import connect, logical_fingerprint  # noqa: E402


class PublishImmutabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data_root = self.tmp / "data"
        self.releases_root = self.data_root / "releases"
        for directory in (self.data_root / "normalized", self.releases_root):
            directory.mkdir(parents=True, exist_ok=True)
        self.current_path = self.data_root / "current.json"
        self.current_path.write_text(
            json.dumps({"dataset_id": "previous", "logical_content_sha256": "old"}),
            encoding="utf-8",
        )
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(manifest, "NORMALIZED_DIR", self.data_root / "normalized"))
        stack.enter_context(mock.patch.object(manifest, "RELEASES_DIR", self.releases_root))
        stack.enter_context(mock.patch.object(paths, "DATA_DIR", self.data_root))

    # -- helpers -----------------------------------------------------------
    def db_path(self, name: str) -> Path:
        return self.data_root / "normalized" / name / "data.sqlite"

    def release_dir(self, dataset_id: str) -> Path:
        return self.releases_root / dataset_id

    def current_bytes(self) -> bytes:
        return self.current_path.read_bytes()

    def current(self) -> dict:
        return json.loads(self.current_path.read_text(encoding="utf-8"))

    def assert_no_lock(self):
        self.assertFalse((self.data_root / ".publish.lock").exists(), "publish lock was not released")

    def assert_no_staging_leftovers(self):
        leftovers = [path.name for path in self.releases_root.glob(".staging-*")]
        self.assertEqual(leftovers, [], f"staging directories left behind: {leftovers}")

    def make_db(self, name: str, status: str = "valid", dataset_id: str | None = None) -> Path:
        dataset_id = dataset_id or name
        return gates.make_dataset_db(self.db_path(name), dataset_id, status, f"skill:{name}")

    # -- idempotency / immutability ---------------------------------------
    def test_same_id_identical_content_is_idempotent(self):
        db = self.make_db("ds-ok")
        first = manifest.publish_dataset("ds-ok", dataset_db=db)
        self.assertEqual(first["result"], "published")
        release_db = self.release_dir("ds-ok") / "data.sqlite"
        release_manifest = self.release_dir("ds-ok") / "manifest.json"
        db_bytes = release_db.read_bytes()
        manifest_bytes = release_manifest.read_bytes()
        current_bytes = self.current_bytes()

        second = manifest.publish_dataset("ds-ok", dataset_db=db)
        self.assertEqual(second["result"], "idempotent")
        self.assertFalse(second["switched_current"])
        self.assertEqual(release_db.read_bytes(), db_bytes)
        self.assertEqual(release_manifest.read_bytes(), manifest_bytes)
        self.assertEqual(self.current_bytes(), current_bytes)
        self.assertEqual(self.current()["dataset_id"], "ds-ok")
        self.assertFalse(second["current"].get("partial"))
        self.assert_no_lock()

    def test_same_id_different_content_is_rejected(self):
        db = self.make_db("ds-same")
        manifest.publish_dataset("ds-same", dataset_db=db)
        release_db = self.release_dir("ds-same") / "data.sqlite"
        release_manifest = self.release_dir("ds-same") / "manifest.json"
        db_bytes = release_db.read_bytes()
        manifest_bytes = release_manifest.read_bytes()
        current_bytes = self.current_bytes()

        gates.add_entity(db, "skill:ds-same-new")
        with self.assertRaises(RuntimeError) as ctx:
            manifest.publish_dataset("ds-same", dataset_db=db)
        message = str(ctx.exception)
        self.assertIn("different content", message)
        self.assertIn("new dataset id", message)
        self.assertEqual(release_db.read_bytes(), db_bytes)
        self.assertEqual(release_manifest.read_bytes(), manifest_bytes)
        self.assertEqual(self.current_bytes(), current_bytes)
        self.assert_no_lock()
        self.assert_no_staging_leftovers()

    def test_corrupted_existing_release_is_not_accepted_as_idempotent(self):
        db = self.make_db("ds-corrupt")
        manifest.publish_dataset("ds-corrupt", dataset_db=db)
        (self.release_dir("ds-corrupt") / "data.sqlite").write_bytes(b"corrupted")
        current_bytes = self.current_bytes()
        with self.assertRaises(RuntimeError) as ctx:
            manifest.publish_dataset("ds-corrupt", dataset_db=db)
        self.assertIn("does not match its manifest", str(ctx.exception))
        self.assertEqual(self.current_bytes(), current_bytes)
        self.assert_no_lock()

    # -- fault injection ---------------------------------------------------
    def test_failure_after_db_write_before_manifest_keeps_previous_state(self):
        first = self.make_db("ds-first")
        manifest.publish_dataset("ds-first", dataset_db=first)
        current_bytes = self.current_bytes()
        release_bytes = (self.release_dir("ds-first") / "data.sqlite").read_bytes()

        second = self.make_db("ds-inject")
        with mock.patch.object(
            manifest,
            "_write_staged_manifest",
            side_effect=OSError("injected manifest write failure"),
        ):
            with self.assertRaises(OSError):
                manifest.publish_dataset("ds-inject", dataset_db=second)
        self.assertEqual(self.current_bytes(), current_bytes)
        self.assertEqual((self.release_dir("ds-first") / "data.sqlite").read_bytes(), release_bytes)
        self.assertFalse(self.release_dir("ds-inject").exists())
        self.assert_no_staging_leftovers()
        self.assert_no_lock()

    def test_failure_during_database_copy_keeps_previous_state(self):
        first = self.make_db("ds-first")
        manifest.publish_dataset("ds-first", dataset_db=first)
        current_bytes = self.current_bytes()
        second = self.make_db("ds-inject-copy")
        with mock.patch.object(manifest.shutil, "copyfile", side_effect=OSError("injected copy failure")):
            with self.assertRaises(OSError):
                manifest.publish_dataset("ds-inject-copy", dataset_db=second)
        self.assertEqual(self.current_bytes(), current_bytes)
        self.assertFalse(self.release_dir("ds-inject-copy").exists())
        self.assert_no_staging_leftovers()
        self.assert_no_lock()

    def test_failure_before_current_switch_keeps_previous_state_and_rolls_back(self):
        first = self.make_db("ds-first")
        manifest.publish_dataset("ds-first", dataset_db=first)
        current_bytes = self.current_bytes()
        release_bytes = (self.release_dir("ds-first") / "data.sqlite").read_bytes()

        second = self.make_db("ds-inject-current")
        with mock.patch.object(
            manifest, "_set_current", side_effect=OSError("injected current switch failure")
        ):
            with self.assertRaises(OSError):
                manifest.publish_dataset("ds-inject-current", dataset_db=second)
        self.assertEqual(self.current_bytes(), current_bytes)
        self.assertEqual((self.release_dir("ds-first") / "data.sqlite").read_bytes(), release_bytes)
        self.assertFalse(self.release_dir("ds-inject-current").exists())
        self.assert_no_staging_leftovers()
        self.assert_no_lock()
        # A retry after the injected failure succeeds and switches current.
        retried = manifest.publish_dataset("ds-inject-current", dataset_db=second)
        self.assertEqual(retried["result"], "published")
        self.assertEqual(self.current()["dataset_id"], "ds-inject-current")

    # -- published artifact consistency -----------------------------------
    def test_release_artifact_matches_manifest_and_current(self):
        db = self.make_db("ds-artifact")
        output = manifest.publish_dataset("ds-artifact", dataset_db=db)
        release = self.release_dir("ds-artifact")
        self.assertEqual(sorted(path.name for path in release.iterdir()), ["data.sqlite", "manifest.json"])
        written = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(written["dataset_id"], "ds-artifact")
        manifest_db_path = Path(written["database"]["path"])
        self.assertEqual(manifest_db_path.name, "data.sqlite")
        self.assertEqual(manifest_db_path.parent.name, "ds-artifact")
        self.assertNotIn(".staging-", written["database"]["path"])
        self.assertEqual(written["database"]["sha256"], manifest.sha256_file(release / "data.sqlite"))
        self.assertEqual(written["database"]["size_bytes"], (release / "data.sqlite").stat().st_size)
        conn = connect(release / "data.sqlite", readonly=True)
        try:
            fingerprint = logical_fingerprint(conn)
        finally:
            conn.close()
        self.assertEqual(
            written["database"]["logical_content_sha256"], fingerprint["logical_sha256"]
        )
        current = self.current()
        self.assertEqual(current["dataset_id"], "ds-artifact")
        self.assertEqual(current["file_sha256"], written["database"]["sha256"])
        self.assertEqual(current["logical_content_sha256"], written["database"]["logical_content_sha256"])
        self.assertEqual(output["current"], current)
        self.assert_no_lock()
        self.assert_no_staging_leftovers()

    # -- P2-3 partial contract --------------------------------------------
    def test_partial_without_allow_partial_is_refused(self):
        db = self.make_db("ds-partial", status="partial")
        current_bytes = self.current_bytes()
        with self.assertRaises(RuntimeError) as ctx:
            manifest.publish_dataset("ds-partial", dataset_db=db)
        self.assertIn("status is 'partial'", str(ctx.exception))
        self.assertIn("allow_partial=True", str(ctx.exception))
        self.assertEqual(self.current_bytes(), current_bytes)
        self.assertFalse(self.release_dir("ds-partial").exists())
        self.assert_no_lock()

    def test_partial_release_keeps_existing_current_by_default(self):
        valid = self.make_db("ds-valid")
        manifest.publish_dataset("ds-valid", dataset_db=valid)
        current_bytes = self.current_bytes()

        partial = self.make_db("ds-partial", status="partial")
        output = manifest.publish_dataset("ds-partial", dataset_db=partial, allow_partial=True)
        self.assertEqual(output["result"], "partial_released")
        self.assertFalse(output["switched_current"])
        self.assertTrue((self.release_dir("ds-partial") / "data.sqlite").exists())
        released = json.loads(
            (self.release_dir("ds-partial") / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(released["dataset_status"], "partial")
        self.assertEqual(self.current_bytes(), current_bytes)
        self.assertEqual(self.current()["dataset_id"], "ds-valid")
        self.assert_no_lock()

        # Re-publishing the same partial content stays a no-op for current.
        again = manifest.publish_dataset("ds-partial", dataset_db=partial, allow_partial=True)
        self.assertEqual(again["result"], "partial_released")
        self.assertEqual(self.current_bytes(), current_bytes)

    def test_partial_force_current_switches_pointer(self):
        valid = self.make_db("ds-valid")
        manifest.publish_dataset("ds-valid", dataset_db=valid)
        partial = self.make_db("ds-partial", status="partial")
        output = manifest.publish_dataset(
            "ds-partial",
            dataset_db=partial,
            allow_partial=True,
            force_current=True,
        )
        self.assertEqual(output["result"], "published")
        self.assertTrue(output["switched_current"])
        current = self.current()
        self.assertEqual(current["dataset_id"], "ds-partial")
        self.assertTrue(current["partial"])
        self.assert_no_lock()

    def test_partial_without_existing_current_only_releases(self):
        self.current_path.unlink()
        partial = self.make_db("ds-partial", status="partial")
        output = manifest.publish_dataset("ds-partial", dataset_db=partial, allow_partial=True)
        self.assertEqual(output["result"], "partial_released")
        self.assertFalse(self.current_path.exists())
        self.assertTrue((self.release_dir("ds-partial") / "manifest.json").exists())

    # -- publish lock ------------------------------------------------------
    def test_existing_lock_file_blocks_publish(self):
        db = self.make_db("ds-locked")
        lock_path = self.data_root / ".publish.lock"
        lock_path.write_text("held", encoding="utf-8")
        current_bytes = self.current_bytes()
        with self.assertRaises(RuntimeError) as ctx:
            manifest.publish_dataset("ds-locked", dataset_db=db)
        self.assertIn("publish lock", str(ctx.exception))
        self.assertEqual(self.current_bytes(), current_bytes)
        self.assertFalse(self.release_dir("ds-locked").exists())
        self.assertTrue(lock_path.exists(), "a pre-existing lock must not be removed")
        lock_path.unlink(missing_ok=True)
        self.assertEqual(manifest.publish_dataset("ds-locked", dataset_db=db)["result"], "published")

    def test_concurrent_publish_is_serialized_by_lock(self):
        first_db = self.make_db("ds-conc-a")
        entered = threading.Event()
        proceed = threading.Event()
        holder: dict = {}
        original_stage = manifest._stage_database

        def slow_stage(*args, **kwargs):
            entered.set()
            if not proceed.wait(10):
                raise AssertionError("timed out waiting for the test to release the lock")
            return original_stage(*args, **kwargs)

        def worker():
            try:
                holder["result"] = manifest.publish_dataset("ds-conc-a", dataset_db=first_db)
            except BaseException as exc:  # noqa: BLE001
                holder["error"] = exc

        with mock.patch.object(manifest, "_stage_database", side_effect=slow_stage):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(10), "first publisher never reached the staging step")
                second_db = self.make_db("ds-conc-b")
                with self.assertRaises(RuntimeError) as ctx:
                    manifest.publish_dataset("ds-conc-b", dataset_db=second_db)
                self.assertIn("publish lock", str(ctx.exception))
            finally:
                proceed.set()
                thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", holder)
        self.assertEqual(holder["result"]["result"], "published")
        self.assert_no_lock()

    # -- safety net --------------------------------------------------------
    def test_real_project_current_json_is_never_touched(self):
        real_current = paths.PROJECT_ROOT / "data" / "current.json"
        before = real_current.read_bytes() if real_current.exists() else None
        db = self.make_db("ds-safety")
        manifest.publish_dataset("ds-safety", dataset_db=db)
        after = real_current.read_bytes() if real_current.exists() else None
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
