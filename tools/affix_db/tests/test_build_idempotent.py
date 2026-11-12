"""Reproducibility / idempotency tests for the offline build."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import pathlib
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _support  # noqa: E402

from affix_db.build import build  # noqa: E402
from affix_db.validate import validate_db  # noqa: E402



#: Artifacts that record where the source snapshot lived on the machine that
#: produced the delivered build. ``data/affix-db`` is frozen (its byte hashes are
#: pinned by ``mapping-manifest.json`` / the delivered ``manifest.json``), so a
#: rebuild performed from a different checkout cannot reproduce those recorded
#: absolute origin paths without rewriting delivered data, which is forbidden.
#: For these artifacts the byte-identity check compares the JSON with absolute
#: path literals normalised, and still requires every other field to match.
LOCATION_DEPENDENT_ARTIFACTS = {"normalized/source-extraction.json"}

_ABS_PATH = re.compile(r"[A-Za-z]:[\\/][^\"]*|/(?:home|Users|mnt)/[^\"]*")


def _normalised_payload(path: pathlib.Path) -> object:
    """Load JSON and replace recorded absolute path literals with a placeholder."""
    payload = json.loads(path.read_text(encoding="utf-8"))

    def scrub(node):
        if isinstance(node, dict):
            return {key: scrub(value) for key, value in node.items()}
        if isinstance(node, list):
            return [scrub(value) for value in node]
        if isinstance(node, str) and _ABS_PATH.search(node):
            return _ABS_PATH.sub("<abs-path>", node)
        return node

    return scrub(payload)

def _sha256(path: pathlib.Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


@unittest.skipUnless(_support.SNAPSHOT_DIR.is_dir(), "frozen source snapshot not present")
class BuildReproducibilityTests(unittest.TestCase):
    def test_rebuild_is_byte_identical_to_delivered_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = build(_support.SNAPSHOT_DIR, tmp, progress=lambda message: None)
            self.assertEqual(result.manifest["integrity_problems"], [])
            if _support.MANIFEST_PATH.is_file():
                delivered = json.loads(_support.MANIFEST_PATH.read_text(encoding="utf-8"))
                delivered_hashes = {item["path"]: item["sha256"] for item in delivered["artifacts"]}
                rebuilt_root = pathlib.Path(tmp)
                delivered_root = _support.DATA_DIR
                for item in result.manifest["artifacts"]:
                    with self.subTest(path=item["path"]):
                        self.assertIn(item["path"], delivered_hashes)
                        if item["path"] in LOCATION_DEPENDENT_ARTIFACTS:
                            self.assertEqual(
                                _normalised_payload(rebuilt_root / item["path"]),
                                _normalised_payload(delivered_root / item["path"]),
                                "records the original machine's absolute origin path; "
                                "compared with path literals normalised",
                            )
                        else:
                            self.assertEqual(item["sha256"], delivered_hashes[item["path"]])
            # A second build in the same process must be byte-identical too.
            second_root = pathlib.Path(tmp) / "second"
            second = build(_support.SNAPSHOT_DIR, second_root, progress=lambda message: None)
            first_hashes = {item["path"]: item["sha256"] for item in result.manifest["artifacts"]}
            for item in second.manifest["artifacts"]:
                with self.subTest(path=item["path"]):
                    self.assertEqual(item["sha256"], first_hashes[item["path"]])

    def test_rebuilt_db_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = build(_support.SNAPSHOT_DIR, tmp, progress=lambda message: None)
            report = validate_db(result.sqlite_path, pathlib.Path(tmp) / "manifest.json")
            self.assertTrue(report.ok, msg=", ".join(check.name for check in report.failures))

    def test_sqlite_is_the_canonical_source_and_json_is_smaller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = build(_support.SNAPSHOT_DIR, tmp, progress=lambda message: None)
            root = pathlib.Path(tmp)
            sqlite_size = result.sqlite_path.stat().st_size
            json_total = sum(path.stat().st_size for path in (root / "normalized" / "index").glob("*.json"))
            self.assertGreater(sqlite_size, 10_000_000)
            self.assertLess(json_total, sqlite_size)
            # Full range table is NOT exported as one flat JSON file.
            self.assertFalse((root / "normalized" / "affix-base-ranges.json").exists())


if __name__ == "__main__":
    unittest.main()
