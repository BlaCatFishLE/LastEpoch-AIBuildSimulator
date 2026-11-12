"""Fetch cache integrity: a body that does not match meta.sha256 is not a cache.

workguide §13 row「资源损坏」/「HTTP 故障」: hash mismatch, truncated response or
missing body must be rejected, and offline mode must not silently use it.
304 handling itself is covered by ``test_fetch.py``; this file adds the
corruption edges (``Fetcher.cached_result`` + ``offline`` behaviour).

No network is used in these tests.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from le_data.fetch import Fetcher, OfflineError, SourcePolicy  # noqa: E402

BODY = b'{"synthetic": true, "note": "fixture, not real data"}'


class CacheIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fetcher = Fetcher(
            "synthetic-source",
            self.tmp,
            "synthetic-snap-cache",
            policy=SourcePolicy(min_interval=0, max_retries=0),
            offline=True,
        )

    def _seed(self, *, sha256: str | None = None, body: bytes = BODY) -> None:
        body_path = self.fetcher.body_path("page")
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(body)
        meta = {
            "url": "https://example.invalid/page",
            "final_url": "https://example.invalid/page",
            "resource_name": "page",
            "path": str(body_path),
            "sha256": sha256 if sha256 is not None else hashlib.sha256(body).hexdigest(),
            "size": len(body),
            "content_type": "application/json",
            "http_status": 200,
            "fetched_at": "2026-11-01T00:00:00+00:00",
            "fetch_status": "fetched",
        }
        self.fetcher.meta_path("page").write_text(
            json.dumps(meta), encoding="utf-8"
        )

    def test_valid_cache_is_reused_offline(self):
        self._seed()
        cached = self.fetcher.cached_result("page")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.fetch_status, "cache_hit")
        result = self.fetcher.fetch("https://example.invalid/page", "page")
        self.assertEqual(result.fetch_status, "cache_hit")
        self.assertEqual(Path(result.path).read_bytes(), BODY)

    def test_tampered_body_is_not_a_valid_cache(self):
        self._seed()
        self.fetcher.body_path("page").write_bytes(BODY + b"tampered")
        self.assertIsNone(self.fetcher.cached_result("page"))

    def test_same_size_body_change_is_detected(self):
        self._seed()
        changed = bytearray(BODY)
        changed[0] = ord("[")
        self.fetcher.body_path("page").write_bytes(bytes(changed))
        self.assertIsNone(self.fetcher.cached_result("page"))

    def test_truncated_body_is_not_a_valid_cache(self):
        self._seed()
        self.fetcher.body_path("page").write_bytes(BODY[: len(BODY) // 2])
        self.assertIsNone(self.fetcher.cached_result("page"))

    def test_meta_sha_mismatch_rejects_cache(self):
        self._seed(sha256="0" * 64)
        self.assertIsNone(self.fetcher.cached_result("page"))

    def test_missing_body_rejects_cache(self):
        self._seed()
        self.fetcher.body_path("page").unlink()
        self.assertIsNone(self.fetcher.cached_result("page"))

    def test_missing_meta_rejects_cache(self):
        self._seed()
        self.fetcher.meta_path("page").unlink()
        self.assertIsNone(self.fetcher.cached_result("page"))

    def test_offline_fetch_refuses_corrupted_cache(self):
        self._seed()
        self.fetcher.body_path("page").write_bytes(BODY + b"corrupted")
        with self.assertRaises(OfflineError):
            self.fetcher.fetch("https://example.invalid/page", "page")

    def test_offline_fetch_without_any_cache_raises(self):
        with self.assertRaises(OfflineError):
            self.fetcher.fetch("https://example.invalid/page", "page")


if __name__ == "__main__":
    unittest.main()
