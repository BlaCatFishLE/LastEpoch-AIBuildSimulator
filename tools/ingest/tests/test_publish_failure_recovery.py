"""Publish failure recovery: refuse invalid datasets, never clobber current.

workguide §13 row「发布失败」: validation failure / interrupted write / existing
current must leave the old current pointer and old release usable.
``publish_dataset`` is exercised directly with all data/release directories
patched to a temporary directory; the real ``data/current.json`` is never written.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import le_data.paths as paths  # noqa: E402
import le_data.report.manifest as manifest  # noqa: E402
from le_data.store import (  # noqa: E402
    apply_migrations,
    connect,
    insert_row,
    upsert_dataset_meta,
)


def make_dataset_db(path: Path, dataset_id: str, status: str, entity_id: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    apply_migrations(conn)
    upsert_dataset_meta(conn, dataset_id, status=status)
    insert_row(
        conn,
        "entities",
        {
            "entity_id": entity_id,
            "kind": "skill",
            "canonical_name": entity_id,
            "record_status": "active",
            "primary_source": "tunklab",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    )
    conn.commit()
    conn.close()
    return path


class PublishFailureRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data_root = self.tmp / "data"
        for name in ("normalized", "releases"):
            (self.data_root / name).mkdir(parents=True, exist_ok=True)
        self.current_path = self.data_root / "current.json"
        self.current_path.write_text(
            json.dumps({"dataset_id": "previous", "logical_content_sha256": "old"}), encoding="utf-8"
        )
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(
            mock.patch.object(manifest, "NORMALIZED_DIR", self.data_root / "normalized")
        )
        stack.enter_context(
            mock.patch.object(manifest, "RELEASES_DIR", self.data_root / "releases")
        )
        stack.enter_context(mock.patch.object(paths, "DATA_DIR", self.data_root))

    def db_path(self, name: str) -> Path:
        return self.data_root / "normalized" / name / "data.sqlite"

    def test_publish_refuses_staging_status(self):
        db = make_dataset_db(self.db_path("ds-staging"), "ds-staging", "staging", "skill:a")
        before = self.current_path.read_bytes()
        with self.assertRaises(RuntimeError) as ctx:
            manifest.publish_dataset("ds-staging", dataset_db=db)
        self.assertIn("status is 'staging'", str(ctx.exception))
        self.assertEqual(self.current_path.read_bytes(), before)
        self.assertFalse((self.data_root / "releases" / "ds-staging").exists())

    def test_publish_refuses_partial_status(self):
        db = make_dataset_db(self.db_path("ds-partial"), "ds-partial", "partial", "skill:b")
        before = self.current_path.read_bytes()
        with self.assertRaises(RuntimeError) as ctx:
            manifest.publish_dataset("ds-partial", dataset_db=db)
        self.assertIn("status is 'partial'", str(ctx.exception))
        self.assertEqual(self.current_path.read_bytes(), before)
        self.assertFalse((self.data_root / "releases" / "ds-partial").exists())

    def test_publish_refuses_database_without_dataset_meta(self):
        db = self.db_path("ds-no-meta")
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(db)
        apply_migrations(conn)
        conn.commit()
        conn.close()
        before = self.current_path.read_bytes()
        with self.assertRaises(RuntimeError):
            manifest.publish_dataset("ds-no-meta", dataset_db=db)
        self.assertEqual(self.current_path.read_bytes(), before)

    def test_successful_publish_updates_current_and_writes_release(self):
        db = make_dataset_db(self.db_path("ds-ok"), "ds-ok", "valid", "skill:ok")
        output = manifest.publish_dataset("ds-ok", dataset_db=db)
        current = json.loads(self.current_path.read_text(encoding="utf-8"))
        self.assertEqual(current["dataset_id"], "ds-ok")
        self.assertEqual(output["current"]["dataset_id"], "ds-ok")
        release_db = self.data_root / "releases" / "ds-ok" / "data.sqlite"
        release_manifest = self.data_root / "releases" / "ds-ok" / "manifest.json"
        self.assertTrue(release_db.exists())
        self.assertTrue(release_manifest.exists())
        published = manifest.published_dataset_paths()
        self.assertEqual(published["dataset_id"], "ds-ok")
        self.assertTrue(Path(published["database"]).exists())

    def test_interrupted_release_keeps_current_and_previous_release(self):
        first = make_dataset_db(self.db_path("ds-first"), "ds-first", "valid", "skill:first")
        manifest.publish_dataset("ds-first", dataset_db=first)
        current_before = self.current_path.read_bytes()
        release_before = (self.data_root / "releases" / "ds-first" / "data.sqlite").read_bytes()

        second = make_dataset_db(self.db_path("ds-second"), "ds-second", "valid", "skill:second")
        real_sha = hashlib.sha256(second.read_bytes()).hexdigest()
        with mock.patch.object(
            manifest, "sha256_file", side_effect=[real_sha, "0" * 64]
        ):
            with self.assertRaises(RuntimeError) as ctx:
                manifest.publish_dataset("ds-second", dataset_db=second)
        self.assertIn("hash does not match", str(ctx.exception))
        # existing current pointer and existing release are untouched
        self.assertEqual(self.current_path.read_bytes(), current_before)
        self.assertEqual(
            (self.data_root / "releases" / "ds-first" / "data.sqlite").read_bytes(),
            release_before,
        )
        second_release = self.data_root / "releases" / "ds-second"
        self.assertFalse((second_release / "data.sqlite").exists())
        self.assertFalse((second_release / "manifest.json").exists())
        self.assertEqual(list(second_release.glob("*.tmp")) if second_release.exists() else [], [])

    def test_real_project_current_json_is_never_touched(self):
        """Safety net: patched tests must not write the real data/current.json."""
        real_current = paths.PROJECT_ROOT / "data" / "current.json"
        before = real_current.read_bytes() if real_current.exists() else None
        db = make_dataset_db(self.db_path("ds-safety"), "ds-safety", "valid", "skill:safety")
        manifest.publish_dataset("ds-safety", dataset_db=db)
        after = real_current.read_bytes() if real_current.exists() else None
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
