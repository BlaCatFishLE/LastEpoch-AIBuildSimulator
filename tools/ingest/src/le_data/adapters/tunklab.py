"""Tunklab source adapter.

Tunklab (https://lastepoch.tunklab.com) is a Next.js/Turbopack site.  Its
entity data is not served from a JSON API and there is no ``__NEXT_DATA__``
payload: every page ships client bundles in which data modules are embedded as
plain JavaScript literals, e.g.::

    e.s(["f",0,{version:"1.5.2",data:{...}}])          // exported module
    let c={"ac-1":[{maxPoints:8,mastery:0,nodeName:"..."}]}  // page-local data

Chunk filenames change on every deploy, so the adapter rediscovers them from
the page HTML referenced by the current snapshot:

  discover -> entry pages -> chunk URLs (plus chunk -> chunk lazy references)
  fetch    -> exact bytes under data/raw/tunklab/<snapshot>/responses
  parse    -> strict literal extraction, never executes JavaScript

parse() is content driven: data modules are recognised by the *shape* of their
payload, not by chunk filename, so a rename across deploys does not break
extraction.  Payloads that cannot be classified are reported as SourceIssues
instead of being dropped silently, and every recognised record keeps the full
original payload in ``raw``.

Contract reminders (phase-1 workguide sections 6 and 7):
  * parse(offline) only reads files fetch() already produced;
  * source records are source-native: no cross-source merge happens here;
  * ``source_scope`` carries the tree / class scope so ids that are only
    unique inside a scope stay addressable.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..fetch import FetchError
from ..jsdata import JsParseError, js_to_jsonable, parse_literal
from ..records import (
    PARSE_FAILED,
    PARSE_OK,
    PARSE_SKIPPED,
    PARSE_UNPARSED,
    SourceIssue,
    SourceRecord,
)
from .base import BaseAdapter

DEFAULT_BASE_URL = "https://lastepoch.tunklab.com"
DEFAULT_ENTRY_PAGES = (
    "/",
    "/skillsearch",
    "/buffs",
    "/ailments",
    "/class/mage",
    "/class/acolyte",
    "/class/primalist",
    "/class/sentinel",
    "/class/rogue",
    "/ability?a=Fireball",
    "/ability?a=FlameWard",
)

#: chunk reference forms seen on the site: absolute in <script src>, relative
#: inside the inline ``(globalThis["TURBOPACK"]...).push({"otherChunks":[...]})``
#: block and inside Turbopack lazy-import tables.
CHUNK_REF_RE = re.compile(
    r"(?:/_next/static/chunks/|static/chunks/)([A-Za-z0-9_.~\-]+\.js)"
)
#: version-wrapped data module, e.g. ``{version:"1.5.2",data:...}``
VERSION_MODULE_RE = re.compile(r'\{version:"')
VERSION_VALUE_RE = re.compile(r'\{version:"([^"]*)",data:')
#: ``NAME=<literal>`` sites; class passive trees are page-local assignments.
ASSIGN_RE = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)=([\[{])")
#: homepage version banner: ``Game Version: <span class="number">1.5.2</span>``
GAME_VERSION_RE = re.compile(r"Game Version:\s*<span[^>]*>([^<]+)</span>")
#: homepage entity counters: ``<li>Skills <span class="number float-right">136</span>``
SITE_COUNT_RE = re.compile(
    r"<li>([A-Za-z][A-Za-z0-9 /'()\-]{1,40}?)\s*"
    r'<span class="number float-right">([0-9][0-9,]*)</span>'
)

#: deterministic emission order, also used by the run summary
KIND_ORDER = [
    "class",
    "mastery",
    "ability",
    "ability_properties",
    "property",
    "property_text",
    "skill_tree",
    "skill_tree_node",
    "passive_tree",
    "passive_tree_node",
    "buff",
    "ailment",
    "affix",
    "unique_item",
    "item_set",
    "item_type",
    "monster",
    "quest",
    "cache",
    "monolith_mod",
    "loot_group",
    "dungeon",
    "timeline",
    "world_config",
    "monolith_config",
    "config_block",
    "module",
    "relation",
]

#: entity families a healthy snapshot must provide (used for completeness issues)
REQUIRED_KINDS = {
    "ability": "ability definitions",
    "skill_tree": "skill trees",
    "skill_tree_node": "skill tree nodes",
    "passive_tree": "class passive trees",
    "buff": "buff index (/buffs)",
    "ailment": "ailment index (/ailments)",
    "affix": "affixes",
    "unique_item": "unique items",
    "class": "classes",
    "mastery": "masteries",
    "property": "stat property dictionary",
}

#: payload keys that identify a collection/entity kind by shape
SHAPE_TESTS: list[tuple[str, frozenset[str]]] = [
    ("affix", frozenset({"affixName", "affixId", "tiers"})),
    ("item_set", frozenset({"setID", "setName", "mods"})),
    ("unique_item", frozenset({"uniqueId", "isSetItem", "legendaryType"})),
    ("ability_properties", frozenset({"abilityID", "properties"})),
    ("monolith_mod", frozenset({"modType", "slug", "title"})),
    ("property", frozenset({"property", "propertyName", "defaultAltText"})),
    ("property_text", frozenset({"propertyName", "altText"})),
    ("ability", frozenset({"id", "name", "abilityId", "tags", "charges"})),
    ("class", frozenset({"className", "classID", "masteries", "unlockableAbilities"})),
    ("ailment", frozenset({"name", "slug", "duration", "maxInstances", "positive"})),
    ("buff", frozenset({"displayName", "maxInstances", "positive", "noFixedDuration"})),
    ("dungeon", frozenset({"displayName", "dungeonID", "tiers"})),
    ("cache", frozenset({"displayName", "cacheType", "slug"})),
    ("quest", frozenset({"chapter", "displayName", "steps", "mainLineQuest"})),
    ("monster", frozenset({"id", "name", "health", "skills", "type"})),
    ("loot_group", frozenset({"id", "lootGroups"})),
    ("timeline", frozenset({"timelineID", "difficulties"})),
]

#: minimum homogeneous-record list length for an unclassified literal to be
#: worth an issue (small UI tables/lookup maps are not entity payloads)
UNCLASSIFIED_MIN_ITEMS = 20
#: unclassified payloads must share at least this many keys to count as
#: entity-like (UI icon descriptors share tag/attrs/children = 3)
UNCLASSIFIED_MIN_COMMON_KEYS = 4


@dataclass
class DataLiteral:
    """One parsed literal candidate inside a JavaScript chunk."""

    resource_name: str
    offset: int
    end: int
    wrapper: str  # "versioned" | "assignment"
    label: str  # module data path root ("data") or variable name
    data: Any
    version: str | None = None
    kind: str | None = None
    duplicate_index: int = 0


@dataclass
class Resource:
    resource_name: str
    url: str
    role: str  # "page" | "chunk"
    page: str = ""  # for role=page: entry path+query; for role=chunk: chunk path
    referenced_by: list[str] = field(default_factory=list)
    status: str = ""
    http_status: int = 0
    size: int = 0
    sha256: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_name": self.resource_name,
            "url": self.url,
            "role": self.role,
            "page": self.page,
            "referenced_by": self.referenced_by,
            "status": self.status,
            "http_status": self.http_status,
            "size": self.size,
            "sha256": self.sha256,
            "error": self.error,
        }


class TunklabAdapter(BaseAdapter):
    """discover / fetch / parse for lastepoch.tunklab.com."""

    name = "tunklab"

    # -- configuration -----------------------------------------------------
    @property
    def base_url(self) -> str:
        return str(self.config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")

    @property
    def claimed_version(self) -> str:
        return str(self.config.get("claimed_version") or "")

    @property
    def include_optional_domains(self) -> bool:
        return bool(self.config.get("include_optional_domains", True))

    @property
    def max_lazy_chunks(self) -> int:
        return int(self.config.get("max_lazy_chunks", 60))

    def _entry_pages(self) -> list[str]:
        raw = self.config.get("entry_pages") or list(DEFAULT_ENTRY_PAGES)
        pages: list[str] = []
        for item in raw:
            text = str(item)
            if text.startswith("http://") or text.startswith("https://"):
                pages.append(text)
            else:
                pages.append(self.base_url + (text if text.startswith("/") else "/" + text))
        return pages

    # -- path / naming helpers --------------------------------------------
    def snapshot_dir(self, snapshot_id: str) -> Path:
        """Snapshot directory under the adapter's raw root (never the global one)."""
        return self.raw_root / self.name / snapshot_id

    def inventory_path(self, snapshot_id: str) -> Path:
        return self.snapshot_dir(snapshot_id) / "inventory.json"

    def responses_dir(self, snapshot_id: str) -> Path:
        return self.snapshot_dir(snapshot_id) / "responses"

    @staticmethod
    def _sanitize(text: str) -> str:
        cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in text)
        return cleaned.strip("_") or "root"

    def page_resource_name(self, url: str) -> str:
        rest = url[len(self.base_url):] if url.startswith(self.base_url) else url
        path, _, query = rest.partition("?")
        if path in ("", "/"):
            name = "page_root"
        else:
            name = "page_" + self._sanitize(path.strip("/").replace("/", "_"))
        if query:
            name += "__q_" + self._sanitize(query)
        return name

    @staticmethod
    def chunk_resource_name(chunk_path: str) -> str:
        return "chunk_" + Path(chunk_path).name

    @staticmethod
    def chunk_url(base_url: str, chunk_path: str) -> str:
        if chunk_path.startswith("http://") or chunk_path.startswith("https://"):
            return chunk_path
        return base_url + "/_next/static/chunks/" + Path(chunk_path).name

    # -- source record helpers --------------------------------------------
    def _record(
        self,
        snapshot_id: str,
        *,
        resource_name: str,
        entity_kind: str,
        source_scope: str,
        source_id: str,
        locator: str,
        raw: Any,
        parse_status: str = PARSE_OK,
        notes: Iterable[str] = (),
    ) -> SourceRecord:
        return SourceRecord(
            source=self.name,
            source_snapshot_id=snapshot_id,
            resource_name=resource_name,
            entity_kind=entity_kind,
            source_scope=source_scope,
            source_id=source_id,
            locator=locator,
            raw=js_to_jsonable(raw),
            parse_status=parse_status,
            notes=list(notes),
        )

    def _issue(
        self,
        snapshot_id: str,
        *,
        issue_type: str,
        severity: str,
        message: str,
        resource_name: str = "",
        entity_kind: str = "",
        source_id: str = "",
        field_path: str = "",
        expected: str = "",
        actual: str = "",
        evidence: str = "",
    ) -> SourceIssue:
        return self.add_issue(
            issue_type=issue_type,
            severity=severity,
            source_snapshot_id=snapshot_id,
            resource_name=resource_name,
            entity_kind=entity_kind,
            source_id=source_id,
            field_path=field_path,
            message=message,
            expected=expected,
            actual=actual,
            evidence=evidence[:4000],
        )

    @staticmethod
    def _locator(resource_name: str, offset: int, path: str) -> str:
        return f"{resource_name}#offset={offset};path={path}"

    # -- literal extraction ------------------------------------------------
    def _iter_literals(self, resource_name: str, text: str) -> tuple[list[DataLiteral], list[dict]]:
        """Return (data literal candidates, parse errors) without executing JS.

        Two passes are required because the site embeds data both ways:

        1. version-wrapped modules ``{version:"...",data:...}`` (usually inside
           ``e.s([...])`` calls, so they are call arguments, not assignments);
        2. assigned literals ``NAME={...}`` / ``NAME=[...]`` (page-local data such
           as the class passive trees).

        Parsing failures are only reported for version-wrapped candidates: an
        assignment site may legally have a non-literal right-hand side.
        """
        found: list[DataLiteral] = []
        errors: list[dict] = []
        covered: list[tuple[int, int]] = []

        for match in VERSION_MODULE_RE.finditer(text):
            start = match.start()
            if any(lo <= start < hi for lo, hi in covered):
                continue
            try:
                value, end = parse_literal(text, start)
            except (JsParseError, ValueError) as exc:
                errors.append({"offset": start, "error": str(exc),
                               "context": text[start:start + 120]})
                covered.append((start, start + 1))
                continue
            covered.append((start, end))
            if not (isinstance(value, dict) and "version" in value and "data" in value):
                errors.append({"offset": start,
                               "error": "version-wrapped literal has no version/data keys",
                               "context": text[start:start + 120]})
                continue
            found.append(DataLiteral(
                resource_name=resource_name, offset=start, end=end,
                wrapper="versioned", label="data", data=value["data"],
                version=value.get("version"),
            ))

        for match in ASSIGN_RE.finditer(text):
            label, start = match.group(1), match.start(2)
            if any(lo <= start < hi for lo, hi in covered):
                continue
            try:
                value, end = parse_literal(text, start)
            except (JsParseError, ValueError):
                continue
            covered.append((start, end))
            found.append(DataLiteral(
                resource_name=resource_name, offset=start, end=end,
                wrapper="assignment", label=label, data=value,
            ))

        found.sort(key=lambda lit: lit.offset)
        return found, errors

    # -- payload classification -------------------------------------------
    def _classify(self, data: Any) -> str | None:
        if isinstance(data, dict):
            keys = set(data)
            if keys and all(isinstance(v, list) for v in data.values()):
                sample = next((v for v in data.values() if v), None)
                if sample and isinstance(sample[0], dict) and "nodeName" in sample[0]:
                    node = sample[0]
                    if "mastery" in node or "masteryReq" in node:
                        return "passive_tree"
                    return "skill_tree"
                return None
            if "equippableItems" in keys and "nonEquippableItems" in keys:
                return "item_type"
            if "scenes" in keys and "defaultSceneData" in keys:
                return "world_config"
            if "naturalSpawnRateModifier" in keys or "baseRunePrisonSpawnRate" in keys:
                return "monolith_config"
            if len(keys) >= 5 and all(isinstance(v, (str, int, float, bool)) for v in data.values()):
                return "config_block"
            return None
        if isinstance(data, list):
            first = next((item for item in data if isinstance(item, dict)), None)
            if first is None:
                return None
            keys = set(first)
            for kind, required in SHAPE_TESTS:
                if required <= keys:
                    return kind
            return None
        return None

    @staticmethod
    def _is_entity_like(data: Any) -> bool:
        """Heuristic for 'this unclassified literal may hold entity data'."""
        if isinstance(data, list):
            dicts = [item for item in data if isinstance(item, dict)]
            if len(data) < UNCLASSIFIED_MIN_ITEMS or len(dicts) < len(data) * 0.8:
                return False
            common = set(dicts[0])
            for item in dicts[:50]:
                common &= set(item)
            return len(common) >= UNCLASSIFIED_MIN_COMMON_KEYS
        if isinstance(data, dict):
            values = list(data.values())
            dicts = [v for v in values if isinstance(v, dict)]
            if len(dicts) >= UNCLASSIFIED_MIN_ITEMS:
                common = set(dicts[0])
                for item in dicts[:50]:
                    common &= set(item)
                return len(common) >= UNCLASSIFIED_MIN_COMMON_KEYS
        return False

    @staticmethod
    def _first_record(data: Any) -> Any:
        if isinstance(data, list):
            return js_to_jsonable(data[0]) if data else None
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, list) and value:
                    return js_to_jsonable(value[0])
                if isinstance(value, dict):
                    return js_to_jsonable(value)
        return None

    # =====================================================================
    # discover
    # =====================================================================
    def discover(self, snapshot_id: str, offline: bool = False) -> dict[str, Any]:
        fetcher = self.fetcher(snapshot_id, offline=offline)
        resources: dict[str, Resource] = {}
        page_chunks: dict[str, list[str]] = {}
        page_status: dict[str, str] = {}
        version_evidence: dict[str, Any] = {
            "claimed_version": self.claimed_version,
            "game_version_banners": [],
            "claimed_version_pages": {},
            "site_counts": {},
            "module_versions": {},
        }
        discovered_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        failures = 0
        for url in self._entry_pages():
            name = self.page_resource_name(url)
            try:
                result = fetcher.fetch(url, name)
            except FetchError as exc:  # defensive: Fetcher normally returns failed
                failures += 1
                self._issue(snapshot_id, issue_type="fetch_failed", severity="error",
                            message=f"entry page could not be fetched: {exc}",
                            resource_name=name, evidence=url)
                continue
            resource = Resource(
                resource_name=name, url=url, role="page",
                page=self._page_key(url), status=result.fetch_status,
                http_status=result.http_status, size=result.size,
                sha256=result.sha256, error=result.error,
            )
            resources[name] = resource
            page_status[url] = result.fetch_status
            if result.fetch_status == "failed":
                failures += 1
                self._issue(snapshot_id, issue_type="fetch_failed", severity="error",
                            message=f"entry page fetch failed: {result.error}",
                            resource_name=name, evidence=url)
                continue
            body = self._read_body(result.path)
            if body is None:
                self._issue(snapshot_id, issue_type="offline_cache_missing", severity="error",
                            message="entry page body is missing from the fetch cache",
                            resource_name=name, evidence=str(result.path))
                continue

            version_evidence["claimed_version_pages"][self._page_key(url)] = body.count(
                self.claimed_version) if self.claimed_version else 0
            for match in GAME_VERSION_RE.finditer(body):
                version_evidence["game_version_banners"].append({
                    "resource_name": name,
                    "page": self._page_key(url),
                    "text": match.group(1).strip(),
                })
            for match in SITE_COUNT_RE.finditer(body):
                label = " ".join(match.group(1).split())
                value = int(match.group(2).replace(",", ""))
                version_evidence["site_counts"][label] = value

            chunk_paths: list[str] = []
            for match in CHUNK_REF_RE.finditer(body):
                chunk_path = "/_next/static/chunks/" + match.group(1)
                if chunk_path not in chunk_paths:
                    chunk_paths.append(chunk_path)
            page_chunks[self._page_key(url)] = chunk_paths
            for chunk_path in chunk_paths:
                chunk_name = self.chunk_resource_name(chunk_path)
                existing = resources.get(chunk_name)
                if existing is None:
                    resources[chunk_name] = Resource(
                        resource_name=chunk_name,
                        url=self.chunk_url(self.base_url, chunk_path),
                        role="chunk", page=chunk_path,
                        referenced_by=[self._page_key(url)], status="not_fetched",
                    )
                elif self._page_key(url) not in existing.referenced_by:
                    existing.referenced_by.append(self._page_key(url))

        # follow chunk -> chunk lazy references using any already cached chunks
        self._scan_chunk_references(snapshot_id, resources, fetcher, version_evidence)

        if failures and len(page_status) == failures:
            self._issue(snapshot_id, issue_type="discover_failed", severity="error",
                        message="no entry page could be fetched; inventory is empty",
                        evidence=json.dumps(page_status, ensure_ascii=False))

        inventory = {
            "source": self.name,
            "source_snapshot_id": snapshot_id,
            "status": ("ok" if failures == 0 else
                       ("failed" if failures == len(page_status) else "partial")),
            "base_url": self.base_url,
            "claimed_version": self.claimed_version,
            "discovered_at": discovered_at,
            "offline": offline,
            "version_evidence": version_evidence,
            "page_chunks": page_chunks,
            "page_status": page_status,
            "resources": [item.to_dict() for item in resources.values()],
        }
        path = self.inventory_path(snapshot_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
        self.logger(
            f"[tunklab] discover: {len(page_chunks)} pages, "
            f"{sum(1 for r in resources.values() if r.role == 'chunk')} chunks"
        )
        return inventory

    def _page_key(self, url: str) -> str:
        """Path+query form used as the human-facing page key in reports."""
        rest = url[len(self.base_url):] if url.startswith(self.base_url) else url
        if not rest.startswith("/"):
            rest = "/" + rest
        return rest

    @staticmethod
    def _read_body(path: str | None) -> str | None:
        if not path:
            return None
        target = Path(path)
        if not target.exists():
            return None
        return target.read_text(encoding="utf-8", errors="replace")

    def _scan_chunk_references(
        self,
        snapshot_id: str,
        resources: dict[str, Resource],
        fetcher,
        version_evidence: dict[str, Any],
    ) -> None:
        """Add chunk resources referenced by already cached chunk bodies.

        Turbopack lazy imports (``e.v(t=>Promise.all(["static/chunks/x.js"]...))``)
        are only visible inside another chunk, so they cannot be found from the
        HTML alone.  This pass only looks at cached bodies (never downloads), so
        it is safe for offline discover.
        """
        scanned: set[str] = set()
        added = 0
        while True:
            pending = [
                item for item in resources.values()
                if item.role == "chunk" and item.resource_name not in scanned
            ]
            if not pending:
                break
            for resource in pending:
                scanned.add(resource.resource_name)
                cached = fetcher.cached_result(resource.resource_name)
                if cached is None:
                    continue
                body = self._read_body(cached.path)
                if body is None:
                    continue
                for match in CHUNK_REF_RE.finditer(body):
                    chunk_path = "/_next/static/chunks/" + match.group(1)
                    chunk_name = self.chunk_resource_name(chunk_path)
                    if chunk_name in resources:
                        continue
                    resources[chunk_name] = Resource(
                        resource_name=chunk_name,
                        url=self.chunk_url(self.base_url, chunk_path),
                        role="chunk", page=chunk_path,
                        referenced_by=[f"chunk:{resource.resource_name}"],
                        status="not_fetched",
                    )
                    added += 1
                    if added > self.max_lazy_chunks:
                        self._issue(
                            snapshot_id, issue_type="chunk_limit_reached", severity="warning",
                            message="stopped following chunk->chunk references at the configured limit",
                            actual=str(self.max_lazy_chunks),
                        )
                        return

    # =====================================================================
    # fetch
    # =====================================================================
    def fetch(
        self,
        snapshot_id: str,
        offline: bool = False,
        inventory: dict | None = None,
    ) -> list[dict]:
        if inventory is None:
            inventory = self._load_inventory(snapshot_id)
        if inventory is None:
            inventory = self.discover(snapshot_id, offline=offline)
        fetcher = self.fetcher(snapshot_id, offline=offline)

        resources = [dict(item) for item in inventory.get("resources", [])]
        by_name: dict[str, dict] = {item["resource_name"]: item for item in resources}
        order = [item["resource_name"] for item in resources]
        results: list[dict] = []
        seen_lazy: set[str] = set()
        index = 0
        while index < len(order):
            name = order[index]
            index += 1
            item = by_name[name]
            if item["role"] == "page":
                # pages were already fetched by discover; reuse their cache
                cached = fetcher.cached_result(name)
                if cached is not None:
                    item.update(status="cache_hit", http_status=cached.http_status,
                                size=cached.size, sha256=cached.sha256)
                    results.append(cached.to_dict())
                    continue
            try:
                result = fetcher.fetch(item["url"], name)
            except FetchError as exc:
                item.update(status="failed", error=str(exc))
                results.append({"url": item["url"], "resource_name": name,
                                "fetch_status": "failed", "error": str(exc)})
                self._issue(snapshot_id, issue_type="fetch_failed", severity="error",
                            message=f"resource could not be fetched: {exc}",
                            resource_name=name, evidence=item["url"])
                continue
            item.update(status=result.fetch_status, http_status=result.http_status,
                        size=result.size, sha256=result.sha256, error=result.error,
                        path=result.path)
            results.append(result.to_dict())
            if result.fetch_status == "failed":
                self._issue(snapshot_id, issue_type="fetch_failed", severity="error",
                            message=f"resource fetch failed: {result.error}",
                            resource_name=name, evidence=item["url"])
                continue
            if item["role"] != "chunk" or name in seen_lazy:
                continue
            seen_lazy.add(name)
            body = self._read_body(result.path)
            if body is None:
                continue
            # follow lazy chunk references discovered inside freshly fetched chunks
            for match in CHUNK_REF_RE.finditer(body):
                chunk_path = "/_next/static/chunks/" + match.group(1)
                chunk_name = self.chunk_resource_name(chunk_path)
                if chunk_name in by_name:
                    continue
                if len(order) - len([r for r in resources if r["role"] == "page"]) > \
                        self.max_lazy_chunks:
                    break
                new_item = Resource(
                    resource_name=chunk_name,
                    url=self.chunk_url(self.base_url, chunk_path),
                    role="chunk", page=chunk_path,
                    referenced_by=[f"chunk:{name}"], status="not_fetched",
                ).to_dict()
                by_name[chunk_name] = new_item
                resources.append(new_item)
                order.append(chunk_name)

        inventory["resources"] = resources
        inventory["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        inventory["fetch_summary"] = _status_counts(results)
        path = self.inventory_path(snapshot_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
        self.logger(
            f"[tunklab] fetch: {len(results)} resources "
            f"({inventory['fetch_summary']})"
        )
        return results

    def _load_inventory(self, snapshot_id: str) -> dict | None:
        path = self.inventory_path(snapshot_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    # =====================================================================
    # parse
    # =====================================================================
    def parse(self, snapshot_id: str) -> Iterator[SourceRecord]:
        self.issues = []
        self.chunk_entity_map: dict[str, list[str]] = {}
        self.module_inventory: list[dict] = []
        self.relation_counts: dict[str, int] = {}
        self.relation_resolved_counts: dict[str, int] = {}
        self._collected: dict[str, list[DataLiteral]] = {}
        self.parse_stats: dict[str, Any] = {"chunks_scanned": 0, "pages_scanned": 0,
                                            "literals": 0, "classify_failed": 0}

        inventory = self._load_inventory(snapshot_id) or {}
        role_by_name = {item["resource_name"]: item.get("role", "")
                        for item in inventory.get("resources", [])}
        page_chunks = inventory.get("page_chunks", {})
        version_evidence = inventory.get("version_evidence", {})

        # ---- pass 1: read local responses and classify data modules ------
        collected: dict[str, list[DataLiteral]] = {}
        parse_failures = 0
        for resource_name in sorted(self._response_names(snapshot_id)):
            body = self._read_body(str(self.responses_dir(snapshot_id) / f"{resource_name}.body"))
            if body is None:
                self._issue(snapshot_id, issue_type="offline_cache_missing", severity="error",
                            message="response body missing for a resource listed in the snapshot",
                            resource_name=resource_name)
                parse_failures += 1
                continue
            role = role_by_name.get(resource_name) or self._sniff_role(body)
            if role == "page":
                self.parse_stats["pages_scanned"] += 1
                continue
            self.parse_stats["chunks_scanned"] += 1
            literals, errors = self._iter_literals(resource_name, body)
            self.parse_stats["literals"] += len(literals)
            for error in errors:
                parse_failures += 1
                self._issue(
                    snapshot_id, issue_type="parse_failed", severity="error",
                    message=f"cannot parse version-wrapped data module: {error['error']}",
                    resource_name=resource_name, field_path=f"offset={error['offset']}",
                    evidence=error.get("context", ""),
                )
            chunk_kinds: list[str] = []
            for literal in literals:
                kind = self._classify(literal.data)
                if kind == "config_block" and literal.wrapper != "versioned":
                    # page-local scalar dictionaries (UI theme/colour maps) are not
                    # game configuration blocks; only exported modules qualify
                    kind = None
                literal.kind = kind
                self.module_inventory.append({
                    "resource_name": resource_name,
                    "offset": literal.offset,
                    "wrapper": literal.wrapper,
                    "label": literal.label,
                    "version": literal.version,
                    "kind": kind,
                    "container": type(literal.data).__name__,
                    "count": len(literal.data) if hasattr(literal.data, "__len__") else 0,
                })
                if kind is None:
                    self.parse_stats["classify_failed"] += 1
                    if literal.wrapper == "versioned":
                        self._issue(
                            snapshot_id, issue_type="unparsed_module", severity="warning",
                            message="version-wrapped data module could not be classified; "
                                    "raw payload preserved in the chunk",
                            resource_name=resource_name,
                            field_path=f"offset={literal.offset}",
                            actual=f"version={literal.version} "
                                   f"container={type(literal.data).__name__} "
                                   f"first_keys={list(self._first_item(literal.data) or [])[:20]}",
                            evidence=json.dumps(
                                {"version": literal.version,
                                 "container": type(literal.data).__name__,
                                 "count": len(literal.data)
                                 if hasattr(literal.data, "__len__") else None,
                                 "first": self._first_record(literal.data)},
                                ensure_ascii=False)[:3500],
                        )
                        collected.setdefault("module", []).append(literal)
                    elif self._is_entity_like(literal.data):
                        self._issue(
                            snapshot_id, issue_type="unparsed_literal", severity="warning",
                            message="unclassified JavaScript literal with entity-like shape; "
                                    "not modelled by this adapter yet",
                            resource_name=resource_name,
                            field_path=f"offset={literal.offset};label={literal.label}",
                            actual=f"container={type(literal.data).__name__} "
                                   f"count={len(literal.data)} "
                                   f"first_keys={list(self._first_item(literal.data) or [])[:20]}",
                            evidence=json.dumps(
                                {"label": literal.label,
                                 "count": len(literal.data),
                                 "first": self._first_record(literal.data)},
                                ensure_ascii=False)[:3500],
                        )
                    continue
                if not self.include_optional_domains and kind in OPTIONAL_KINDS:
                    continue
                collected.setdefault(kind, []).append(literal)
                chunk_kinds.append(kind)
            if chunk_kinds:
                self.chunk_entity_map[resource_name] = sorted(set(chunk_kinds))

        # ---- pass 2: emit entity records (deterministic order) -----------
        emitters = {
            "class": self._emit_classes,
            "ability": self._emit_abilities,
            "ability_properties": self._emit_ability_properties,
            "property": self._emit_properties,
            "property_text": self._emit_property_texts,
            "skill_tree": self._emit_skill_trees,
            "passive_tree": self._emit_passive_trees,
            "buff": self._emit_buffs,
            "ailment": self._emit_ailments,
            "affix": self._emit_affixes,
            "unique_item": self._emit_unique_items,
            "item_set": self._emit_item_sets,
            "item_type": self._emit_item_types,
            "monster": self._emit_monsters,
            "quest": self._emit_quests,
            "cache": self._emit_caches,
            "monolith_mod": self._emit_monolith_mods,
            "loot_group": self._emit_loot_groups,
            "dungeon": self._emit_dungeons,
            "timeline": self._emit_timelines,
            "world_config": self._emit_config_blocks,
            "monolith_config": self._emit_config_blocks,
            "config_block": self._emit_config_blocks,
            "module": self._emit_modules,
        }
        seen_ids: dict[tuple[str, str, str], int] = {}
        records: list[SourceRecord] = []
        for kind in KIND_ORDER:
            if kind == "relation" or kind not in collected:
                continue
            emitter = emitters[kind]
            records.extend(emitter(snapshot_id, collected[kind], seen_ids))

        for record in records:
            yield record

        # ---- pass 3: relationships confirmed by the source data ----------
        relation_records = self._emit_relations(snapshot_id, collected)
        for record in relation_records:
            yield record

        # ---- pass 4: completeness / cross checks -------------------------
        self._collected = collected
        emitted_counts: dict[str, int] = {}
        for record in records + relation_records:
            emitted_counts[record.entity_kind] = emitted_counts.get(record.entity_kind, 0) + 1
        self._emit_completeness_issues(snapshot_id, emitted_counts, version_evidence)
        ui_chunks = sorted({
            self.chunk_resource_name(chunk_path)
            for chunks in page_chunks.values() for chunk_path in chunks
            if self.chunk_resource_name(chunk_path) not in self.chunk_entity_map
        })
        if ui_chunks:
            self._issue(
                snapshot_id, issue_type="no_data_module", severity="info",
                message=f"{len(ui_chunks)} referenced chunks carry no recognised data "
                        f"module (client UI chunks); they are retained in the snapshot "
                        f"and are not parsed into source records",
                actual=", ".join(ui_chunks)[:2000],
                evidence="page->chunk mapping is in tunklab_summary.json "
                         "parse.chunk_entity_map",
            )
        self.parse_stats["records"] = len(records) + len(relation_records)
        self.parse_stats["parse_failures"] = parse_failures

    def _response_names(self, snapshot_id: str) -> list[str]:
        directory = self.responses_dir(snapshot_id)
        if not directory.exists():
            return []
        names = []
        for meta in directory.glob("*.meta.json"):
            names.append(meta.name[: -len(".meta.json")])
        return names

    @staticmethod
    def _sniff_role(body: str) -> str:
        head = body.lstrip()[:200].lower()
        if head.startswith("<!doctype html") or head.startswith("<html"):
            return "page"
        return "chunk"

    @staticmethod
    def _first_item(data: Any) -> dict | None:
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    return item
        elif isinstance(data, dict):
            for value in data.values():
                if isinstance(value, dict):
                    return value
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    return value[0]
        return None

    # -- id dedupe helper -------------------------------------------------
    @staticmethod
    def _unique_id(
        kind: str,
        scope: str,
        source_id: str,
        seen: dict[tuple[str, str, str], int],
    ) -> tuple[str, bool]:
        key = (kind, scope, source_id)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 1:
            return source_id, False
        return f"{source_id}#dup{seen[key]}", True

    # =====================================================================
    # entity emitters
    # =====================================================================
    def _emit_classes(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict):
                    continue
                class_name = str(item.get("className") or f"class-{index}")
                source_id, duplicate = self._unique_id("class", "", class_name, seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id", severity="warning",
                                message="duplicate className in class module",
                                resource_name=literal.resource_name, entity_kind="class",
                                source_id=class_name,
                                field_path=f"data.{index}.className")
                notes = [
                    "class record keeps the full payload: base stats, default/known/"
                    "unlockable abilities, masteries and starting items",
                ]
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="class", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{index}"),
                    raw=item,
                    notes=notes + (["optional domain: C"] if False else []),
                ))
                # masteries are separate scoped entities
                for m_index, mastery in enumerate(item.get("masteries") or []):
                    if not isinstance(mastery, dict):
                        continue
                    mastery_name = str(mastery.get("name") or f"mastery-{m_index}")
                    scoped_id, m_dup = self._unique_id("mastery", class_name,
                                                       mastery_name, seen)
                    if m_dup:
                        self._issue(snapshot_id, issue_type="duplicate_source_id",
                                    severity="warning",
                                    message="duplicate mastery name inside a class",
                                    resource_name=literal.resource_name,
                                    entity_kind="mastery", source_id=mastery_name,
                                    field_path=f"data.{index}.masteries.{m_index}.name")
                    records.append(self._record(
                        snapshot_id, resource_name=literal.resource_name,
                        entity_kind="mastery", source_scope=class_name,
                        source_id=scoped_id,
                        locator=self._locator(literal.resource_name, literal.offset,
                                              f"data.{index}.masteries.{m_index}"),
                        raw=mastery,
                        notes=[f"mastery index {m_index} of class {class_name} "
                               f"(index 0 is the base class row)"],
                    ))
        return records

    def _emit_abilities(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict) or "id" not in item:
                    continue
                raw_id = str(item.get("id"))
                source_id, duplicate = self._unique_id("ability", "", raw_id, seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning",
                                message="duplicate ability id in the ability module; "
                                        "the second occurrence got a #dup suffix so both "
                                        "records survive",
                                resource_name=literal.resource_name,
                                entity_kind="ability", source_id=raw_id,
                                field_path=f"data.{index}.id")
                notes = []
                tree = item.get("playerAbilityID")
                if isinstance(tree, str) and tree and tree != raw_id:
                    notes.append(f"playerAbilityID={tree} (skill tree scope id)")
                if item.get("abilityId"):
                    notes.append(f"game abilityId={item['abilityId']}")
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="ability", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{index}"),
                    raw=item, notes=notes,
                ))
        return records

    def _emit_ability_properties(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict) or "abilityID" not in item:
                    continue
                source_id, duplicate = self._unique_id(
                    "ability_properties", "", str(item["abilityID"]), seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning",
                                message="duplicate abilityID grouping in the "
                                        "per-ability property module",
                                resource_name=literal.resource_name,
                                entity_kind="ability_properties",
                                source_id=str(item["abilityID"]),
                                field_path=f"data.{index}.abilityID")
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="ability_properties", source_scope="",
                    source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{index}"),
                    raw=item,
                    notes=["property descriptions keyed by game abilityId "
                           "(join with ability.abilityId)"],
                ))
        return records

    def _emit_properties(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict) or "property" not in item:
                    continue
                source_id, duplicate = self._unique_id(
                    "property", "", str(item["property"]), seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning",
                                message="duplicate stat property id",
                                resource_name=literal.resource_name,
                                entity_kind="property",
                                source_id=str(item["property"]),
                                field_path=f"data.{index}.property")
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="property", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{index}"),
                    raw=item,
                    notes=["stat property dictionary: propertyName + defaultAltText + "
                           "altTextOverrides; referenced by nodes, affixes and items"],
                ))
        return records

    def _emit_property_texts(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict):
                    continue
                name = str(item.get("propertyName") or "")
                source_id, duplicate = self._unique_id("property_text", "", f"idx-{index}", seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning",
                                message="duplicate positional id in property text module",
                                resource_name=literal.resource_name,
                                entity_kind="property_text", source_id=f"idx-{index}",
                                field_path=f"data.{index}")
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="property_text", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{index}"),
                    raw=item,
                    notes=["source module has no id field; positional id assigned",
                           f"propertyName={name}"],
                ))
        return records

    def _emit_skill_trees(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for tree_id, nodes in literal.data.items():
                nodes = nodes if isinstance(nodes, list) else []
                source_id, duplicate = self._unique_id("skill_tree", "", str(tree_id), seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning",
                                message="duplicate skill tree id across chunks",
                                resource_name=literal.resource_name,
                                entity_kind="skill_tree", source_id=str(tree_id))
                root = nodes[0] if nodes and isinstance(nodes[0], dict) else None
                root_name = str(root.get("nodeName")) if root else ""
                notes = [
                    "tree id is the source skill-tree id (ability.playerAbilityID refers to it)",
                    f"root node name (display name): {root_name or '<none>'}",
                    "node layout/position is not provided by this source module (absent)",
                ]
                if not nodes:
                    notes.append("source data contains this tree id with an empty node list")
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="skill_tree", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{tree_id}"),
                    raw={"treeId": tree_id, "nodeCount": len(nodes),
                         "root": root, "nodes": nodes},
                    notes=notes,
                ))
                for node_index, node in enumerate(nodes):
                    if not isinstance(node, dict):
                        continue
                    node_id = str(node.get("id", f"idx{node_index}"))
                    node_source_id, node_duplicate = self._unique_id(
                        "skill_tree_node", str(tree_id), node_id, seen)
                    if node_duplicate:
                        self._issue(snapshot_id, issue_type="duplicate_source_id",
                                    severity="warning",
                                    message="duplicate node id inside a skill tree",
                                    resource_name=literal.resource_name,
                                    entity_kind="skill_tree_node",
                                    source_id=node_id, field_path=f"tree {tree_id}[{node_index}]")
                    records.append(self._record(
                        snapshot_id, resource_name=literal.resource_name,
                        entity_kind="skill_tree_node", source_scope=str(tree_id),
                        source_id=node_source_id,
                        locator=self._locator(literal.resource_name, literal.offset,
                                              f"data.{tree_id}.{node_index}"),
                        raw=node,
                    ))
        return records

    def _emit_passive_trees(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for tree_id, nodes in literal.data.items():
                nodes = nodes if isinstance(nodes, list) else []
                source_id, duplicate = self._unique_id("passive_tree", "", str(tree_id), seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning",
                                message="duplicate passive tree id across chunks",
                                resource_name=literal.resource_name,
                                entity_kind="passive_tree", source_id=str(tree_id))
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="passive_tree", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f'"{tree_id}"'),
                    raw={"treeId": tree_id, "nodeCount": len(nodes), "nodes": nodes},
                    notes=["class passive tree (keyed by class treeID, e.g. mg-1); "
                           "nodes carry mastery/masteryReq thresholds",
                           "node layout/position is not provided by this literal (absent)"],
                ))
                for node_index, node in enumerate(nodes):
                    if not isinstance(node, dict):
                        continue
                    node_id = str(node.get("id", f"idx{node_index}"))
                    node_source_id, node_duplicate = self._unique_id(
                        "passive_tree_node", str(tree_id), node_id, seen)
                    if node_duplicate:
                        self._issue(snapshot_id, issue_type="duplicate_source_id",
                                    severity="warning",
                                    message="duplicate node id inside a passive tree",
                                    resource_name=literal.resource_name,
                                    entity_kind="passive_tree_node",
                                    source_id=node_id, field_path=f"tree {tree_id}[{node_index}]")
                    records.append(self._record(
                        snapshot_id, resource_name=literal.resource_name,
                        entity_kind="passive_tree_node", source_scope=str(tree_id),
                        source_id=node_source_id,
                        locator=self._locator(literal.resource_name, literal.offset,
                                              f'"{tree_id}".{node_index}'),
                        raw=node,
                    ))
        return records

    def _emit_buffs(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict):
                    continue
                source_id, duplicate = self._unique_id("buff", "", str(item.get("id")), seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning", message="duplicate buff id",
                                resource_name=literal.resource_name, entity_kind="buff",
                                source_id=str(item.get("id")))
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="buff", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{index}"),
                    raw=item,
                    notes=["dataset: /buffs page (Non-Ailment Buffs table); "
                           "duration/stack rules are only known when the source gives them"],
                ))
        return records

    def _emit_ailments(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict):
                    continue
                source_id, duplicate = self._unique_id("ailment", "", str(item.get("id")), seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning", message="duplicate status id",
                                resource_name=literal.resource_name,
                                entity_kind="ailment", source_id=str(item.get("id")))
                notes = ["dataset: /ailments page (status-effect table: "
                         "DoT/Spell/Base Damage/Duration/Max Stacks/Damage Type)"]
                if item.get("positive"):
                    notes.append("positive=true: the /ailments page lists positive statuses too")
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="ailment", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{index}"),
                    raw=item, notes=notes,
                ))
        return records

    def _emit_affixes(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict):
                    continue
                source_id, duplicate = self._unique_id(
                    "affix", "", str(item.get("affixId")), seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning", message="duplicate affixId",
                                resource_name=literal.resource_name,
                                entity_kind="affix", source_id=str(item.get("affixId")))
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="affix", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{index}"),
                    raw=item,
                    notes=["affix definition with tier roll ranges (tiers[].minRoll/maxRoll)"],
                ))
        return records

    def _emit_unique_items(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict):
                    continue
                raw_id = str(item.get("uniqueId"))
                source_id, duplicate = self._unique_id("unique_item", "", raw_id, seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning", message="duplicate uniqueId",
                                resource_name=literal.resource_name,
                                entity_kind="unique_item", source_id=raw_id)
                notes = ["isSetItem=true entries are set pieces (set bonuses live in "
                         "the item_set module)"] if item.get("isSetItem") else []
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="unique_item", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{index}"),
                    raw=item, notes=notes,
                ))
        return records

    def _emit_item_sets(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict):
                    continue
                source_id, duplicate = self._unique_id(
                    "item_set", "", str(item.get("setID")), seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning", message="duplicate setID",
                                resource_name=literal.resource_name,
                                entity_kind="item_set", source_id=str(item.get("setID")))
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="item_set", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{index}"),
                    raw=item,
                    notes=["set bonus definition (mods[].setRequirement = pieces needed)"],
                ))
        return records

    def _emit_item_types(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            data = literal.data
            for group in ("equippableItems", "nonEquippableItems"):
                for index, item in enumerate(data.get(group) or []):
                    if not isinstance(item, dict):
                        continue
                    source_id, duplicate = self._unique_id(
                        "item_type", group, str(item.get("baseTypeID")), seen)
                    if duplicate:
                        self._issue(snapshot_id, issue_type="duplicate_source_id",
                                    severity="warning",
                                    message=f"duplicate baseTypeID in {group}",
                                    resource_name=literal.resource_name,
                                    entity_kind="item_type",
                                    source_id=str(item.get("baseTypeID")))
                    records.append(self._record(
                        snapshot_id, resource_name=literal.resource_name,
                        entity_kind="item_type", source_scope=group, source_id=source_id,
                        locator=self._locator(literal.resource_name, literal.offset,
                                              f"data.{group}.{index}"),
                        raw=item,
                        notes=["base item type; subItems[] are the concrete base items "
                               "(implicits included in this payload)"],
                    ))
        return records

    def _emit_monsters(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict):
                    continue
                source_id, duplicate = self._unique_id(
                    "monster", "", str(item.get("id")), seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning", message="duplicate monster id",
                                resource_name=literal.resource_name,
                                entity_kind="monster", source_id=str(item.get("id")))
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="monster", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{index}"),
                    raw=item,
                    notes=["optional domain: C (enemy/minion actor definition)",
                           "used to resolve ability.summon.actor references"],
                ))
        return records

    def _emit_quests(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict):
                    continue
                source_id, duplicate = self._unique_id(
                    "quest", "", str(item.get("id")), seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning", message="duplicate quest id",
                                resource_name=literal.resource_name,
                                entity_kind="quest", source_id=str(item.get("id")))
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="quest", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{index}"),
                    raw=item, notes=["optional domain: C"],
                ))
        return records

    def _emit_caches(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict):
                    continue
                source_id, duplicate = self._unique_id(
                    "cache", "", str(item.get("slug")), seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning", message="duplicate cache slug",
                                resource_name=literal.resource_name,
                                entity_kind="cache", source_id=str(item.get("slug")))
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="cache", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{index}"),
                    raw=item, notes=["optional domain: C (monolith cache definition)"],
                ))
        return records

    def _emit_monolith_mods(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict):
                    continue
                source_id, duplicate = self._unique_id(
                    "monolith_mod", "", str(item.get("slug")), seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning", message="duplicate monolith mod slug",
                                resource_name=literal.resource_name,
                                entity_kind="monolith_mod", source_id=str(item.get("slug")))
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="monolith_mod", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{index}"),
                    raw=item, notes=["optional domain: C (monolith echo modifier)"],
                ))
        return records

    def _emit_loot_groups(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict):
                    continue
                source_id, duplicate = self._unique_id(
                    "loot_group", "", str(item.get("id")), seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning", message="duplicate loot group id",
                                resource_name=literal.resource_name,
                                entity_kind="loot_group", source_id=str(item.get("id")))
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="loot_group", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{index}"),
                    raw=item, notes=["optional domain: C (boss loot table)"],
                ))
        return records

    def _emit_dungeons(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict):
                    continue
                source_id, duplicate = self._unique_id(
                    "dungeon", "", str(item.get("dungeonID")), seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning", message="duplicate dungeonID",
                                resource_name=literal.resource_name,
                                entity_kind="dungeon", source_id=str(item.get("dungeonID")))
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="dungeon", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{index}"),
                    raw=item, notes=["optional domain: C (dungeon definition)"],
                ))
        return records

    def _emit_timelines(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict):
                    continue
                source_id, duplicate = self._unique_id(
                    "timeline", "", str(item.get("timelineID")), seen)
                if duplicate:
                    self._issue(snapshot_id, issue_type="duplicate_source_id",
                                severity="warning", message="duplicate timelineID",
                                resource_name=literal.resource_name,
                                entity_kind="timeline", source_id=str(item.get("timelineID")))
                records.append(self._record(
                    snapshot_id, resource_name=literal.resource_name,
                    entity_kind="timeline", source_scope="", source_id=source_id,
                    locator=self._locator(literal.resource_name, literal.offset,
                                          f"data.{index}"),
                    raw=item, notes=["optional domain: C (monolith timeline definition)"],
                ))
        return records

    def _emit_config_blocks(self, snapshot_id, literals, seen):
        records = []
        for literal in literals:
            data = literal.data
            signature = ",".join(list(data)[:6]) if isinstance(data, dict) else ""
            kind = literal.kind or "config_block"
            source_id, duplicate = self._unique_id(
                kind, "",
                f"{Path(literal.resource_name).stem}@{literal.offset}", seen)
            records.append(self._record(
                snapshot_id, resource_name=literal.resource_name,
                entity_kind=kind, source_scope="", source_id=source_id,
                locator=self._locator(literal.resource_name, literal.offset, literal.label),
                raw=data,
                notes=["optional domain: C (singleton game configuration block)",
                       f"top-level keys: {signature}"],
            ))
        return records

    def _emit_modules(self, snapshot_id, literals, seen):
        """Preserve unclassified version-wrapped modules as unparsed records."""
        records = []
        for literal in literals:
            data = literal.data
            source_id = f"{Path(literal.resource_name).stem}@{literal.offset}"
            records.append(self._record(
                snapshot_id, resource_name=literal.resource_name,
                entity_kind="module", source_scope="", source_id=source_id,
                locator=self._locator(literal.resource_name, literal.offset, literal.label),
                raw={
                    "version": literal.version,
                    "container": type(data).__name__,
                    "count": len(data) if hasattr(data, "__len__") else None,
                    "first_keys": list(self._first_item(data) or [])[:30],
                    "first_items": ([js_to_jsonable(item) for item in data[:3]]
                                    if isinstance(data, list) else None),
                },
                parse_status=PARSE_UNPARSED,
                notes=["version-wrapped payload not modelled by this adapter; "
                       "full bytes remain in the raw response"],
            ))
        return records

    # =====================================================================
    # relationships
    # =====================================================================
    def _emit_relations(
        self,
        snapshot_id: str,
        collected: dict[str, list[DataLiteral]],
    ) -> list[SourceRecord]:
        """Emit relationship candidates confirmed by the source payloads."""
        records: list[SourceRecord] = []
        seen: set[tuple[str, str, str]] = set()
        counts: dict[str, int] = {}
        resolved_counts: dict[str, int] = {}
        unresolved: dict[str, int] = {}

        classes: list[tuple[DataLiteral, dict]] = []
        abilities: list[tuple[DataLiteral, dict]] = []
        skill_trees: dict[str, DataLiteral] = {}
        passive_trees: dict[str, DataLiteral] = {}
        skill_nodes: dict[str, dict[str, DataLiteral]] = {}
        passive_nodes: dict[str, dict[str, DataLiteral]] = {}
        ailments: list[tuple[DataLiteral, dict]] = []
        properties: set[str] = set()
        monsters: set[str] = set()
        item_types: list[tuple[DataLiteral, dict, str]] = []

        for literal in collected.get("class", []):
            for item in literal.data:
                if isinstance(item, dict):
                    classes.append((literal, item))
        for literal in collected.get("ability", []):
            for item in literal.data:
                if isinstance(item, dict) and "id" in item:
                    abilities.append((literal, item))
        for literal in collected.get("skill_tree", []):
            for tree_id, nodes in literal.data.items():
                skill_trees[str(tree_id)] = literal
                skill_nodes[str(tree_id)] = {
                    str(node.get("id")): literal
                    for node in (nodes or []) if isinstance(node, dict) and "id" in node
                }
        for literal in collected.get("passive_tree", []):
            for tree_id, nodes in literal.data.items():
                passive_trees[str(tree_id)] = literal
                passive_nodes[str(tree_id)] = {
                    str(node.get("id")): literal
                    for node in (nodes or []) if isinstance(node, dict) and "id" in node
                }
        for literal in collected.get("ailment", []):
            for item in literal.data:
                if isinstance(item, dict):
                    ailments.append((literal, item))
        ailment_ids = {str(item.get("id")) for _, item in ailments}
        for literal in collected.get("property", []):
            for item in literal.data:
                if isinstance(item, dict):
                    properties.add(str(item.get("property")))
        for literal in collected.get("monster", []):
            for item in literal.data:
                if isinstance(item, dict):
                    monsters.add(str(item.get("id")))
        for literal in collected.get("item_type", []):
            for group in ("equippableItems", "nonEquippableItems"):
                for item in literal.data.get(group) or []:
                    if isinstance(item, dict):
                        item_types.append((literal, item, group))
        ability_ids = {str(item.get("id")) for _, item in abilities}
        ability_by_id = {str(item.get("id")): item for _, item in abilities}

        def add(
            relationship: str,
            from_kind: str,
            from_scope: str,
            from_id: str,
            to_kind: str | None,
            to_scope: str,
            to_id: str | None,
            literal: DataLiteral,
            path: str,
            *,
            value: Any = None,
            method: str = "direct",
            extra: dict | None = None,
        ) -> None:
            key = (from_scope, from_id, relationship + "|" + str(to_id))
            if key in seen:
                return
            seen.add(key)
            resolved = to_kind is not None and to_id is not None
            counts[relationship] = counts.get(relationship, 0) + 1
            if resolved:
                resolved_counts[relationship] = resolved_counts.get(relationship, 0) + 1
            else:
                unresolved[relationship] = unresolved.get(relationship, 0) + 1
            raw: dict[str, Any] = {
                "relationship": relationship,
                "from": {"kind": from_kind, "scope": from_scope, "id": from_id},
                "to": ({"kind": to_kind, "scope": to_scope, "id": to_id}
                       if resolved else
                       {"kind": to_kind or "unknown", "scope": to_scope,
                        "unresolved_reference": to_id if to_id is not None else None}),
                "resolution_status": "resolved" if resolved else "unresolved",
                "method": method,
                "source_field": path,
            }
            if value is not None:
                raw["value"] = js_to_jsonable(value)
            if extra:
                raw.update(js_to_jsonable(extra))
            records.append(self._record(
                snapshot_id, resource_name=literal.resource_name,
                entity_kind="relation", source_scope=from_scope,
                source_id=f"{from_id}->{relationship}->{to_id}",
                locator=self._locator(literal.resource_name, literal.offset, path),
                raw=raw,
            ))

        # class / mastery -> ability ownership ---------------------------
        for literal, item in classes:
            class_name = str(item.get("className") or "")
            for field_name in ("defaultAbilities", "knownAbilities"):
                for ability in item.get(field_name) or []:
                    add("belongs_to_class", "ability", "", str(ability),
                        "class", "", class_name, literal, f"{field_name}",
                        value={"slot": field_name})
            for entry in item.get("unlockableAbilities") or []:
                if isinstance(entry, dict) and entry.get("ability"):
                    add("belongs_to_class", "ability", "", str(entry["ability"]),
                        "class", "", class_name, literal, "unlockableAbilities",
                        value={"slot": "unlockable", "level": entry.get("level")})
            for m_index, mastery in enumerate(item.get("masteries") or []):
                if not isinstance(mastery, dict):
                    continue
                mastery_name = str(mastery.get("name") or "")
                for entry in mastery.get("abilities") or []:
                    if isinstance(entry, dict) and entry.get("ability"):
                        add("belongs_to_mastery", "ability", "", str(entry["ability"]),
                            "mastery", class_name, mastery_name, literal,
                            f"masteries.{m_index}.abilities",
                            value={"level": entry.get("level")})
                mastery_ability = mastery.get("masteryAbility")
                if isinstance(mastery_ability, str) and mastery_ability:
                    add("grants_ability", "mastery", class_name, mastery_name,
                        "ability" if mastery_ability in ability_ids else None, "",
                        mastery_ability if mastery_ability in ability_ids else mastery_ability,
                        literal, f"masteries.{m_index}.masteryAbility",
                        extra={"note": "masteryAbility resolved against the ability module"
                               if mastery_ability in ability_ids else
                               "masteryAbility not present as an ability id in this snapshot"})
            tree_id = item.get("treeID")
            if isinstance(tree_id, str) and tree_id:
                resolved = tree_id in passive_trees
                add("has_tree", "class", "", class_name,
                    "passive_tree" if resolved else None, "",
                    tree_id, literal, "treeID",
                    extra={"note": "class passive tree resolved"
                           if resolved else
                           "treeID did not match a passive tree payload in this snapshot"})

        # ability -> skill tree -----------------------------------------
        for literal, item in abilities:
            ability_id = str(item.get("id"))
            tree_id = item.get("playerAbilityID")
            if isinstance(tree_id, str) and tree_id:
                resolved = tree_id in skill_trees
                add("has_tree", "ability", "", ability_id,
                    "skill_tree" if resolved else None, "",
                    tree_id, literal, "playerAbilityID",
                    extra={"note": "ability owns this skill tree (source field "
                           "playerAbilityID)" if resolved else
                           "playerAbilityID did not match a skill tree payload"})
            # trigger references
            for path, ref in self._ability_trigger_refs(item):
                resolved = ref in ability_ids
                add("triggers_skill", "ability", "", ability_id,
                    "ability" if resolved else None, "", ref, literal, path,
                    extra={"note": "" if resolved else
                           "referenced ability id not present in this snapshot"})
            for path, ref in self._ability_summon_refs(item):
                resolved = ref in monsters
                add("summons_minion", "ability", "", ability_id,
                    "monster" if resolved else None, "", ref, literal, path,
                    extra={"note": "summon.actor resolved against monster module"
                           if resolved else
                           "summon.actor is not a monster id in this snapshot"})
            for path, ref, chance in self._ability_ailment_refs(item):
                resolved = ref in ailment_ids
                add("applies_ailment", "ability", "", ability_id,
                    "ailment" if resolved else None, "", ref, literal, path,
                    value={"chance": chance},
                    extra={"note": "" if resolved else
                           "ailment numeric id not present in the /ailments dataset"})

        # skill / passive tree node prerequisites + node -> ability -------
        for tree_id, literal in skill_trees.items():
            nodes = literal.data.get(tree_id) or []
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                node_id = str(node.get("id"))
                for req_index, requirement in enumerate(node.get("requirements") or []):
                    if not isinstance(requirement, dict):
                        continue
                    target = requirement.get("nodeID")
                    if target is None:
                        continue
                    resolved = str(target) in skill_nodes.get(tree_id, {})
                    add("requires_node", "skill_tree_node", tree_id, node_id,
                        "skill_tree_node" if resolved else None, tree_id,
                        target, literal,
                        f"data.{tree_id}.<node {node_id}>.requirements.{req_index}",
                        value={"points": requirement.get("requirement")},
                        extra={"note": "" if resolved else
                               "prerequisite node id absent from this tree payload"})
                self._node_ability_relation(
                    add, literal, tree_id, node, "skill_tree_node", ability_ids)
        for tree_id, literal in passive_trees.items():
            nodes = literal.data.get(tree_id) or []
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                node_id = str(node.get("id"))
                for req_index, requirement in enumerate(node.get("requirements") or []):
                    if not isinstance(requirement, dict):
                        continue
                    target = requirement.get("nodeID")
                    if target is None:
                        continue
                    resolved = str(target) in passive_nodes.get(tree_id, {})
                    add("requires_node", "passive_tree_node", tree_id, node_id,
                        "passive_tree_node" if resolved else None, tree_id,
                        target, literal,
                        f'"{tree_id}".<node {node_id}>.requirements.{req_index}',
                        value={"points": requirement.get("requirement")},
                        extra={"note": "" if resolved else
                               "prerequisite node id absent from this tree payload"})
                self._node_ability_relation(
                    add, literal, tree_id, node, "passive_tree_node", ability_ids)

        # status effect -> ability procs --------------------------------
        for literal, item in ailments:
            status_id = str(item.get("id"))
            for field_name in ("abilityToProcOnExpiration",
                               "abilityToProcWhenStacksReached"):
                value = item.get(field_name)
                if not isinstance(value, str) or not value:
                    continue
                resolved = value in ability_ids
                add("procs_ability", "ailment", "", status_id,
                    "ability" if resolved else None, "", value, literal, field_name,
                    extra={"note": "" if resolved else
                           "referenced ability id not present in this snapshot"})

        # A/B stat references -------------------------------------------
        for literal in collected.get("affix", []):
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict):
                    continue
                prop = item.get("statProperty")
                if prop is None:
                    continue
                resolved = str(prop) in properties
                add("modifies_stat", "affix", "", str(item.get("affixId")),
                    "property" if resolved else None, "", prop, literal,
                    f"data.{index}.statProperty",
                    extra={"note": "" if resolved else
                           "stat property id absent from the property dictionary"})
        for literal in collected.get("unique_item", []):
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict):
                    continue
                for mod_index, mod in enumerate(item.get("mods") or []):
                    if not isinstance(mod, dict) or mod.get("property") is None:
                        continue
                    prop = mod.get("property")
                    resolved = str(prop) in properties
                    add("modifies_stat", "unique_item", "", str(item.get("uniqueId")),
                        "property" if resolved else None, "", prop, literal,
                        f"data.{index}.mods.{mod_index}",
                        value={"value": mod.get("value"), "maxValue": mod.get("maxValue"),
                               "tags": mod.get("tags"), "specialTag": mod.get("specialTag"),
                               "type": mod.get("type")},
                        extra={"note": "" if resolved else
                               "stat property absent: only an implicit name is known"})
        for literal in collected.get("item_set", []):
            for index, item in enumerate(literal.data):
                if not isinstance(item, dict):
                    continue
                for mod_index, mod in enumerate(item.get("mods") or []):
                    if not isinstance(mod, dict) or mod.get("property") is None:
                        continue
                    prop = mod.get("property")
                    resolved = str(prop) in properties
                    add("modifies_stat", "item_set", "", str(item.get("setID")),
                        "property" if resolved else None, "", prop, literal,
                        f"data.{index}.mods.{mod_index}",
                        value={"value": mod.get("value"),
                               "setRequirement": mod.get("setRequirement"),
                               "tags": mod.get("tags"), "specialTag": mod.get("specialTag")},
                        extra={"note": "" if resolved else
                               "stat property absent from the property dictionary"})
        for literal, item, group in item_types:
            for sub_index, sub in enumerate(item.get("subItems") or []):
                if not isinstance(sub, dict):
                    continue
                for imp_index, implicit in enumerate(sub.get("implicits") or []):
                    if not isinstance(implicit, dict) or implicit.get("property") is None:
                        continue
                    prop = implicit.get("property")
                    resolved = str(prop) in properties
                    add("modifies_stat", "item_type", group,
                        str(item.get("baseTypeID")),
                        "property" if resolved else None, "", prop, literal,
                        f"data.{group}.<{item.get('baseTypeName')}>.subItems.{sub_index}"
                        f".implicits.{imp_index}",
                        value={"implicitValue": implicit.get("implicitValue"),
                               "implicitMaxValue": implicit.get("implicitMaxValue"),
                               "subTypeID": sub.get("subTypeID")},
                        extra={"note": "" if resolved else
                               "implicit property id absent from the dictionary"})
        for literal, item in ailments:
            for stat_index, stat in enumerate(item.get("buffs") or []):
                if not isinstance(stat, dict) or stat.get("property") is None:
                    continue
                prop = stat.get("property")
                resolved = str(prop) in properties
                add("modifies_stat", "ailment", "", str(item.get("id")),
                    "property" if resolved else None, "", prop, literal,
                    f"<status {item.get('name')}>.buffs.{stat_index}",
                    value={"addedValue": stat.get("addedValue"),
                           "increasedValue": stat.get("increasedValue"),
                           "moreValues": stat.get("moreValues"),
                           "tags": stat.get("tags")},
                    extra={"note": "inline status stats; this source gives no separate "
                                   "buff entity for them"})

        # unresolved reference summary (single roll-up instead of per-record noise)
        for relationship, missing in sorted(unresolved.items()):
            total = counts.get(relationship, 0)
            self._issue(
                snapshot_id, issue_type="unresolved_reference", severity="warning",
                message=f"{relationship}: {total - missing} of {total} source references "
                        f"resolved against this snapshot; {missing} stay unresolved and are "
                        f"kept as relation records with resolution_status=unresolved",
                entity_kind="relation",
                expected=f"resolved={total - missing}",
                actual=f"unresolved={missing}",
            )
        self.relation_counts = dict(counts)
        self.relation_resolved_counts = dict(resolved_counts)
        return records

    def _node_ability_relation(self, add, literal, tree_id, node, node_kind, ability_ids):
        target = node.get("ability")
        if not isinstance(target, str) or not target:
            return
        resolved = target in ability_ids
        add("modifies_skill", node_kind, tree_id, str(node.get("id")),
            "ability" if resolved else None, "", target, literal,
            f"<node {node.get('id')}>.ability",
            extra={"note": "node explicitly names the ability it modifies"
                   if resolved else
                   "node.ability does not match an ability id in this snapshot"})

    @staticmethod
    def _ability_trigger_refs(item: dict) -> Iterator[tuple[str, str]]:
        for field_name in ("comboAbilities", "nextAbilities"):
            for ref in item.get(field_name) or []:
                if isinstance(ref, str) and ref:
                    yield field_name, ref
        for list_field in ("onHit", "randomOnHit", "delayedCast"):
            entries = item.get(list_field)
            if isinstance(entries, list):
                for index, entry in enumerate(entries):
                    if isinstance(entry, dict) and isinstance(entry.get("ability"), str):
                        yield f"{list_field}.{index}.ability", entry["ability"]
        for list_field in ("delayedRandomCast",):
            for index, entry in enumerate(item.get(list_field) or []):
                if not isinstance(entry, dict):
                    continue
                for sub_index, sub in enumerate(entry.get("randomAbilities") or []):
                    if isinstance(sub, dict) and isinstance(sub.get("ability"), str):
                        yield (f"{list_field}.{index}.randomAbilities.{sub_index}.ability",
                               sub["ability"])
        for index, entry in enumerate(item.get("delayedSequenceCast") or []):
            if not isinstance(entry, dict):
                continue
            for sub_index, ref in enumerate(entry.get("abilities") or []):
                if isinstance(ref, str):
                    yield f"delayedSequenceCast.{index}.abilities.{sub_index}", ref
        for field_name in ("randomCast", "mineTrigger"):
            entry = item.get(field_name)
            if isinstance(entry, dict) and isinstance(entry.get("ability"), str):
                yield f"{field_name}.ability", entry["ability"]

    @staticmethod
    def _ability_summon_refs(item: dict) -> Iterator[tuple[str, str]]:
        summon = item.get("summon")
        if isinstance(summon, dict) and isinstance(summon.get("actor"), str):
            yield "summon.actor", summon["actor"]
        elif isinstance(summon, list):
            for index, entry in enumerate(summon):
                if isinstance(entry, dict) and isinstance(entry.get("actor"), str):
                    yield f"summon.{index}.actor", entry["actor"]

    @staticmethod
    def _ability_ailment_refs(item: dict) -> Iterator[tuple[str, str, Any]]:
        blocks = []
        if isinstance(item.get("ailmentOnHit"), dict):
            blocks.append(("ailmentOnHit", item["ailmentOnHit"]))
        for field_name in ("ailmentInRadius", "repeatAilment", "onHitAilment"):
            value = item.get(field_name)
            if isinstance(value, list):
                for index, entry in enumerate(value):
                    if isinstance(entry, dict):
                        blocks.append((f"{field_name}.{index}", entry))
        for path, block in blocks:
            for index, entry in enumerate(block.get("ailments") or []):
                if not isinstance(entry, dict):
                    continue
                ref = entry.get("ailment")
                if ref is None:
                    continue
                yield f"{path}.ailments.{index}.ailment", str(ref), entry.get("chance")

    # =====================================================================
    # quality checks
    # =====================================================================
    def _emit_completeness_issues(
        self,
        snapshot_id: str,
        emitted_counts: dict[str, int],
        version_evidence: dict[str, Any],
    ) -> None:
        for kind, description in REQUIRED_KINDS.items():
            if emitted_counts.get(kind, 0) > 0:
                continue
            severity = "error" if kind in {"ability", "skill_tree", "skill_tree_node"} else "warning"
            self._issue(
                snapshot_id, issue_type="missing_entity_family", severity=severity,
                message=f"no payload for required entity family {kind!r} ({description}) "
                        f"was found in the fetched chunks",
                entity_kind=kind,
                expected=description,
                actual="0 payload modules",
            )
        # claimed site counts vs parsed payload sizes (only informational)
        claimed = version_evidence.get("site_counts") or {}
        collected = self._collected
        base_items = sum(
            len(item.get("subItems") or [])
            for literal in collected.get("item_type", [])
            for group in ("equippableItems", "nonEquippableItems")
            for item in literal.data.get(group) or []
            if isinstance(item, dict)
        )
        non_empty_trees = sum(
            1 for literal in collected.get("skill_tree", [])
            for nodes in literal.data.values() if nodes
        )
        checks = {
            "Skills": ("skill_tree", non_empty_trees),
            "Skill Nodes": ("skill_tree_node", emitted_counts.get("skill_tree_node", 0)),
            "Affixes": ("affix", emitted_counts.get("affix", 0)),
            "Unique Items": ("unique_item", emitted_counts.get("unique_item", 0)),
            "Base Items": ("item_type", base_items),
        }
        for label, (kind, parsed) in checks.items():
            if label not in claimed:
                continue
            if parsed == claimed[label]:
                continue
            self._issue(
                snapshot_id, issue_type="count_mismatch", severity="info",
                message=f"homepage counter {label!r} does not equal the payload count; "
                        f"the counters may use a different definition (documented in "
                        f"the author-local discovery notes (archived))",
                entity_kind=kind,
                expected=str(claimed[label]),
                actual=str(parsed),
                evidence=json.dumps({"homepage_counters": claimed}, ensure_ascii=False),
            )
        self.parse_stats["claimed_counts"] = claimed
        self.parse_stats["module_versions"] = _module_version_counts(self._collected)


#: entity kinds outside the A/B data-domain priorities
OPTIONAL_KINDS = {
    "monster", "quest", "cache", "monolith_mod", "loot_group", "dungeon",
    "timeline", "world_config", "monolith_config", "config_block", "module",
}


def _status_counts(rows: Iterable[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("fetch_status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _module_version_counts(collected: dict[str, list[DataLiteral]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for literals in collected.values():
        for literal in literals:
            key = literal.version or "(no version wrapper)"
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


#: order of entity families used in the exported chunk map / summary
__all__ = ["TunklabAdapter"]
