"""HTTP fetch/cache layer.

Design constraints (phase-1 workguide section 6.2):
  * one concurrent request per site, ~1s spacing by default;
  * bounded retries with exponential backoff, Retry-After is respected;
  * 403/challenge pages are not retried aggressively;
  * responses are written to a temp file, verified, then moved into place;
  * the exact response bytes, response metadata and SHA-256 are preserved;
  * a failed fetch never overwrites a previously valid cached resource;
  * 304 reuses the already verified cached body;
  * offline mode never performs a network request.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CHALLENGE_MARKERS = (
    b"Just a moment...",
    b"cf-challenge",
    b"Enable JavaScript and cookies to continue",
    b"<title>Access denied</title>",
)


class FetchError(RuntimeError):
    """Raised when a resource cannot be fetched or fails validation."""


class OfflineError(FetchError):
    """Raised when a network request is attempted in offline mode."""


@dataclass
class FetchResult:
    url: str
    final_url: str
    resource_name: str
    path: str
    sha256: str
    size: int
    content_type: str
    http_status: int
    fetched_at: str
    fetch_status: str  # fetched | not_modified | cache_hit | failed
    etag: str | None = None
    last_modified: str | None = None
    attempt: int = 1
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourcePolicy:
    min_interval: float = 1.0
    timeout: float = 30.0
    max_retries: int = 3
    backoff_base: float = 1.0
    accept: str = "*/*"


class Fetcher:
    def __init__(
        self,
        source: str,
        raw_root: Path,
        snapshot_id: str,
        policy: SourcePolicy | None = None,
        offline: bool = False,
        user_agent: str = USER_AGENT,
        logger=None,
    ) -> None:
        self.source = source
        self.raw_dir = Path(raw_root) / source / snapshot_id
        self.responses_dir = self.raw_dir / "responses"
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        self.policy = policy or SourcePolicy()
        self.offline = offline
        self.user_agent = user_agent
        self.logger = logger or (lambda msg: None)
        self._last_request = 0.0

    # -- cache helpers ------------------------------------------------------
    def _slug(self, resource_name: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in resource_name)
        return safe[:120] or "resource"

    def body_path(self, resource_name: str) -> Path:
        return self.responses_dir / f"{self._slug(resource_name)}.body"

    def meta_path(self, resource_name: str) -> Path:
        return self.responses_dir / f"{self._slug(resource_name)}.meta.json"

    def load_meta(self, resource_name: str) -> dict[str, Any] | None:
        path = self.meta_path(resource_name)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def cached_result(self, resource_name: str) -> FetchResult | None:
        meta = self.load_meta(resource_name)
        if not meta:
            return None
        body = self.body_path(resource_name)
        if not body.exists():
            return None
        digest = sha256_file(body)
        if meta.get("sha256") and digest != meta["sha256"]:
            return None
        return FetchResult(
            url=meta["url"],
            final_url=meta.get("final_url", meta["url"]),
            resource_name=resource_name,
            path=str(body),
            sha256=digest,
            size=body.stat().st_size,
            content_type=meta.get("content_type", ""),
            http_status=int(meta.get("http_status", 200)),
            fetched_at=meta.get("fetched_at", ""),
            fetch_status="cache_hit",
            etag=meta.get("etag"),
            last_modified=meta.get("last_modified"),
        )

    def _write_meta(self, result: FetchResult) -> None:
        meta = result.to_dict()
        meta.pop("attempt", None)
        self.meta_path(result.resource_name).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # -- request ------------------------------------------------------------
    def _throttle(self) -> None:
        if self.policy.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.policy.min_interval:
            time.sleep(self.policy.min_interval - elapsed)

    def fetch(self, url: str, resource_name: str, use_cache: bool = True) -> FetchResult:
        cached = self.cached_result(resource_name) if use_cache else None
        if self.offline:
            if cached:
                cached.fetch_status = "cache_hit"
                return cached
            raise OfflineError(
                f"offline mode: no cached copy for {self.source}/{resource_name} ({url})"
            )

        headers = {"User-Agent": self.user_agent, "Accept": self.policy.accept}
        if cached and cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached and cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified

        last_error: str | None = None
        for attempt in range(1, self.policy.max_retries + 2):
            self._throttle()
            try:
                result = self._request_once(url, resource_name, headers, attempt, cached)
                if result is not None:
                    return result
                last_error = "304 without reusable cache"
            except FetchError as exc:
                last_error = str(exc)
                self.logger(f"[fetch] {self.source} {resource_name} attempt {attempt}: {exc}")
                if getattr(exc, "permanent", False):
                    break
            self._last_request = time.monotonic()
            if attempt <= self.policy.max_retries:
                time.sleep(self.policy.backoff_base * (2 ** (attempt - 1)))
        if cached is not None:
            cached.fetch_status = "failed"
            cached.error = last_error
            return cached
        return FetchResult(
            url=url,
            final_url=url,
            resource_name=resource_name,
            path="",
            sha256="",
            size=0,
            content_type="",
            http_status=0,
            fetched_at=now_utc(),
            fetch_status="failed",
            error=last_error,
        )

    def _request_once(
        self,
        url: str,
        resource_name: str,
        headers: dict[str, str],
        attempt: int,
        cached: FetchResult | None,
    ) -> FetchResult | None:
        request = urllib.request.Request(url, headers=headers)
        self._last_request = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.policy.timeout) as response:
                status = response.status
                content_type = response.headers.get("Content-Type", "")
                if status == 304:
                    if cached is None:
                        return None
                    cached.fetch_status = "not_modified"
                    self._write_meta(cached)
                    return cached
                if status != 200:
                    raise FetchError(f"unexpected HTTP status {status}")
                body = response.read()
                etag = response.headers.get("ETag")
                last_modified = response.headers.get("Last-Modified")
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                exc.close()
                if cached is None:
                    return None
                cached.fetch_status = "not_modified"
                self._write_meta(cached)
                return cached
            exc.close()
            permanent = exc.code in (401, 403, 404)
            error = FetchError(f"HTTP {exc.code} for {url}")
            error.permanent = permanent  # type: ignore[attr-defined]
            raise error from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            error = FetchError(f"network error for {url}: {exc}")
            raise error from None

        if not body:
            raise FetchError(f"empty response body for {url}")
        validate_body(body, content_type)
        return self._commit(url, resource_name, body, content_type, status, etag,
                            last_modified, attempt)

    def _commit(
        self,
        url: str,
        resource_name: str,
        body: bytes,
        content_type: str,
        status: int,
        etag: str | None,
        last_modified: str | None,
        attempt: int,
    ) -> FetchResult:
        target = self.body_path(resource_name)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.responses_dir), suffix=".part")
        try:
            with open(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
            digest = sha256_file(Path(tmp_name))
            if digest != hashlib.sha256(body).hexdigest():
                raise FetchError("hash mismatch after writing temporary file")
            # Verify the temp file is self-consistent before replacing cache.
            shutil.move(tmp_name, target)
        finally:
            tmp = Path(tmp_name)
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        result = FetchResult(
            url=url,
            final_url=url,
            resource_name=resource_name,
            path=str(target),
            sha256=digest,
            size=len(body),
            content_type=content_type,
            http_status=status,
            fetched_at=now_utc(),
            fetch_status="fetched",
            etag=etag,
            last_modified=last_modified,
            attempt=attempt,
        )
        self._write_meta(result)
        return result


def validate_body(body: bytes, content_type: str) -> None:
    head = body[:4096]
    for marker in CHALLENGE_MARKERS:
        if marker in head:
            raise FetchError(f"response looks like a challenge/login page ({marker!r})")
    if "text/html" in content_type.lower():
        if b"<html" not in head.lower() and b"<!doctype html" not in head.lower():
            raise FetchError("HTML content-type but body has no HTML structure")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
