"""One-time source acquisition for the affix database.

This module is the *only* place in :mod:`affix_db` that talks to the network.  It
is deliberately not imported by ``build``/``query``/``validate``: those run fully
offline against the frozen snapshot produced here.

The tunklab site is a Next.js/Turbopack app.  The affix data is embedded in one
minified client chunk as a plain JS object literal; this module discovers the
current chunk names from the public affix page HTML, then downloads exactly the
chunks the page loads, plus a set of affix page HTML snapshots used for display
verification.  Downloaded JavaScript is treated as inert bytes and is never
executed.

Usage (from the repository root)::

    python -m affix_db fetch --out data/affix-db/v1/source-snapshot \
        --page-slugs fire_resistance added_health ...
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "https://lastepoch.tunklab.com"
USER_AGENT = "LastEpoch-BuildSimulator-affix-db/1.0 (static data extraction; no JS execution)"

_CHUNK_RE = re.compile(r"/_next/static/chunks/[A-Za-z0-9_./-]+\.js")
_ROUTE_CHUNKS_RE = re.compile(
    r'__turbopack_load_page_chunks__\("([^"]+)",\s*\[(.*?)\]\)', re.S
)
_QUOTED_RE = re.compile(r'"([^"]+)"')

#: Slugs used for the display-verification matrix (see reports/affix-db/).
DEFAULT_PAGE_SLUGS = (
    "fire_resistance",
    "added_health",
    "increased_armor",
    "added_melee_physical_damage",
    "void_penetration",
    "increased_movement_speed",
    "added_mana",
    "freeze_rate_multiplier_and_cold_resistance",
    "idol_fire_resistance",
    "idol_all_resistances_for_you_and_your_minions",
)


def normalize_chunk_path(path: str) -> str:
    """Turn a chunk reference from the page HTML into an absolute URL path.

    Server-rendered ``<script src>`` values are already ``/_next/static/...``;
    the Turbopack route list uses bare ``static/chunks/...``.
    """
    if path.startswith("/_next/"):
        return path
    if path.startswith("/"):
        return path
    if path.startswith("static/"):
        return "/_next/" + path
    return "/_next/static/chunks/" + path.rsplit("/", 1)[-1]


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    fetched_at: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def http_get(url: str, timeout: float = 60.0) -> FetchResult:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https base
        body = resp.read()
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return FetchResult(
            url=url,
            final_url=resp.geturl(),
            status=resp.status,
            headers=headers,
            body=body,
            fetched_at=_now(),
        )


def _write_bytes(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def _extract_chunk_urls(html: str) -> list[str]:
    seen: list[str] = []
    for match in _CHUNK_RE.finditer(html):
        url = match.group(0)
        if url not in seen:
            seen.append(url)
    return seen


def _route_chunks(text: str) -> list[str]:
    """Return chunk paths declared by ``__turbopack_load_page_chunks__``."""
    out: list[str] = []
    for match in _ROUTE_CHUNKS_RE.finditer(text):
        for item in _QUOTED_RE.findall(match.group(2)):
            if item not in out:
                out.append(item)
    return out


def fetch_snapshot(
    out_dir: Path | str,
    base_url: str = DEFAULT_BASE_URL,
    page_slugs: tuple[str, ...] = DEFAULT_PAGE_SLUGS,
    timeout: float = 60.0,
    progress=print,
) -> dict:
    """Download the affix page, its chunk graph and the verification pages.

    Returns the inventory dict that is also written to ``inventory.json``.
    """
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(
            f"refusing to overwrite non-empty snapshot directory: {out} "
            "(delete it or pick a new --out)"
        )
    out.mkdir(parents=True, exist_ok=True)
    chunks_dir = out / "chunks"
    pages_dir = out / "pages"

    inventory: dict = {
        "source": "tunklab",
        "base_url": base_url,
        "fetched_at": _now(),
        "user_agent": USER_AGENT,
        "entry_page": f"{base_url}/affix/{page_slugs[0]}",
        "discovered_chunks": [],
        "chunks": [],
        "pages": [],
        "errors": [],
    }

    # 1. Entry page HTML (SSR shell; contains the chunk script tags).
    entry = http_get(f"{base_url}/affix/{page_slugs[0]}", timeout=timeout)
    entry_text = entry.body.decode("utf-8", errors="replace")
    _write_bytes(pages_dir / f"{page_slugs[0]}.html", entry.body)
    inventory["pages"].append(
        {
            "role": "entry",
            "slug": page_slugs[0],
            "url": entry.url,
            "status": entry.status,
            "sha256": entry.sha256,
            "size": len(entry.body),
            "fetched_at": entry.fetched_at,
            "path": f"pages/{page_slugs[0]}.html",
        }
    )

    discovered = _extract_chunk_urls(entry_text)
    inventory["discovered_chunks"] = discovered
    progress(f"entry page disclosed {len(discovered)} chunks")

    # 2. Fetch every JS chunk the page references.  The route chunk for
    #    /affix/[slug] declares the complete page chunk list; fetch any chunk
    #    that only shows up there as well.
    fetched: dict[str, dict] = {}
    queue = list(discovered)
    while queue:
        rel = queue.pop(0)
        if rel in fetched:
            continue
        url = base_url + rel
        try:
            res = http_get(url, timeout=timeout)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            inventory["errors"].append({"url": url, "error": repr(exc)})
            progress(f"ERROR {url}: {exc}")
            continue
        name = Path(rel).name
        _write_bytes(chunks_dir / name, res.body)
        record = {
            "url": url,
            "file_name": f"chunks/{name}",
            "status": res.status,
            "sha256": res.sha256,
            "size": len(res.body),
            "content_type": res.headers.get("content-type"),
            "etag": res.headers.get("etag"),
            "last_modified": res.headers.get("last-modified"),
            "fetched_at": res.fetched_at,
        }
        fetched[rel] = record
        text = res.body.decode("utf-8", errors="replace")
        for extra in _route_chunks(text):
            norm = normalize_chunk_path(extra)
            if norm not in fetched and norm not in queue:
                queue.append(norm)
        progress(f"fetched {name} ({len(res.body)} bytes)")

    inventory["chunks"] = sorted(fetched.values(), key=lambda r: r["file_name"])

    # 3. Remaining verification pages (HTML).  Their chunk graph is the same;
    #    we still snapshot the server-rendered HTML for provenance.
    for slug in page_slugs:
        if slug == page_slugs[0]:
            continue
        url = f"{base_url}/affix/{slug}"
        try:
            res = http_get(url, timeout=timeout)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            inventory["errors"].append({"url": url, "error": repr(exc)})
            continue
        _write_bytes(pages_dir / f"{slug}.html", res.body)
        inventory["pages"].append(
            {
                "role": "affix-page",
                "slug": slug,
                "url": url,
                "status": res.status,
                "sha256": res.sha256,
                "size": len(res.body),
                "fetched_at": res.fetched_at,
                "path": f"pages/{slug}.html",
            }
        )
        progress(f"fetched page {slug} ({len(res.body)} bytes)")

    inventory["fetched_at"] = _now()
    inventory["summary"] = {
        "chunks": len(inventory["chunks"]),
        "pages": len(inventory["pages"]),
        "errors": len(inventory["errors"]),
        "total_chunk_bytes": sum(c["size"] for c in inventory["chunks"]),
    }
    _write_bytes(
        out / "inventory.json",
        json.dumps(inventory, indent=2, ensure_ascii=False, sort_keys=False).encode("utf-8"),
    )
    return inventory
