"""Tests for the fetch/cache layer using a local HTTP server."""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from le_data.fetch import (  # noqa: E402
    FetchError,
    Fetcher,
    OfflineError,
    SourcePolicy,
    validate_body,
)


class _State:
    mode = "ok"
    hits = 0
    etag = '"v1"'
    body = b'<html><body>hello</body></html>'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence test output
        pass

    def do_GET(self):  # noqa: N802
        _State.hits += 1
        if _State.mode == "challenge":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><title>Just a moment...</title></html>")
            return
        if _State.mode == "notfound":
            self.send_error(404)
            return
        if self.headers.get("If-None-Match") == _State.etag:
            self.send_response(304)
            self.send_header("ETag", _State.etag)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("ETag", _State.etag)
        self.end_headers()
        self.wfile.write(_State.body)


class FetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _State.mode = "ok"
        _State.etag = '"v1"'
        _State.hits = 0
        self.tmp = Path(tempfile.mkdtemp())

    def fetcher(self, offline=False):
        return Fetcher(
            "testsource",
            self.tmp,
            "snap-1",
            policy=SourcePolicy(min_interval=0, max_retries=2, backoff_base=0.0, timeout=5),
            offline=offline,
        )

    def test_fetch_writes_body_and_meta(self):
        result = self.fetcher().fetch(f"{self.base}/page", "page")
        self.assertEqual(result.fetch_status, "fetched")
        self.assertEqual(result.http_status, 200)
        self.assertTrue(Path(result.path).exists())
        self.assertEqual(Path(result.path).read_bytes(), _State.body)
        meta = Path(result.path).with_suffix(".meta.json")
        self.assertTrue(meta.exists())

    def test_cache_hit_does_not_refetch(self):
        fetcher = self.fetcher()
        fetcher.fetch(f"{self.base}/page", "page")
        hits_after_first = _State.hits
        result = fetcher.fetch(f"{self.base}/page", "page")
        # A conditional request is allowed once (304); the body is not re-sent.
        self.assertIn(result.fetch_status, ("cache_hit", "not_modified"))
        self.assertEqual(_State.hits, hits_after_first + 1)
        self.assertEqual(Path(result.path).read_bytes(), _State.body)

    def test_304_reuses_cached_body(self):
        fetcher = self.fetcher()
        fetcher.fetch(f"{self.base}/page", "page", use_cache=False)
        res = fetcher.fetch(f"{self.base}/page", "page")
        self.assertEqual(res.fetch_status, "not_modified")
        self.assertEqual(Path(res.path).read_bytes(), _State.body)

    def test_404_is_permanent_failure(self):
        _State.mode = "notfound"
        result = self.fetcher().fetch(f"{self.base}/missing", "missing")
        self.assertEqual(result.fetch_status, "failed")
        self.assertIn("404", result.error or "")

    def test_challenge_page_rejected(self):
        _State.mode = "challenge"
        result = self.fetcher().fetch(f"{self.base}/challenged", "challenged")
        self.assertEqual(result.fetch_status, "failed")
        self.assertIn("challenge", (result.error or "").lower())

    def test_offline_uses_cache(self):
        fetcher = self.fetcher()
        fetcher.fetch(f"{self.base}/page", "page")
        offline = self.fetcher(offline=True)
        result = offline.fetch(f"{self.base}/page", "page")
        self.assertEqual(result.fetch_status, "cache_hit")

    def test_offline_without_cache_raises(self):
        with self.assertRaises(OfflineError):
            self.fetcher(offline=True).fetch(f"{self.base}/page", "page")

    def test_failed_fetch_keeps_previous_cache(self):
        fetcher = self.fetcher()
        good = fetcher.fetch(f"{self.base}/page", "page")
        _State.mode = "notfound"
        bad = fetcher.fetch(f"{self.base}/page", "page")
        self.assertEqual(bad.fetch_status, "failed")
        self.assertTrue(Path(good.path).exists())
        self.assertEqual(Path(good.path).read_bytes(), _State.body)

    def test_validate_body_rejects_empty_and_challenge(self):
        with self.assertRaises(FetchError):
            validate_body(b"", "text/html")
        with self.assertRaises(FetchError):
            validate_body(b"<html>Just a moment...</html>", "text/html")
        validate_body(b'{"a":1}', "application/json")


if __name__ == "__main__":
    unittest.main()
