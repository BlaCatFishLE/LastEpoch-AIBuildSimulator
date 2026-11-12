"""LE Tools share-link source adapter: URL validation, fetch, archive, offline replay.

Only this module performs network I/O.  Everything downstream works on an already
fetched/archived payload so decode/resolve/validate/report stay fully offline.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .. import PROTOCOL_VERSION
from ..paths import sha256_bytes, write_json

SUPPORTED_HOSTS = frozenset({"www.lastepochtools.com", "lastepochtools.com"})
PLANNER_PATH_RE = re.compile(r"^/planner/(?P<share_id>[A-Za-z0-9_-]{3,64})/?$")
API_URL_TEMPLATE = "https://www.lastepochtools.com/api/public/build_data/{share_id}"
USER_AGENT = "LastEpoch-BuildSimulator/phase2-build-import (local, non-commercial research; +https://github.com/)"

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_RETRIES = 2
RETRY_BACKOFF_SECONDS = (1.0, 2.0)
MAX_RETRY_AFTER = 60.0

ERROR_CODES = {
    403: "http_403_forbidden",
    404: "http_404_not_found",
    429: "http_429_rate_limited",
}


class UrlError(ValueError):
    """Raised when a share URL is not an accepted LE Tools planner URL."""


class FetchError(RuntimeError):
    """Raised when the network fetch failed after classification."""

    def __init__(self, message: str, *, code: str, fetch: dict):
        super().__init__(message)
        self.code = code
        self.fetch = fetch


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_share_url(url: str) -> dict:
    """Validate an LE Tools planner URL and return its share id.

    Rejects non-http(s) schemes, unknown hosts, other paths and empty ids.  Query
    strings and fragments are dropped; the share id keeps its original casing.
    """
    if not isinstance(url, str) or not url.strip():
        raise UrlError("empty URL")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise UrlError(f"unsupported URL scheme {parts.scheme!r}: only http/https planner URLs are accepted")
    host = (parts.hostname or "").lower()
    if host not in SUPPORTED_HOSTS:
        raise UrlError(
            f"unsupported host {host!r}: only {', '.join(sorted(SUPPORTED_HOSTS))} planner URLs are accepted"
        )
    match = PLANNER_PATH_RE.match(parts.path)
    if not match:
        raise UrlError(
            f"unsupported planner path {parts.path!r}: expected /planner/{{shareId}}"
        )
    share_id = match.group("share_id")
    normalized = urlunsplit(("https", "www.lastepochtools.com", f"/planner/{share_id}", "", ""))
    return {
        "share_id": share_id,
        "host": host,
        "path": parts.path,
        "normalized_url": normalized,
        "api_url": API_URL_TEMPLATE.format(share_id=share_id),
    }


def detect_versions(doc: dict) -> dict:
    """Read and normalise the source version fields (never silently overwrite them)."""
    data = doc.get("data") or {}
    raw = {
        "dataVersion": data.get("dataVersion"),
        "data_version": doc.get("data_version"),
        "created_for_build": doc.get("created_for_build"),
    }

    def normalize(value):
        if value is None:
            return None
        text = str(value).strip()
        match = re.search(r"(\d+\.\d+(?:\.\d+)*)", text)
        return match.group(1) if match else None

    normalized = {key: normalize(value) for key, value in raw.items()}
    return {
        "raw": raw,
        "normalized": normalized,
        "protocol_version": PROTOCOL_VERSION,
    }


def classify_versions(versions: dict, target_game_version: str) -> dict:
    """Compare source version fields against the catalog target version."""
    normalized = versions.get("normalized") or {}
    raw = versions.get("raw") or {}
    current = normalized.get("dataVersion")
    doc_level = normalized.get("data_version")
    created = normalized.get("created_for_build")
    problems: list[str] = []
    warnings: list[str] = []

    if current is None:
        problems.append("data.dataVersion is missing or unparseable")
    elif target_game_version and current != target_game_version:
        problems.append(f"data.dataVersion {current!r} != catalog target {target_game_version!r}")
    if doc_level is None:
        problems.append("data_version is missing or unparseable")
    elif target_game_version and doc_level != target_game_version:
        problems.append(f"data_version {doc_level!r} != catalog target {target_game_version!r}")
    if created is None:
        warnings.append("created_for_build is missing or unparseable")
    elif target_game_version and created != target_game_version:
        warnings.append(
            f"created_for_build {created!r} != catalog target {target_game_version!r} "
            "(kept as historical creation version; not used to select the catalog)"
        )
    status = "matched" if not problems else "mismatched"
    return {
        "status": status,
        "target_game_version": target_game_version,
        "raw": raw,
        "normalized": normalized,
        "problems": problems,
        "warnings": warnings,
    }


def _read_response(response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise FetchError(
                f"response larger than {max_bytes} bytes",
                code="payload_too_large",
                fetch={"http_status": getattr(response, "status", None)},
            ) from None
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_build(
    share_id: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    api_url: str | None = None,
) -> dict:
    """Fetch ``/api/public/build_data/{shareId}`` and classify the result.

    Returns a fetch record (``mode='network'``) containing the raw bytes, sha256 and
    diagnostics.  Raises :class:`FetchError` with a stable ``code`` on failure.
    """
    url = api_url or API_URL_TEMPLATE.format(share_id=share_id)
    attempts: list[dict] = []
    last_error: dict = {}
    for attempt in range(retries + 1):
        started = _now()
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "en",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                content_type = response.headers.get("Content-Type", "")
                final_url = response.geturl()
                body = _read_response(response, max_bytes)
            final_host = (urlsplit(final_url).hostname or "").lower()
            if final_host not in SUPPORTED_HOSTS:
                raise FetchError(
                    f"unexpected redirect host {final_host!r} (only LE Tools hosts are followed)",
                    code="unexpected_redirect_host",
                    fetch={"http_status": status, "final_url": final_url},
                )
            attempts.append({"attempt": attempt + 1, "started_at": started, "http_status": status})
            fetched_at = _now()
            record = {
                "mode": "network",
                "api_url": url,
                "final_url": final_url,
                "http_status": status,
                "content_type": content_type,
                "fetched_at": fetched_at,
                "raw_sha256": sha256_bytes(body),
                "raw_bytes": body,
                "attempts": attempts,
                "retry_after": None,
            }
            _classify_payload(record)
            return record
        except urllib.error.HTTPError as exc:
            status = exc.code
            headers = {key.lower(): value for key, value in (exc.headers or {}).items()}
            retry_after = headers.get("retry-after")
            body = b""
            try:
                body = exc.read() or b""
            except Exception:  # pragma: no cover - defensive
                pass
            attempts.append(
                {"attempt": attempt + 1, "started_at": started, "http_status": status, "error": "http_error"}
            )
            code = ERROR_CODES.get(status, "http_5xx" if status >= 500 else f"http_{status}")
            last_error = {
                "code": code,
                "http_status": status,
                "api_url": url,
                "retry_after": retry_after,
                "body_prefix": body[:200].decode("utf-8", "replace"),
                "attempts": attempts,
                "fetched_at": _now(),
            }
            retryable = status == 429 or status >= 500
            if not retryable or attempt >= retries:
                raise FetchError(f"HTTP {status} from {url}", code=code, fetch=last_error) from None
            delay = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            if retry_after:
                try:
                    delay = min(MAX_RETRY_AFTER, float(retry_after))
                except ValueError:
                    delay = min(MAX_RETRY_AFTER, delay * 4)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "started_at": started,
                    "error": type(exc).__name__,
                    "detail": str(exc),
                }
            )
            last_error = {
                "code": "timeout" if isinstance(exc, TimeoutError) else "network_error",
                "api_url": url,
                "detail": str(exc),
                "attempts": attempts,
                "fetched_at": _now(),
            }
            if attempt >= retries:
                raise FetchError(
                    f"network failure fetching {url}: {exc}", code=last_error["code"], fetch=last_error
                ) from None
            time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
    raise FetchError(f"fetch failed for {url}", code="unknown", fetch=last_error)


def _classify_payload(record: dict) -> None:
    body: bytes = record["raw_bytes"]
    content_type = (record.get("content_type") or "").lower()
    text = None
    if b"<html" in body[:512].lower() or b"<!doctype html" in body[:512].lower():
        record["error"] = {"code": "html_response", "detail": "HTML document instead of JSON"}
        raise FetchError("endpoint returned HTML instead of JSON", code="html_response", fetch=record)
    try:
        text = body.decode("utf-8")
        document = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        record["error"] = {"code": "json_error", "detail": str(exc)}
        raise FetchError(f"response is not valid JSON: {exc}", code="json_error", fetch=record) from None
    if not isinstance(document, dict):
        record["error"] = {"code": "json_error", "detail": "top-level JSON value is not an object"}
        raise FetchError("top-level JSON value is not an object", code="json_error", fetch=record)
    if not isinstance(document.get("data"), dict):
        record["error"] = {"code": "missing_data", "detail": "JSON has no object 'data' field"}
        raise FetchError("build payload has no 'data' object", code="missing_data", fetch=record)
    if "application/json" not in content_type and content_type:
        record.setdefault("warnings", []).append(f"unexpected content type {content_type!r}")
    record["document"] = document


def load_archived_file(path: Path, *, source_label: str = "file") -> dict:
    """Load a previously archived raw build_data response (offline replay)."""
    path = Path(path)
    body = path.read_bytes()
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchError(f"archived file is not valid JSON: {exc}", code="json_error", fetch={"path": str(path)}) from None
    if not isinstance(document, dict) or not isinstance(document.get("data"), dict):
        raise FetchError("archived file has no object 'data' field", code="missing_data", fetch={"path": str(path)})
    record = {
        "mode": source_label,
        "file_path": str(path),
        "fetched_at": None,
        "raw_sha256": sha256_bytes(body),
        "raw_bytes": body,
        "document": document,
        "http_status": None,
        "api_url": None,
    }
    return record


def build_draft(fetch: dict, *, share_id: str | None = None, requested_url: str | None = None) -> dict:
    """Turn a fetch record into a protocol draft, preserving every source field."""
    document = fetch["document"]
    data = document.get("data")
    versions = detect_versions(document)
    top_level_keys = sorted(document.keys())
    data_keys = sorted(data.keys())
    known_data_keys = {
        "fromSaveFile",
        "gh",
        "newGrid",
        "bio",
        "equipment",
        "idols",
        "blessings",
        "weaverTreeContainers",
        "charTree",
        "skillTrees",
        "weaverTree",
        "hud",
        "weaponTypes",
        "dataVersion",
        "charTreeProgression",
        "skillTreesProgression",
        "weaverTreeProgression",
        "conditions",
        "customConditions",
        "activeBuffs",
        "buffSettings",
        "completedQuests",
    }
    known_top_level = {"data", "tags", "level", "class", "mastery", "created_for_build", "data_version", "guide"}
    return {
        "protocol_version": PROTOCOL_VERSION,
        "share_id": share_id,
        "requested_url": requested_url,
        "fetch": {key: value for key, value in fetch.items() if key not in ("raw_bytes", "document")},
        "document": document,
        "data": data,
        "versions": versions,
        "unexpected_top_level_fields": [key for key in top_level_keys if key not in known_top_level],
        "unexpected_data_fields": [key for key in data_keys if key not in known_data_keys],
    }


def archive_fetch(paths, share_id: str, fetch: dict) -> dict:
    """Persist raw payload + attempt metadata.  Idempotent by content hash."""
    raw_dir = paths.raw_dir(share_id)
    fetches_dir = paths.fetches_dir(share_id)
    raw_dir.mkdir(parents=True, exist_ok=True)
    fetches_dir.mkdir(parents=True, exist_ok=True)
    raw_sha = fetch["raw_sha256"]
    raw_path = raw_dir / f"{raw_sha}.json"
    if not raw_path.is_file():
        raw_path.write_bytes(fetch["raw_bytes"])
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    attempt_id = f"{stamp}-{raw_sha[:12]}"
    fetch_path = fetches_dir / f"{attempt_id}.json"
    metadata = {
        "attempt_id": attempt_id,
        "share_id": share_id,
        "raw_sha256": raw_sha,
        "raw_path": str(raw_path.relative_to(paths.root)),
        "mode": fetch.get("mode"),
        "api_url": fetch.get("api_url"),
        "final_url": fetch.get("final_url"),
        "http_status": fetch.get("http_status"),
        "content_type": fetch.get("content_type"),
        "fetched_at": fetch.get("fetched_at"),
        "attempts": fetch.get("attempts"),
        "file_path": fetch.get("file_path"),
        "archived_at": _now(),
    }
    write_json(fetch_path, metadata)
    return {"attempt_id": attempt_id, "raw_path": raw_path, "fetch_path": fetch_path, "raw_sha256": raw_sha}
