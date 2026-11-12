"""Offline tests for the LE Tools source adapter (P2-5).

No real network is used.  The happy path reads the archived real payload; every
network branch (HTML/JSON/missing-data classification, HTTP errors, retries) is
exercised through ``unittest.mock.patch`` on ``urllib.request.urlopen`` with a fake
response object, so the module's error classification is verified deterministically.

Run from ``tools/build_import``::

    & $py -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from le_build.paths import BuildPaths, sha256_bytes
from le_build.sources import letools

REPO = Path(__file__).resolve().parents[3]
DISCOVERY = REPO / "reports" / "discovery" / "phase-2-o3ZRm9my"
FIXTURE = DISCOVERY / "build-response.json"
CAPTURE_META = DISCOVERY / "capture-meta.json"

TARGET_VERSION = "1.5.2"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

class FakeResponse:
    """Minimal urllib response double with the attributes ``fetch_build`` reads."""

    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
        url: str = "https://www.lastepochtools.com/api/public/build_data/o3ZRm9my",
        chunk_size: int | None = None,
    ) -> None:
        self._body = body
        self._offset = 0
        self._chunk_size = chunk_size
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._url = url

    def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._body):
            return b""
        limit = size if size and size > 0 else len(self._body) - self._offset
        if self._chunk_size:
            limit = min(limit, self._chunk_size)
        chunk = self._body[self._offset : self._offset + limit]
        self._offset += len(chunk)
        return chunk

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _http_error(status: int, *, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError(
        "https://www.lastepochtools.com/api/public/build_data/o3ZRm9my",
        status,
        f"status {status}",
        headers,
        None,
    )


class ShareUrlTest(unittest.TestCase):
    def test_accepts_real_planner_urls(self) -> None:
        cases = [
            ("https://www.lastepochtools.com/planner/o3ZRm9my", "o3ZRm9my"),
            ("https://lastepochtools.com/planner/o3ZRm9my", "o3ZRm9my"),
            ("http://www.lastepochtools.com/planner/o3ZRm9my", "o3ZRm9my"),
            ("https://www.lastepochtools.com/planner/o3ZRm9my/", "o3ZRm9my"),
            ("  https://www.lastepochtools.com/planner/o3ZRm9my  ", "o3ZRm9my"),
            # Query strings and fragments are transport noise: dropped.
            ("https://www.lastepochtools.com/planner/o3ZRm9my?x=1&y=2#frag", "o3ZRm9my"),
            # Case is preserved in the share id, normalised only in the host.
            ("https://WWW.LastEpochTools.com/planner/O3zRM9My", "O3zRM9My"),
        ]
        for url, share_id in cases:
            parsed = letools.parse_share_url(url)
            self.assertEqual(parsed["share_id"], share_id, url)
            self.assertIn(parsed["host"], letools.SUPPORTED_HOSTS, url)
            self.assertEqual(parsed["host"], parsed["host"].lower(), url)
            self.assertEqual(
                parsed["normalized_url"],
                f"https://www.lastepochtools.com/planner/{share_id}",
                url,
            )
            self.assertEqual(
                parsed["api_url"],
                f"https://www.lastepochtools.com/api/public/build_data/{share_id}",
                url,
            )

    def test_rejects_non_planner_urls(self) -> None:
        invalid = [
            "",
            "   ",
            None,
            123,
            "not-a-url",
            "ftp://www.lastepochtools.com/planner/o3ZRm9my",
            "file:///tmp/o3ZRm9my",
            "https://evil.example/planner/o3ZRm9my",
            "https://www.lastepochtools.com/build/o3ZRm9my",
            "https://www.lastepochtools.com/planner/ab",  # share ids are >= 3 chars
            "https://www.lastepochtools.com/planner/o3ZRm9my/extra",
            "https://www.lastepochtools.com/planner/",
            "https://www.lastepochtools.com/planner/a b c",
        ]
        for url in invalid:
            with self.assertRaises(letools.UrlError, msg=repr(url)):
                letools.parse_share_url(url)


class ArchivedPayloadTest(unittest.TestCase):
    def test_archived_fixture_loads_with_recorded_hash(self) -> None:
        meta = json.loads(CAPTURE_META.read_text(encoding="utf-8"))
        record = letools.load_archived_file(FIXTURE)
        self.assertEqual(record["mode"], "file")
        self.assertEqual(record["raw_sha256"], meta["raw_sha256"])
        self.assertEqual(record["raw_sha256"], sha256_bytes(FIXTURE.read_bytes()))
        self.assertIsNone(record["http_status"])
        self.assertIsNone(record["api_url"])
        self.assertEqual(record["file_path"], str(FIXTURE))
        document = record["document"]
        self.assertIsInstance(document["data"], dict)
        self.assertEqual(document["data"]["dataVersion"], "Version 1.5.2")
        self.assertEqual(document["share_id"] if "share_id" in document else None, None)

    def test_archived_payload_errors_are_classified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="le_source_test_") as tmp:
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaises(letools.FetchError) as caught:
                letools.load_archived_file(broken)
            self.assertEqual(caught.exception.code, "json_error")

            array = Path(tmp) / "array.json"
            array.write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertRaises(letools.FetchError) as caught:
                letools.load_archived_file(array)
            # A non-object document has no object `data` field either.
            self.assertEqual(caught.exception.code, "missing_data")

            no_data = Path(tmp) / "no-data.json"
            no_data.write_text('{"foo": 1}', encoding="utf-8")
            with self.assertRaises(letools.FetchError) as caught:
                letools.load_archived_file(no_data)
            self.assertEqual(caught.exception.code, "missing_data")

    def test_offline_load_does_not_touch_network(self) -> None:
        def _forbidden(*args, **kwargs):
            raise AssertionError("offline replay attempted network I/O")

        with mock.patch("urllib.request.urlopen", side_effect=_forbidden), mock.patch(
            "socket.socket", side_effect=_forbidden
        ):
            record = letools.load_archived_file(FIXTURE)
            versions = letools.detect_versions(record["document"])
            compatibility = letools.classify_versions(versions, TARGET_VERSION)
            draft = letools.build_draft(record, share_id="o3ZRm9my")
            self.assertEqual(compatibility["status"], "matched")
            self.assertIn("equipment", draft["data"])


class VersionDetectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.record = letools.load_archived_file(FIXTURE)

    def test_real_payload_versions_are_normalised_and_match_target(self) -> None:
        versions = letools.detect_versions(self.record["document"])
        self.assertEqual(versions["protocol_version"], letools.PROTOCOL_VERSION)
        self.assertEqual(versions["raw"]["dataVersion"], "Version 1.5.2")
        self.assertEqual(versions["normalized"]["dataVersion"], "1.5.2")
        self.assertEqual(versions["normalized"]["data_version"], "1.5.2")
        self.assertEqual(versions["normalized"]["created_for_build"], "1.5.2")
        compatibility = letools.classify_versions(versions, TARGET_VERSION)
        self.assertEqual(compatibility["status"], "matched")
        self.assertEqual(compatibility["problems"], [])
        self.assertEqual(compatibility["warnings"], [])
        self.assertEqual(compatibility["target_game_version"], TARGET_VERSION)

    def _classify_document(self, document: dict) -> dict:
        return letools.classify_versions(letools.detect_versions(document), TARGET_VERSION)

    def test_mismatched_versions_are_problems(self) -> None:
        document = json.loads(FIXTURE.read_text(encoding="utf-8"))
        document["data"]["dataVersion"] = "Version 1.4.0"
        document["data_version"] = "Version 1.4.0"
        compatibility = self._classify_document(document)
        self.assertEqual(compatibility["status"], "mismatched")
        self.assertEqual(len(compatibility["problems"]), 2)
        self.assertTrue(
            any("data.dataVersion" in problem for problem in compatibility["problems"])
        )
        self.assertTrue(any("data_version" in problem for problem in compatibility["problems"]))

    def test_missing_data_version_is_a_problem(self) -> None:
        document = json.loads(FIXTURE.read_text(encoding="utf-8"))
        document["data"].pop("dataVersion")
        compatibility = self._classify_document(document)
        self.assertEqual(compatibility["status"], "mismatched")
        self.assertTrue(
            any("data.dataVersion is missing" in problem for problem in compatibility["problems"])
        )

    def test_unparseable_version_is_a_problem(self) -> None:
        document = json.loads(FIXTURE.read_text(encoding="utf-8"))
        document["data"]["dataVersion"] = "garbage"
        compatibility = self._classify_document(document)
        self.assertEqual(compatibility["status"], "mismatched")
        self.assertTrue(
            any("data.dataVersion is missing or unparseable" in problem
                for problem in compatibility["problems"])
        )

    def test_creation_version_mismatch_is_only_a_warning(self) -> None:
        document = json.loads(FIXTURE.read_text(encoding="utf-8"))
        document["created_for_build"] = "Version 1.4.0"
        compatibility = self._classify_document(document)
        self.assertEqual(compatibility["status"], "matched")
        self.assertEqual(compatibility["problems"], [])
        self.assertEqual(len(compatibility["warnings"]), 1)
        self.assertIn("created_for_build", compatibility["warnings"][0])

    def test_version_variants_via_archived_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="le_source_versions_") as tmp:
            document = json.loads(FIXTURE.read_text(encoding="utf-8"))
            document["data"]["dataVersion"] = "Version 1.4.0"
            document["data_version"] = "Version 1.4.0"
            path = Path(tmp) / "build-response-1.4.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            record = letools.load_archived_file(path)
            compatibility = letools.classify_versions(
                letools.detect_versions(record["document"]), TARGET_VERSION
            )
            self.assertEqual(compatibility["status"], "mismatched")


class FetchClassificationTest(unittest.TestCase):
    URL = "https://www.lastepochtools.com/api/public/build_data/o3ZRm9my"

    def _fetch(self, response, **kwargs):
        with mock.patch("urllib.request.urlopen", return_value=response) as patched:
            record = letools.fetch_build("o3ZRm9my", **kwargs)
        return record, patched

    def test_valid_json_fetch_is_archived_with_hash(self) -> None:
        body = FIXTURE.read_bytes()
        record, patched = self._fetch(FakeResponse(body))
        self.assertEqual(record["mode"], "network")
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["api_url"], self.URL)
        self.assertEqual(record["raw_sha256"], sha256_bytes(body))
        self.assertEqual(record["raw_bytes"], body)
        self.assertIsInstance(record["document"]["data"], dict)
        self.assertEqual(record["attempts"], [{"attempt": 1, "started_at": mock.ANY, "http_status": 200}])
        self.assertNotIn("warnings", record)
        self.assertEqual(patched.call_count, 1)

    def test_html_response_is_classified(self) -> None:
        body = b"<!DOCTYPE html><html><head><title>oops</title></head></html>"
        with self.assertRaises(letools.FetchError) as caught:
            self._fetch(FakeResponse(body, content_type="text/html"))
        self.assertEqual(caught.exception.code, "html_response")
        self.assertEqual(caught.exception.fetch["http_status"], 200)

    def test_invalid_json_is_classified(self) -> None:
        with self.assertRaises(letools.FetchError) as caught:
            self._fetch(FakeResponse(b"{truncated"))
        self.assertEqual(caught.exception.code, "json_error")

    def test_missing_data_object_is_classified(self) -> None:
        with self.assertRaises(letools.FetchError) as caught:
            self._fetch(FakeResponse(b'{"hello": "world"}'))
        self.assertEqual(caught.exception.code, "missing_data")

    def test_non_object_json_is_classified(self) -> None:
        with self.assertRaises(letools.FetchError) as caught:
            self._fetch(FakeResponse(b'"a string"'))
        self.assertEqual(caught.exception.code, "json_error")

    def test_unexpected_content_type_is_recorded_as_warning(self) -> None:
        record, _ = self._fetch(FakeResponse(FIXTURE.read_bytes(), content_type="text/plain"))
        self.assertEqual(record["mode"], "network")
        self.assertTrue(record.get("warnings"))
        self.assertIn("content type", record["warnings"][0])

    def test_payload_larger_than_limit_is_rejected(self) -> None:
        with self.assertRaises(letools.FetchError) as caught:
            self._fetch(FakeResponse(b"x" * 4096), max_bytes=1024)
        self.assertEqual(caught.exception.code, "payload_too_large")

    def test_http_404_is_not_retried(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(404)) as patched:
            with self.assertRaises(letools.FetchError) as caught:
                letools.fetch_build("o3ZRm9my")
        self.assertEqual(caught.exception.code, "http_404_not_found")
        self.assertEqual(caught.exception.fetch["http_status"], 404)
        self.assertEqual(len(caught.exception.fetch["attempts"]), 1)
        self.assertEqual(patched.call_count, 1)

    def test_http_500_is_retried_then_reported(self) -> None:
        sleeps: list[float] = []
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(500)) as patched:
            with mock.patch(
                "le_build.sources.letools.time.sleep", side_effect=lambda s: sleeps.append(s)
            ):
                with self.assertRaises(letools.FetchError) as caught:
                    letools.fetch_build("o3ZRm9my", retries=1)
        self.assertEqual(caught.exception.code, "http_5xx")
        self.assertEqual(len(caught.exception.fetch["attempts"]), 2)
        self.assertEqual(patched.call_count, 2)
        self.assertEqual(len(sleeps), 1)

    def test_timeout_is_retried_then_reported(self) -> None:
        with mock.patch(
            "urllib.request.urlopen", side_effect=TimeoutError("timed out")
        ) as patched:
            with mock.patch("le_build.sources.letools.time.sleep"):
                with self.assertRaises(letools.FetchError) as caught:
                    letools.fetch_build("o3ZRm9my", retries=1)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(patched.call_count, 2)

    def test_rate_limit_with_retry_after_is_retried(self) -> None:
        sleeps: list[float] = []
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(429, retry_after="7")):
            with mock.patch(
                "le_build.sources.letools.time.sleep", side_effect=lambda s: sleeps.append(s)
            ):
                with self.assertRaises(letools.FetchError) as caught:
                    letools.fetch_build("o3ZRm9my", retries=1)
        self.assertEqual(caught.exception.code, "http_429_rate_limited")
        self.assertEqual(sleeps, [7.0])


class DraftAndArchiveTest(unittest.TestCase):
    def test_build_draft_preserves_source_and_reports_unknown_fields(self) -> None:
        document = json.loads(FIXTURE.read_text(encoding="utf-8"))
        document["phase2TopField"] = {"a": 1}
        document["tags"] = ["tag-a"]
        document["data"]["phase2DataField"] = [1, 2]
        record = {
            "mode": "file",
            "file_path": str(FIXTURE),
            "raw_sha256": sha256_bytes(FIXTURE.read_bytes()),
            "raw_bytes": FIXTURE.read_bytes(),
            "document": document,
            "http_status": None,
            "api_url": None,
        }
        draft = letools.build_draft(
            record, share_id="o3ZRm9my", requested_url="https://www.lastepochtools.com/planner/o3ZRm9my"
        )
        self.assertEqual(draft["share_id"], "o3ZRm9my")
        self.assertEqual(draft["unexpected_top_level_fields"], ["phase2TopField"])
        self.assertEqual(draft["unexpected_data_fields"], ["phase2DataField"])
        self.assertEqual(draft["data"]["phase2DataField"], [1, 2])
        self.assertEqual(draft["document"]["phase2TopField"], {"a": 1})
        self.assertEqual(draft["versions"]["normalized"]["dataVersion"], "1.5.2")
        # The raw transport bytes are deliberately not copied into the draft record.
        self.assertNotIn("raw_bytes", draft["fetch"])
        self.assertNotIn("document", draft["fetch"])

    def test_archive_fetch_is_content_addressed_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="le_archive_test_") as tmp:
            paths = BuildPaths(Path(tmp))
            body = FIXTURE.read_bytes()
            fetch = {
                "mode": "file",
                "file_path": str(FIXTURE),
                "raw_sha256": sha256_bytes(body),
                "raw_bytes": body,
                "http_status": None,
                "api_url": None,
            }
            first = letools.archive_fetch(paths, "o3ZRm9my", fetch)
            raw_path = Path(first["raw_path"])
            self.assertTrue(raw_path.is_file())
            self.assertEqual(raw_path.read_bytes(), body)
            self.assertEqual(raw_path.name, sha256_bytes(body) + ".json")
            self.assertTrue(Path(first["fetch_path"]).is_file())

            second = letools.archive_fetch(paths, "o3ZRm9my", fetch)
            raw_files = list(paths.raw_dir("o3ZRm9my").iterdir())
            self.assertEqual(len(raw_files), 1)
            self.assertEqual(raw_files[0].read_bytes(), body)
            self.assertEqual(second["raw_sha256"], first["raw_sha256"])
            # Attempt metadata is per-fetch; the raw payload is stored once per hash.
            self.assertEqual(len(list(paths.fetches_dir("o3ZRm9my").iterdir())), 2)


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
