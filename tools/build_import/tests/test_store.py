"""Independent tests for the immutable revision store (P2-5).

Everything here runs in ``tempfile.TemporaryDirectory`` roots; no test writes under
``data/builds``.  The store is exercised through its public API only
(``publish_revision`` / ``load_build`` / ``cleanup_temp_revisions`` ...), with the
content-identity rules taken from the module contract, not re-derived from a stored
revision.

Run from ``tools/build_import``::

    & $py -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from le_build.paths import BuildPaths
from le_build.store import (
    RevisionConflict,
    StoreError,
    build_ref_parts,
    cleanup_temp_revisions,
    content_hash,
    content_view,
    list_revisions,
    load_build,
    load_current,
    publish_revision,
    revision_id_for,
    write_failure_report,
)

SHARE_ID = "p2simTest1"


def make_spec(*, level: int = 100, raw_sha256: str = "a" * 64, fetched_at: str = "t0") -> dict:
    """A small but structurally realistic BuildSpec-shaped dict for the store."""
    return {
        "build_spec_version": "build-spec-v1",
        "parser_version": "le-build-parser-v1",
        "mapping_version": "letools-catalog-map-v1",
        "meta": {
            "build_id": f"letools:{SHARE_ID}",
            "share_id": SHARE_ID,
            "share_url": f"https://www.lastepochtools.com/planner/{SHARE_ID}",
            "source": "letools",
            "protocol": {
                "protocol_version": "letools-build-data-v1",
                "versions": {"raw": {"dataVersion": "1.5.2"}, "normalized": {"dataVersion": "1.5.2"}},
            },
            "source_payload": {
                "raw_sha256": raw_sha256,
                "data_field_sha256": "d" * 64,
                "raw_path": "data/builds/letools/p2simTest1/raw/x.json",
                "attempt_id": "attempt-1",
                "fetch_mode": "file",
                "fetched_at": fetched_at,
            },
            "codec": {"codec_version": "letools-id-codec-v1"},
            "created_at": "2026-01-01T00:00:00+00:00",
            "idempotency": {"content_hash_scope": "stable fields only"},
        },
        "catalog": {"dataset_id": "phase1-dataset-002", "logical_content_sha256": "l" * 64},
        "character": {"level": level, "class": {"ref": {"name": "Mage"}}},
        "issues": [],
        "statuses": {"identity_resolution": "resolved"},
    }


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="le_store_test_")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.paths = BuildPaths(self.root)
        self.share_id = SHARE_ID

    # ------------------------------------------------------------- publishing
    def test_publish_creates_revision_files_and_current_pointer(self) -> None:
        spec = make_spec()
        result = publish_revision(
            self.paths,
            self.share_id,
            spec,
            artifacts={
                "import-report.json": {"report_version": "import-report-v1", "ok": True},
                "import-report.zh-CN.md": "# 报告\n",
            },
            published_at="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual(result["state"], "published")
        self.assertEqual(result["revision_seq"], 1)
        self.assertRegex(result["revision_id"], r"^rev-[0-9a-f]{16}$")
        self.assertEqual(result["revision_id"], revision_id_for(make_spec()))
        self.assertEqual(result["content_sha256"], content_hash(make_spec()))

        revision_dir = self.paths.revision_dir(self.share_id, result["revision_id"])
        for name in ("build.json", "import-report.json", "import-report.zh-CN.md"):
            self.assertTrue((revision_dir / name).is_file(), name)
        stored = json.loads((revision_dir / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["meta"]["revision"]["revision_id"], result["revision_id"])
        self.assertEqual(stored["meta"]["revision"]["content_sha256"], result["content_sha256"])
        self.assertEqual(stored["meta"]["revision"]["revision_seq"], 1)
        self.assertEqual(
            stored["meta"]["revision"]["published_at"], "2026-01-01T00:00:00+00:00"
        )
        # Artifacts stay byte-identical to what was published (markdown kept as text).
        self.assertEqual(
            (revision_dir / "import-report.zh-CN.md").read_text(encoding="utf-8"), "# 报告\n"
        )

        pointer = load_current(self.paths, self.share_id)
        self.assertIsNotNone(pointer)
        self.assertEqual(pointer["revision_id"], result["revision_id"])
        self.assertEqual(pointer["content_sha256"], result["content_sha256"])
        self.assertEqual(pointer["revision_seq"], 1)
        self.assertEqual(pointer["dataset_id"], "phase1-dataset-002")
        self.assertEqual(pointer["source_raw_sha256"], "a" * 64)

        loaded = load_build(self.paths, f"letools:{self.share_id}")
        self.assertTrue(loaded["is_current"])
        self.assertEqual(loaded["revision_id"], result["revision_id"])
        self.assertEqual(loaded["spec"]["character"]["level"], 100)

    def test_second_identical_publish_is_idempotent(self) -> None:
        first = publish_revision(self.paths, self.share_id, make_spec())
        pointer_path = self.paths.current_file(self.share_id)
        pointer_before = pointer_path.read_bytes()
        revision_dir = self.paths.revision_dir(self.share_id, first["revision_id"])
        build_before = (revision_dir / "build.json").read_bytes()

        second = publish_revision(
            self.paths,
            self.share_id,
            make_spec(),
            artifacts={"import-report.json": {"different": True}},
        )
        self.assertEqual(second["state"], "exists")
        self.assertEqual(second["revision_id"], first["revision_id"])
        self.assertEqual(second["content_sha256"], first["content_sha256"])
        self.assertEqual(second["revision_seq"], first["revision_seq"])
        self.assertEqual(list_revisions(self.paths, self.share_id), [first["revision_id"]])
        # Neither the pointer nor the immutable revision was rewritten.
        self.assertEqual(pointer_path.read_bytes(), pointer_before)
        self.assertEqual((revision_dir / "build.json").read_bytes(), build_before)

    def test_changed_content_publishes_a_new_revision_with_sequence(self) -> None:
        first = publish_revision(self.paths, self.share_id, make_spec(level=100))
        second = publish_revision(self.paths, self.share_id, make_spec(level=99))
        self.assertEqual(second["state"], "published")
        self.assertNotEqual(second["revision_id"], first["revision_id"])
        self.assertEqual(first["revision_seq"], 1)
        self.assertEqual(second["revision_seq"], 2)
        self.assertEqual(
            list_revisions(self.paths, self.share_id),
            sorted([first["revision_id"], second["revision_id"]]),
        )
        pointer = load_current(self.paths, self.share_id)
        self.assertEqual(pointer["revision_id"], second["revision_id"])
        self.assertEqual(pointer["previous_revision_id"], first["revision_id"])
        self.assertTrue(load_build(self.paths, self.share_id)["is_current"])
        older = load_build(
            self.paths, self.share_id, revision_id=first["revision_id"]
        )
        self.assertFalse(older["is_current"])
        self.assertEqual(older["spec"]["character"]["level"], 100)

    # ------------------------------------------------------- content identity
    def test_content_hash_ignores_volatile_import_metadata(self) -> None:
        base = make_spec()
        volatile = copy.deepcopy(base)
        volatile["meta"]["created_at"] = "2026-12-31T23:59:59+00:00"
        volatile["meta"]["attempt_id"] = "other-attempt"
        volatile["meta"]["revision"] = {"revision_id": "rev-deadbeefdeadbeef"}
        volatile["meta"]["import_attempt"] = {"attempt_id": "x", "mode": "network"}
        volatile["meta"]["previous_revision"] = {"revision_id": "rev-0000000000000000"}
        volatile["meta"]["source_payload"].update(
            {
                "attempt_id": "other-attempt",
                "raw_path": "data/builds/letools/other/raw/other.json",
                "fetched_at": "2026-02-02T02:02:02+00:00",
                "fetch_mode": "network",
                "http_status": 200,
                "api_url": "https://example.invalid/api",
            }
        )
        self.assertEqual(content_hash(volatile), content_hash(base))
        self.assertEqual(revision_id_for(volatile), revision_id_for(base))
        view = content_view(volatile)
        for key in ("attempt_id", "raw_path", "fetched_at", "fetch_mode", "http_status", "api_url"):
            self.assertNotIn(key, view["meta"]["source_payload"])
        self.assertNotIn("created_at", view["meta"])
        self.assertNotIn("import_attempt", view["meta"])
        self.assertNotIn("revision", view["meta"])
        self.assertNotIn("previous_revision", view["meta"])
        # The source payload hash remains part of the identity.
        self.assertIn("raw_sha256", view["meta"]["source_payload"])
        self.assertIn("data_field_sha256", view["meta"]["source_payload"])

    def test_content_hash_changes_when_build_content_changes(self) -> None:
        base = make_spec()
        for mutate in (
            lambda spec: spec["character"].__setitem__("level", 99),
            lambda spec: spec["meta"]["source_payload"].__setitem__("raw_sha256", "b" * 64),
            lambda spec: spec["meta"]["protocol"]["versions"]["normalized"].__setitem__(
                "dataVersion", "1.4.0"
            ),
            lambda spec: spec["statuses"].__setitem__("identity_resolution", "partial"),
            lambda spec: spec["catalog"].__setitem__("dataset_id", "phase1-dataset-001"),
        ):
            changed = copy.deepcopy(base)
            mutate(changed)
            self.assertNotEqual(content_hash(changed), content_hash(base), mutate)

    def test_content_hash_is_stable_under_dict_key_order(self) -> None:
        base = make_spec()
        reordered = json.loads(json.dumps(base))
        # Rebuild one nested dict with reversed insertion order.
        reordered["meta"] = dict(reversed(list(reordered["meta"].items())))
        self.assertEqual(content_hash(reordered), content_hash(base))

    # ------------------------------------------------------------ immutability
    def test_tampered_revision_is_detected_and_not_overwritten(self) -> None:
        spec = make_spec()
        first = publish_revision(self.paths, self.share_id, spec)
        revision_dir = self.paths.revision_dir(self.share_id, first["revision_id"])
        build_path = revision_dir / "build.json"
        tampered = json.loads(build_path.read_text(encoding="utf-8"))
        tampered["character"]["level"] = 1  # content silently edited after publish
        tampered_bytes = json.dumps(tampered, ensure_ascii=False, indent=2).encode("utf-8")
        build_path.write_bytes(tampered_bytes)
        pointer_before = self.paths.current_file(self.share_id).read_bytes()

        with self.assertRaises(RevisionConflict):
            publish_revision(self.paths, self.share_id, copy.deepcopy(spec))
        # The tampered file is not silently repaired or overwritten.
        self.assertEqual(build_path.read_bytes(), tampered_bytes)
        self.assertEqual(self.paths.current_file(self.share_id).read_bytes(), pointer_before)

    def test_interrupted_publish_rolls_back_and_keeps_old_pointer(self) -> None:
        first = publish_revision(self.paths, self.share_id, make_spec(level=100))
        pointer_before = self.paths.current_file(self.share_id).read_bytes()
        revisions_before = list_revisions(self.paths, self.share_id)

        with mock.patch("le_build.store.os.rename", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                publish_revision(self.paths, self.share_id, make_spec(level=99))

        self.assertEqual(list_revisions(self.paths, self.share_id), revisions_before)
        self.assertEqual(self.paths.current_file(self.share_id).read_bytes(), pointer_before)
        pointer = load_current(self.paths, self.share_id)
        self.assertEqual(pointer["revision_id"], first["revision_id"])
        self.assertEqual(pointer["revision_seq"], 1)
        # The finally-block must have removed the temporary revision directory.
        self.assertEqual(self.cleanup_temp_dirs(), [])

    def test_cleanup_temp_revisions_removes_only_orphans(self) -> None:
        first = publish_revision(self.paths, self.share_id, make_spec())
        revisions_dir = self.paths.revisions_dir(self.share_id)
        orphan = revisions_dir / ".tmp-rev-deadbeefdeadbeef-123-20260101T000000000000Z"
        orphan.mkdir()
        (orphan / "build.json").write_text("{}", encoding="utf-8")
        unrelated = revisions_dir / "not-a-revision"
        unrelated.mkdir()

        removed = cleanup_temp_revisions(self.paths, self.share_id)
        self.assertEqual(removed, [orphan.name])
        self.assertFalse(orphan.exists())
        self.assertTrue(unrelated.is_dir())
        self.assertEqual(list_revisions(self.paths, self.share_id), [first["revision_id"]])
        # Idempotent: nothing left to remove.
        self.assertEqual(cleanup_temp_revisions(self.paths, self.share_id), [])

    # --------------------------------------------------------------- recovery
    def test_failure_report_is_written_outside_revisions(self) -> None:
        path = write_failure_report(
            self.paths,
            self.share_id,
            "attempt-xyz",
            {"report_version": "import-failure-report-v1", "status": "incompatible"},
        )
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, "failure-report.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "incompatible")
        self.assertNotEqual(path.parent.parent.parent.name, "revisions")
        self.assertEqual(list_revisions(self.paths, self.share_id), [])

    def test_missing_store_state_raises_store_error(self) -> None:
        self.assertIsNone(load_current(self.paths, self.share_id))
        with self.assertRaises(StoreError):
            load_build(self.paths, f"letools:{self.share_id}")
        with self.assertRaises(StoreError):
            load_build(self.paths, f"letools:{self.share_id}", revision_id="rev-0000000000000000")

    def test_build_ref_parts(self) -> None:
        self.assertEqual(build_ref_parts("letools:o3ZRm9my"), ("letools", "o3ZRm9my"))
        self.assertEqual(build_ref_parts("o3ZRm9my"), ("letools", "o3ZRm9my"))
        self.assertEqual(build_ref_parts("a:b:c"), ("a", "b:c"))

    def test_revision_id_is_derived_from_canonical_content_hash(self) -> None:
        spec = make_spec()
        revision_id = revision_id_for(spec)
        self.assertRegex(revision_id, r"^rev-[0-9a-f]{16}$")
        self.assertEqual(revision_id, "rev-" + content_hash(spec)[:16])
        self.assertEqual(len(re.sub("^rev-", "", revision_id)), 16)

    # ---------------------------------------------------------------- helpers
    def cleanup_temp_dirs(self) -> list[str]:
        revisions_dir = self.paths.revisions_dir(self.share_id)
        if not revisions_dir.is_dir():
            return []
        return sorted(
            entry.name
            for entry in revisions_dir.iterdir()
            if entry.is_dir() and entry.name.startswith(".tmp-")
        )


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
