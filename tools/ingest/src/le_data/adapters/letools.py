"""Last Epoch Tools (lastepochtools.com) source adapter.

The site ships its static data as plain JavaScript *literal assignments* under
``/data/versionNNN/<role>/js/<hash>.js`` (role in db, planner, skills, endgame,
factions).  Both the version directory and the script filenames change on every
deploy, so :meth:`LeToolsAdapter.discover` always re-reads the configured entry
pages and re-derives them.  Nothing is executed: every payload goes through
``le_data.jsdata`` (strict literal subset parser, see there for what is
supported).

Record model (phase-1 workguide 6.3): one :class:`SourceRecord` per source
entity, ``raw`` keeps the complete source object, dimensions that belong to an
outer object live in ``source_scope`` (tree id, base type id, class id, ...),
``source_id`` is the entity's own id inside that scope.  Overlapping windows
(e.g. a tree object that contains its nodes) are stored both at tree level and
per node so that downstream normalizers can choose granularity without having
to re-parse the bundle.

No file is written here except ``<raw>/letools/<snapshot>/inventory.json``
(discovery output) and the responses fetched by :mod:`le_data.fetch`.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..fetch import FetchError, Fetcher
from ..jsdata import (
    UnsupportedJsConstruct,
    find_assignment_sites,
    js_to_jsonable,
    parse_literal,
)
from ..records import (
    PARSE_FAILED,
    PARSE_OK,
    PARSE_SKIPPED,
    PARSE_UNPARSED,
    SourceIssue,
    SourceRecord,
)
from .base import BaseAdapter

# -- discovery regexes ------------------------------------------------------
# Observed forms (2026-11-01):
#   window['dataVersion']='version152';
#   <script type=text/javascript src=/data/version152/db/js/<32 hex>.js></script>
_DATA_VERSION_RE = re.compile(
    r"""window\s*\[\s*['"]dataVersion['"]\s*\]\s*=\s*['"](?P<version>[^'"]+)['"]"""
)
# Some pages may use window.dataVersion = 'version152' instead of bracket form.
_DATA_VERSION_DOT_RE = re.compile(
    r"""window\.dataVersion\s*=\s*['"](?P<version>[^'"]+)['"]"""
)
_SCRIPT_SRC_RE = re.compile(r"""<script[^>]*\ssrc\s*=\s*["'](?P<src>[^"']+)["']""", re.I)
_DATA_JS_RE = re.compile(
    r"""/data/(?P<data_version>version\w+)/(?P<role>[A-Za-z0-9_]+)/js/(?P<stem>[A-Za-z0-9_.-]+)\.js"""
)
# gameVersion lives in the db bundle as window.gameVersion='Version 1.5.2'
_GAME_VERSION_RE = re.compile(r"""window(?:\.gameVersion|\[['"]gameVersion['"]\])\s*=\s*['"](?P<value>[^'"]+)['"]""")
_WINDOW_ASSIGN_RE = re.compile(r"""window\.(?P<name>[A-Za-z_$][\w$]*)\s*=""")

# Bundle parsing order.  Different entry pages reference different bundle
# variants of the same data (the site duplicates LEAbilities/LESkillTrees/
# LECharTrees across the planner and skills pages).  Processing db -> skills ->
# planner means the role-specific bundle wins as the canonical copy and the
# other copies are recorded as ``duplicate_window`` evidence instead of being
# parsed twice (see _parse_bundle).
_ROLE_ORDER = ("db", "skills", "planner", "endgame", "factions", "checklist", "translations")

# Default translation endpoints.  The game-data translations live under the
# same version directory as the JS bundles (version directory is rediscovered
# on every run); the site-UI translations live under the fixed static_data
# prefix.  A run may override these through config:
#   "translation_resources": [{"resource_name": "...", "path_template": "...",
#                              "namespace": "full", "language": "en"}, ...]
# where {data_version} / {base_url} are substituted.
_DEFAULT_TRANSLATION_RESOURCES = (
    {
        "resource_name": "translations_game_full_en",
        "namespace": "full",
        "language": "en",
        "path_template": "/data/{data_version}/i18n/full/en.json",
        "requires_data_version": True,
        "derivation": "dataVersion directory + /i18n/full/<lang>.json",
    },
    {
        "resource_name": "translations_site_en",
        "namespace": "site",
        "language": "en",
        "path_template": "/static_data/i18n/en.json",
        "requires_data_version": False,
        "derivation": "fixed static_data i18n path (versioned cache marker langSiteCacheMarker)",
    },
)

# Translation keys are looked up with these key-field suffixes when checking
# coverage (report only; raw payloads keep every field untouched).
_TRANSLATION_KEY_FIELD_SUFFIX = "Key"

_PAGE_SIGNAL_RES = {
    "langSiteCacheMarker": re.compile(r"""window\[['"]langSiteCacheMarker['"]\]\s*=\s*['"](?P<value>[^'"]*)['"]"""),
    "langGameCacheMarker": re.compile(r"""window\[['"]langGameCacheMarker['"]\]\s*=\s*['"](?P<value>[^'"]*)['"]"""),
    "siteLanguage": re.compile(r"""window\[['"]siteLanguage['"]\]\s*=\s*['"](?P<value>[^'"]*)['"]"""),
}

# Windows we know how to turn into records; anything else becomes an
# ``unknown_window`` record plus an issue (never silently dropped).
_KNOWN_WINDOWS = {
    # db
    "itemDB", "gameVersion", "AT", "SP", "AilmentID", "LocalSP", "diffs",
    # planner
    "coreDB", "LEAbilities", "classInfoForSources",
    "itemSkillSources", "itemSkillLevelSources", "itemAilmentSources",
    "classSkillSources", "masterySkillSources", "passiveSkillSources",
    "treeSkillSources", "procSkillSources", "entityProcSkillSources",
    "transformSkillSources", "masteryBonusSkillSources",
    "ailmentSkillSources", "passiveAilmentSources", "treeAilmentSources",
    "skillAilmentSources", "invocationAilmentSources",
    "masteryBonusAilmentSources", "ailmentAilmentSources",
    # planner page-only windows (buff / bonus tables)
    "UIBuffs", "UIBuffIds", "BuffId", "BuffPropId", "CustomConditionType",
    "skillBonuses", "partialSkillBonuses", "passiveBonuses", "LEHUD",
    # skills
    "LESkillTrees", "LESkillTreesUI", "LECharTrees", "LECharTreesUI",
    "LEWeaverTree", "LEWeaverTreeUI", "LETooltipIcons",
    "skillTreeDiffs", "passiveTreeDiffs", "abilityDiffs", "ailmentDiffs",
    # endgame
    "monsterMods", "monolithRewards", "timelineData", "arenaData",
    "dungeonData", "vaultsOfUncertainFate", "dropBossLoot", "wovenEchoList",
    # factions
    "factionList", "prophecyData", "merchantsGuildBagData",
    # checklist (quest data, loaded by the planner page)
    "checklist",
}

_PLANNER_SOURCE_MAPS = (
    "itemSkillSources",
    "itemSkillLevelSources",
    "itemAilmentSources",
    "classSkillSources",
    "masterySkillSources",
    "passiveSkillSources",
    "treeSkillSources",
    "procSkillSources",
    "entityProcSkillSources",
    "transformSkillSources",
    "masteryBonusSkillSources",
    "ailmentSkillSources",
    "passiveAilmentSources",
    "treeAilmentSources",
    "skillAilmentSources",
    "invocationAilmentSources",
    "masteryBonusAilmentSources",
    "ailmentAilmentSources",
)

_ITEMDB_SOURCE_MAPS = (
    "relIncLevelAbilities",
    "relModAbilities",
    "relModAilments",
    "triggeredAbilities",
    "triggeredAilments",
)

_HISTORY_WINDOWS = ("skillTreeDiffs", "passiveTreeDiffs", "abilityDiffs", "ailmentDiffs")
_ENDGAME_WINDOWS = (
    "monsterMods", "monolithRewards", "timelineData", "arenaData",
    "dungeonData", "vaultsOfUncertainFate", "dropBossLoot", "wovenEchoList",
)

# itemDB top-level keys that are decomposed into entity records; everything
# else is preserved as an ``item_config`` record (complete value).
_ITEMDB_DECOMPOSED = {"itemList", "affixList", "uniqueList", "setBonusList"}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")
    return cleaned or "x"


def _short_version(data_version: str | None) -> str | None:
    """"version152" -> "1.5.2" (LE Tools encodes x.y.z as three digits)."""
    if not data_version:
        return None
    digits = re.sub(r"\D", "", data_version)
    if len(digits) == 3:
        return ".".join(digits)
    return None


class LeToolsAdapter(BaseAdapter):
    """discover/fetch/parse for Last Epoch Tools static JS data bundles."""

    name = "letools"

    def __init__(self, config: dict[str, Any], raw_root: Path, logger=None) -> None:
        super().__init__(config, raw_root, logger)
        self._snapshot_id: str = ""
        self._inventory: dict[str, Any] | None = None
        self._window_first: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _value_digest(value: Any) -> str:
        """Content hash of a parsed window value (used for duplicate detection)."""
        canonical = json.dumps(js_to_jsonable(value), sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # =================================================================
    # discover
    # =================================================================
    def discover(self, snapshot_id: str, offline: bool = False) -> dict[str, Any]:
        """Read the entry pages and derive the current data version + bundles."""
        fetcher = self.fetcher(snapshot_id, offline=offline)
        entry_pages: list[str] = list(self.config.get("entry_pages") or [])
        configured_claim = self.config.get("claimed_version")

        resources: dict[str, dict[str, Any]] = {}
        page_results: list[dict[str, Any]] = []
        version_evidence: list[dict[str, Any]] = []
        source_urls: list[str] = []
        page_signals: dict[str, dict[str, Any]] = {}
        data_version: str | None = None
        entry_url: str | None = None
        any_page_ok = False

        for url in entry_pages:
            resource_name = self._page_resource_name(url)
            try:
                result = fetcher.fetch(url, resource_name)
            except FetchError as exc:
                page_results.append({
                    "url": url, "resource_name": resource_name,
                    "fetch_status": "failed", "error": str(exc),
                })
                self.add_issue(
                    issue_type="entry_page_fetch_failed",
                    severity="error",
                    source_snapshot_id=snapshot_id,
                    resource_name=resource_name,
                    message=f"entry page could not be fetched: {exc}",
                    evidence=url,
                )
                continue

            row: dict[str, Any] = {
                "url": url,
                "resource_name": resource_name,
                "fetch_status": result.fetch_status,
                "http_status": result.http_status,
                "sha256": result.sha256,
                "size": result.size,
            }
            page_results.append(row)
            if result.fetch_status == "failed" or not result.path:
                row["error"] = result.error or "no body"
                self.add_issue(
                    issue_type="entry_page_fetch_failed",
                    severity="error",
                    source_snapshot_id=snapshot_id,
                    resource_name=resource_name,
                    message=f"entry page fetch failed: {result.error}",
                    evidence=url,
                )
                continue

            any_page_ok = True
            source_urls.append(url)
            if entry_url is None:
                entry_url = url
            text = Path(result.path).read_text(encoding="utf-8", errors="replace")

            version_match = _DATA_VERSION_RE.search(text) or _DATA_VERSION_DOT_RE.search(text)
            row["data_version"] = None
            if version_match:
                found = version_match.group("version")
                row["data_version"] = found
                if data_version is None:
                    data_version = found
                    version_evidence.append({
                        "kind": "js_global_variable",
                        "name": "dataVersion",
                        "raw_value": found,
                        "locator": "window['dataVersion']",
                        "url": url,
                        "resource_name": resource_name,
                        "sha256": result.sha256,
                        "observed_at": _now_utc(),
                    })

            scripts: list[dict[str, str]] = []
            seen_scripts: set[str] = set()
            for src in _SCRIPT_SRC_RE.findall(text):
                match = _DATA_JS_RE.search(src)
                if not match:
                    continue
                bundle = {
                    "role": match.group("role"),
                    "data_version": match.group("data_version"),
                    "stem": match.group("stem"),
                    "url": self._absolute_url(url, src),
                }
                if bundle["url"] in seen_scripts:
                    continue
                seen_scripts.add(bundle["url"])
                scripts.append(bundle)
                resources.setdefault(bundle["url"], {
                    "url": bundle["url"],
                    "role": bundle["role"],
                    "data_version": bundle["data_version"],
                    "script_stem": bundle["stem"],
                    "resource_name": f"{bundle['role']}_{_slug(bundle['stem'])}",
                    "discovered_from": [url],
                })
                if url not in resources[bundle["url"]]["discovered_from"]:
                    resources[bundle["url"]]["discovered_from"].append(url)
            row["script_count"] = len(scripts)
            if not scripts:
                self.add_issue(
                    issue_type="no_data_scripts_on_entry_page",
                    severity="warning",
                    source_snapshot_id=snapshot_id,
                    resource_name=resource_name,
                    message="entry page references no /data/<version>/<role>/js/*.js bundle",
                    evidence=url,
                )
            for signal_name, pattern in _PAGE_SIGNAL_RES.items():
                if signal_name in page_signals:
                    continue
                signal_match = pattern.search(text)
                if signal_match:
                    page_signals[signal_name] = {
                        "value": signal_match.group("value"),
                        "url": url,
                        "resource_name": resource_name,
                    }

        # Translations are not referenced by <script src>; they are derived from
        # the rediscovered data version directory (game data) and the documented
        # static_data prefix (site UI).  See _DEFAULT_TRANSLATION_RESOURCES.
        translation_resources = self._translation_resources(
            snapshot_id=snapshot_id,
            data_version=data_version,
            page_signals=page_signals,
            version_evidence=version_evidence,
        )
        for res in translation_resources:
            resources.setdefault(res["url"], res)

        claim: str | None = None
        if configured_claim:
            claim = str(configured_claim)
            version_evidence.append({
                "kind": "config_claim",
                "value": claim,
                "source": "config (sources config claimed_version)",
            })
        derived = _short_version(data_version)
        if derived and claim and derived != claim:
            self.add_issue(
                issue_type="claimed_version_mismatch",
                severity="warning",
                source_snapshot_id=snapshot_id,
                message=f"config claimed_version {claim!r} != dataVersion-derived {derived!r}",
                actual=data_version or "",
            )
        if derived and not claim:
            claim = derived
            version_evidence.append({
                "kind": "derived_from_data_version",
                "value": derived,
                "raw_value": data_version or "",
            })

        resource_list = self._sorted_resources(resources.values())
        if not resource_list:
            status = "failed"
        elif not any_page_ok or any(r.get("fetch_status") == "failed" for r in page_results):
            status = "partial"
        else:
            status = "ok"

        inventory: dict[str, Any] = {
            "source": self.name,
            "snapshot_id": snapshot_id,
            "entry_url": entry_url or (entry_pages[0] if entry_pages else None),
            "entry_pages": entry_pages,
            "discovered_at": _now_utc(),
            "offline": offline,
            "claimed_version": claim,
            "data_version": data_version,
            "version_evidence": version_evidence,
            "page_signals": page_signals,
            "status": status,
            "page_results": page_results,
            "source_urls": source_urls,
            "resources": resource_list,
        }
        self._write_inventory(snapshot_id, inventory)
        self._inventory = inventory
        self.logger(
            f"[discover] letools/{snapshot_id}: status={status} dataVersion={data_version} "
            f"bundles={len(resource_list)} pages_ok={sum(1 for p in page_results if p.get('fetch_status') != 'failed')}/{len(page_results)}"
        )
        return inventory

    def _translation_resources(
        self,
        *,
        snapshot_id: str,
        data_version: str | None,
        page_signals: dict[str, dict[str, Any]],
        version_evidence: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Derive the translation JSON endpoints for the current data version."""
        base_url = str(self.config.get("base_url") or "https://www.lastepochtools.com").rstrip("/")
        overrides = self.config.get("translation_resources")
        templates = overrides if isinstance(overrides, list) and overrides else list(_DEFAULT_TRANSLATION_RESOURCES)
        resources: list[dict[str, Any]] = []
        for spec in templates:
            if not isinstance(spec, dict):
                continue
            template = str(spec.get("path_template") or "")
            if not template:
                continue
            if spec.get("requires_data_version") and not data_version:
                self.add_issue(
                    issue_type="translation_url_not_derivable",
                    severity="warning",
                    source_snapshot_id=snapshot_id,
                    message=(
                        f"cannot derive translation resource {spec.get('resource_name')!r}: "
                        "dataVersion was not found on any entry page"
                    ),
                )
                continue
            path = template.replace("{data_version}", data_version or "")
            url = path if path.startswith("http") else base_url + ("" if path.startswith("/") else "/") + path
            resource = {
                "url": url,
                "role": "translations",
                "resource_name": str(spec.get("resource_name") or f"translations_{spec.get('namespace', 'unknown')}"),
                "namespace": spec.get("namespace", "unknown"),
                "language": spec.get("language", "en"),
                "format": "json",
                "path_template": template,
                "derivation": spec.get("derivation", ""),
                "discovered_from": [p.get("url") for p in page_signals.values() if p.get("url")],
            }
            if page_signals:
                resource["page_signals"] = page_signals
            if data_version:
                resource["data_version"] = data_version
            resources.append(resource)
            version_evidence.append({
                "kind": "translation_endpoint",
                "resource_name": resource["resource_name"],
                "namespace": resource["namespace"],
                "url": url,
                "derivation": resource["derivation"],
                "derived_from_data_version": data_version,
            })
        return resources

    @staticmethod
    def _absolute_url(page_url: str, src: str) -> str:
        if src.startswith("http://") or src.startswith("https://"):
            return src
        if src.startswith("//"):
            return "https:" + src
        if not src.startswith("/"):
            base = page_url.rsplit("/", 1)[0]
            return f"{base}/{src}"
        root = re.match(r"https?://[^/]+", page_url)
        return (root.group(0) if root else "") + src

    @staticmethod
    def _page_resource_name(url: str) -> str:
        path = re.sub(r"^https?://[^/]+", "", url)
        path = path.split("?", 1)[0].split("#", 1)[0]
        trimmed = path.strip("/")
        if not trimmed:
            return "page_root"
        return "page_" + _slug(trimmed.replace("/", "_"))

    @staticmethod
    def _sorted_resources(resources: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        def key(res: dict[str, Any]):
            role = res.get("role", "")
            order = _ROLE_ORDER.index(role) if role in _ROLE_ORDER else len(_ROLE_ORDER)
            return (order, role, res.get("script_stem", ""), res.get("url", ""))

        return sorted(resources, key=key)

    def _inventory_path(self, snapshot_id: str) -> Path:
        return self.raw_root / self.name / snapshot_id / "inventory.json"

    def _write_inventory(self, snapshot_id: str, inventory: dict[str, Any]) -> None:
        path = self._inventory_path(snapshot_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_inventory(self, snapshot_id: str) -> dict[str, Any] | None:
        path = self._inventory_path(snapshot_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    # =================================================================
    # fetch
    # =================================================================
    def fetch(
        self,
        snapshot_id: str,
        offline: bool = False,
        inventory: dict | None = None,
    ) -> list[dict]:
        """Fetch every discovered bundle sequentially (1 req/s by Fetcher policy)."""
        if inventory is None:
            inventory = self._load_inventory(snapshot_id)
        if inventory is None:
            inventory = self.discover(snapshot_id, offline=offline)
        self._inventory = inventory
        fetcher = self.fetcher(snapshot_id, offline=offline)
        resources: list[dict[str, Any]] = list(inventory.get("resources") or [])
        results: list[dict[str, Any]] = []
        for res in resources:
            url = res["url"]
            resource_name = res["resource_name"]
            result = fetcher.fetch(url, resource_name)
            row = result.to_dict()
            row["role"] = res.get("role")
            results.append(row)
            res["fetch_status"] = result.fetch_status
            res["sha256"] = result.sha256
            res["size"] = result.size
            res["content_type"] = result.content_type
            res["fetched_at"] = result.fetched_at
            res["http_status"] = result.http_status
            if result.fetch_status == "failed" or not result.path:
                res["error"] = result.error
                self.add_issue(
                    issue_type="bundle_fetch_failed",
                    severity="error",
                    source_snapshot_id=snapshot_id,
                    resource_name=resource_name,
                    message=f"data bundle fetch failed: {result.error}",
                    evidence=url,
                )
                continue
            # Enrich the inventory with facts that only the payload can confirm:
            # which window globals it defines and the game version it claims.
            try:
                text = Path(result.path).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:  # pragma: no cover - defensive
                self.add_issue(
                    issue_type="bundle_read_failed",
                    severity="error",
                    source_snapshot_id=snapshot_id,
                    resource_name=resource_name,
                    message=str(exc),
                    evidence=url,
                )
                continue
            if res.get("role") == "translations" or res.get("format") == "json":
                self._enrich_translation_resource(
                    ctx_res=res,
                    text=text,
                    url=url,
                    snapshot_id=snapshot_id,
                    resource_name=resource_name,
                    sha256=result.sha256,
                )
                continue
            windows = sorted(set(_WINDOW_ASSIGN_RE.findall(text)))
            res["windows"] = windows
            unknown = sorted(set(windows) - _KNOWN_WINDOWS)
            if unknown:
                res["unknown_windows"] = unknown
                self.add_issue(
                    issue_type="unknown_window_in_bundle",
                    severity="info",
                    source_snapshot_id=snapshot_id,
                    resource_name=resource_name,
                    message=f"bundle defines windows the adapter does not model yet: {unknown}",
                    evidence=url,
                )
            game_version = _GAME_VERSION_RE.search(text)
            if game_version:
                raw_value = game_version.group("value")
                claimed = raw_value.replace("Version", "").strip() or raw_value
                inventory["claimed_version"] = claimed
                inventory.setdefault("version_evidence", []).append({
                    "kind": "js_global_variable",
                    "name": "gameVersion",
                    "raw_value": raw_value,
                    "locator": "window.gameVersion",
                    "url": url,
                    "resource_name": resource_name,
                    "sha256": result.sha256,
                    "observed_at": _now_utc(),
                })
        inventory["fetched_at"] = _now_utc()
        inventory["resources"] = resources
        self._write_inventory(snapshot_id, inventory)
        # fetch_manifest.json is what le-data normalize reads for raw_resources
        # provenance; keep it next to the cached responses.
        manifest_path = self.raw_root / self.name / snapshot_id / "fetch_manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "source": self.name,
                    "source_snapshot_id": snapshot_id,
                    "fetched_at": inventory["fetched_at"],
                    "offline": offline,
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        ok = sum(1 for r in results if r["fetch_status"] in ("fetched", "cache_hit", "not_modified"))
        self.logger(
            f"[fetch] letools/{snapshot_id}: {ok}/{len(results)} bundles available "
            f"(claimed_version={inventory.get('claimed_version')})"
        )
        return results

    # =================================================================
    # parse
    # =================================================================
    def parse(self, snapshot_id: str) -> Iterator[SourceRecord]:
        """Yield SourceRecords from the cached responses only (never network)."""
        self._snapshot_id = snapshot_id
        self._window_first = {}
        inventory = self._load_inventory(snapshot_id)
        self._inventory = inventory
        fetcher = self.fetcher(snapshot_id, offline=True)
        resources = self._parse_resources(snapshot_id, inventory)
        if not resources:
            self.add_issue(
                issue_type="no_cached_resources",
                severity="error",
                source_snapshot_id=snapshot_id,
                message="no cached data bundle found; run discover+fetch first",
            )
            return
        for resource in resources:
            resource_name = resource["resource_name"]
            path = fetcher.body_path(resource_name)
            meta = fetcher.load_meta(resource_name) or {}
            if not path.exists():
                self.add_issue(
                    issue_type="missing_cached_resource",
                    severity="error",
                    source_snapshot_id=snapshot_id,
                    resource_name=resource_name,
                    message="resource is in the inventory but its body is not cached",
                    evidence=str(path),
                )
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if resource.get("role") == "translations" or resource.get("format") == "json":
                yield from self._parse_translation_resource(
                    snapshot_id=snapshot_id,
                    resource_name=resource_name,
                    namespace=str(resource.get("namespace") or ""),
                    language=str(resource.get("language") or "en"),
                    url=meta.get("url") or resource.get("url", ""),
                    sha256=meta.get("sha256") or resource.get("sha256", ""),
                    text=text,
                )
                continue
            yield from self._parse_bundle(
                snapshot_id=snapshot_id,
                resource_name=resource_name,
                role=resource.get("role", ""),
                url=meta.get("url") or resource.get("url", ""),
                sha256=meta.get("sha256") or resource.get("sha256", ""),
                text=text,
            )

    def _parse_resources(
        self, snapshot_id: str, inventory: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        if inventory and inventory.get("resources"):
            return list(inventory["resources"])
        # Fallback: rebuild the resource list from cached response metadata so
        # parse still works when inventory.json is absent.
        responses = self.raw_root / self.name / snapshot_id / "responses"
        if not responses.exists():
            return []
        resources: list[dict[str, Any]] = []
        for meta_path in sorted(responses.glob("*.meta.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            url = str(meta.get("url", ""))
            match = _DATA_JS_RE.search(url)
            if not match:
                if "/i18n" in url and url.endswith(".json"):
                    namespace = "full" if "/data/" in url else "site"
                    resources.append({
                        "url": url,
                        "role": "translations",
                        "namespace": namespace,
                        "language": "en",
                        "format": "json",
                        "resource_name": meta.get("resource_name") or meta_path.name[: -len(".meta.json")],
                        "sha256": meta.get("sha256", ""),
                    })
                continue
            resources.append({
                "url": url,
                "role": match.group("role"),
                "data_version": match.group("data_version"),
                "script_stem": match.group("stem"),
                "resource_name": meta.get("resource_name") or meta_path.name[: -len(".meta.json")],
                "sha256": meta.get("sha256", ""),
            })
        return self._sorted_resources(resources)

    # -- translation resources (JSON, not JS bundles) ----------------------
    def _enrich_translation_resource(
        self,
        *,
        ctx_res: dict[str, Any],
        text: str,
        url: str,
        snapshot_id: str,
        resource_name: str,
        sha256: str,
    ) -> None:
        ctx_res["format"] = "json"
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            ctx_res["valid_json"] = False
            self.add_issue(
                issue_type="translation_json_invalid",
                severity="error",
                source_snapshot_id=snapshot_id,
                resource_name=resource_name,
                entity_kind="translation",
                message=f"translation file is not valid JSON: {exc}",
                evidence=url,
            )
            return
        ctx_res["valid_json"] = True
        ctx_res["json_top_level"] = type(data).__name__
        entries = len(data) if isinstance(data, dict) else 0
        ctx_res["translation_entries"] = entries
        if isinstance(data, dict):
            ctx_res["translation_sample"] = dict(list(data.items())[:2])
            samples = {
                key: data[key]
                for key in (
                    "Abilities.Ability_HungeringSouls_Name",
                    "Skills.Skill_av75ch_5_Name",
                    "Item_Names.Item_SubType_Name_0_1",
                )
                if key in data
            }
            if samples:
                ctx_res["validation_samples"] = samples
        if entries == 0:
            self.add_issue(
                issue_type="translation_payload_empty",
                severity="warning",
                source_snapshot_id=snapshot_id,
                resource_name=resource_name,
                entity_kind="translation",
                message="translation JSON has no key/value entries",
                evidence=url,
            )

    def _parse_translation_resource(
        self,
        *,
        snapshot_id: str,
        resource_name: str,
        namespace: str,
        language: str,
        url: str,
        sha256: str,
        text: str,
    ) -> Iterator[SourceRecord]:
        namespace = namespace or "unknown"
        ctx = {
            "snapshot_id": snapshot_id,
            "resource_name": resource_name,
            "role": "translations",
            "url": url,
            "sha256": sha256,
        }
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            self.add_issue(
                issue_type="translation_json_invalid",
                severity="error",
                source_snapshot_id=snapshot_id,
                resource_name=resource_name,
                entity_kind="translation",
                message=f"translation file is not valid JSON: {exc}",
                evidence=url,
            )
            yield self._make_record(
                ctx,
                entity_kind="translation",
                source_scope=namespace,
                source_id=resource_name,
                window="json",
                anchor="json",
                raw={"parse_error": str(exc), "text_prefix": text[:2000]},
                parse_status=PARSE_FAILED,
                notes=["translation payload could not be decoded"],
            )
            return
        if not isinstance(data, dict):
            self.add_issue(
                issue_type="unexpected_translation_type",
                severity="error",
                source_snapshot_id=snapshot_id,
                resource_name=resource_name,
                entity_kind="translation",
                message=f"translation JSON top level is {type(data).__name__}, expected object",
                evidence=url,
            )
            return
        non_string = [key for key, value in data.items() if not isinstance(value, str)]
        if non_string:
            self.add_issue(
                issue_type="translation_non_string_value",
                severity="info",
                source_snapshot_id=snapshot_id,
                resource_name=resource_name,
                entity_kind="translation",
                message=f"{len(non_string)} translation entries have non-string values; preserved verbatim",
                evidence=f"{url} sample_keys={non_string[:5]}",
            )
        for key, value in data.items():
            yield self._make_record(
                ctx,
                entity_kind="translation",
                source_scope=namespace,
                source_id=str(key),
                window="json",
                path="",
                anchor=f"json[{key!r}]",
                raw={
                    "key": key,
                    "value": value,
                    "language": language,
                    "namespace": namespace,
                },
                notes=[f"translation namespace={namespace} language={language}"],
            )

    # -- bundle dispatch ----------------------------------------------------
    def _parse_bundle(
        self,
        *,
        snapshot_id: str,
        resource_name: str,
        role: str,
        url: str,
        sha256: str,
        text: str,
    ) -> Iterator[SourceRecord]:
        ctx = {
            "snapshot_id": snapshot_id,
            "resource_name": resource_name,
            "role": role,
            "url": url,
            "sha256": sha256,
        }
        windows: dict[str, Any] = {}
        for name, value_start in find_assignment_sites(text):
            try:
                value, _ = parse_literal(text, value_start)
            except UnsupportedJsConstruct as exc:
                self.add_issue(
                    issue_type="unsupported_js_construct",
                    severity="error",
                    source_snapshot_id=snapshot_id,
                    resource_name=resource_name,
                    entity_kind="unparsed_window",
                    source_id=name,
                    field_path=f"window.{name}",
                    message=f"literal parser does not support this construct: {exc}",
                    evidence=url,
                )
                yield self._make_record(
                    ctx,
                    entity_kind="unparsed_window",
                    source_scope="window",
                    source_id=name,
                    window=name,
                    path="",
                    raw={"window": name, "text_prefix": text[value_start:value_start + 4000],
                         "text_length": len(text) - value_start},
                    parse_status=PARSE_FAILED,
                    notes=["right-hand side is outside the supported JS literal subset"],
                )
                continue
            except Exception as exc:  # JsParseError and defensive catch
                self.add_issue(
                    issue_type="js_parse_error",
                    severity="error",
                    source_snapshot_id=snapshot_id,
                    resource_name=resource_name,
                    entity_kind="unparsed_window",
                    source_id=name,
                    field_path=f"window.{name}",
                    message=f"failed to parse assignment: {exc}",
                    evidence=url,
                )
                yield self._make_record(
                    ctx,
                    entity_kind="unparsed_window",
                    source_scope="window",
                    source_id=name,
                    window=name,
                    path="",
                    raw={"window": name, "text_prefix": text[value_start:value_start + 4000],
                         "text_length": len(text) - value_start},
                    parse_status=PARSE_FAILED,
                    notes=["assignment could not be parsed as a JS literal"],
                )
                continue
            windows[name] = value

        for name, value in windows.items():
            digest = self._value_digest(value)
            first = self._window_first.get(name)
            if first is None:
                self._window_first[name] = {
                    "digest": digest,
                    "resource_name": resource_name,
                    "url": url,
                }
            elif first["digest"] == digest:
                # The site ships some windows twice (per-page bundle variants);
                # byte-identical payloads are proven duplicates, so the entity
                # records are produced once and this copy is kept as evidence.
                yield self._make_record(
                    ctx,
                    entity_kind="duplicate_window",
                    # the resource name is part of the scope because a window can
                    # be duplicated by several bundles (e.g. LEAbilities in two
                    # planner variants) and record identity must stay unique
                    source_scope=f"duplicate:{resource_name}",
                    source_id=name,
                    window=name,
                    path="",
                    raw={
                        "window": name,
                        "value_sha256": digest,
                        "duplicate_of_resource": first["resource_name"],
                        "duplicate_of_url": first["url"],
                    },
                    parse_status=PARSE_SKIPPED,
                    notes=[
                        "window value is identical to the canonical copy from "
                        f"{first['resource_name']}; entities were emitted from that copy only"
                    ],
                )
                continue
            else:
                self.add_issue(
                    issue_type="divergent_duplicate_window",
                    severity="warning",
                    source_snapshot_id=snapshot_id,
                    resource_name=resource_name,
                    entity_kind="duplicate_window",
                    source_id=name,
                    field_path=f"window.{name}",
                    message=(
                        f"window.{name} differs from the canonical copy in "
                        f"{first['resource_name']}; both versions parsed"
                    ),
                    expected=first["digest"],
                    actual=digest,
                    evidence=url,
                )
            if name not in _KNOWN_WINDOWS:
                self.add_issue(
                    issue_type="unknown_window",
                    severity="info",
                    source_snapshot_id=snapshot_id,
                    resource_name=resource_name,
                    entity_kind="unknown_window",
                    source_id=name,
                    field_path=f"window.{name}",
                    message="data window is not modelled yet; payload preserved as a record",
                    evidence=url,
                )
                yield self._make_record(
                    ctx,
                    entity_kind="unknown_window",
                    source_scope="window",
                    source_id=name,
                    window=name,
                    path="",
                    raw={"window": name, "value": value},
                    parse_status=PARSE_UNPARSED,
                    notes=["unmodelled window; raw payload retained"],
                )
                continue
            handler = getattr(self, f"_parse_window_{name}", None)
            if handler is None and name in _PLANNER_SOURCE_MAPS:
                yield from self._emit_source_map(
                    ctx, "planner", name, value, entity_kind="relation_candidate"
                )
                continue
            if handler is None:
                self.add_issue(
                    issue_type="missing_window_handler",
                    severity="error",
                    source_snapshot_id=snapshot_id,
                    resource_name=resource_name,
                    entity_kind="unknown_window",
                    source_id=name,
                    field_path=f"window.{name}",
                    message="window is listed as known but has no handler (adapter bug)",
                    evidence=url,
                )
                yield self._make_record(
                    ctx,
                    entity_kind="unknown_window",
                    source_scope="window",
                    source_id=name,
                    window=name,
                    path="",
                    raw={"window": name, "value": value},
                    parse_status=PARSE_UNPARSED,
                )
                continue
            yield from handler(ctx, value)

    # -- record factory -----------------------------------------------------
    def _make_record(
        self,
        ctx: dict[str, Any],
        *,
        entity_kind: str,
        source_scope: str,
        source_id: str | int,
        window: str,
        path: str = "",
        raw: Any,
        parse_status: str = PARSE_OK,
        notes: Iterable[str] | None = None,
        anchor: str | None = None,
    ) -> SourceRecord:
        if anchor is not None:
            locator = f"{ctx['url'] or ctx['resource_name']}#{anchor}"
        else:
            locator = f"{ctx['url'] or ctx['resource_name']}#window.{window}"
            if path:
                locator = f"{locator}.{path}"
        if not isinstance(raw, dict):
            raw = {"value": raw}
        return SourceRecord(
            source=self.name,
            source_snapshot_id=ctx["snapshot_id"],
            resource_name=ctx["resource_name"],
            entity_kind=entity_kind,
            source_scope=source_scope,
            source_id=str(source_id),
            locator=locator,
            raw=js_to_jsonable(raw),
            parse_status=parse_status,
            notes=list(notes or []),
        )

    @staticmethod
    def _entry_id(entry: Any, fallback: str | int, *fields: str) -> str | int:
        if isinstance(entry, dict):
            for field in fields:
                if field in entry and entry[field] is not None:
                    return entry[field]
        return fallback

    # =================================================================
    # db bundle: itemDB, gameVersion, enums
    # =================================================================
    def _parse_window_gameVersion(self, ctx, value) -> Iterator[SourceRecord]:
        yield self._make_record(
            ctx, entity_kind="version", source_scope="itemDB bundle",
            source_id="gameVersion", window="gameVersion", path="",
            raw={"gameVersion": value},
            notes=["version claim made by the data bundle itself; strongest source evidence"],
        )

    def _parse_window_AT(self, ctx, value):
        yield from self._emit_enum(ctx, "AT", value)

    def _parse_window_SP(self, ctx, value):
        yield from self._emit_enum(ctx, "SP", value)

    def _parse_window_AilmentID(self, ctx, value):
        yield from self._emit_enum(ctx, "AilmentID", value)

    def _parse_window_LocalSP(self, ctx, value):
        yield from self._emit_enum(ctx, "LocalSP", value)

    def _emit_enum(self, ctx, name: str, value: Any) -> Iterator[SourceRecord]:
        yield self._make_record(
            ctx, entity_kind="enum", source_scope="enum", source_id=name,
            window=name, path="", raw=value,
            notes=[f"name->numeric enumeration with {len(value) if isinstance(value, dict) else '?'} entries"],
        )

    def _parse_window_itemDB(self, ctx, value) -> Iterator[SourceRecord]:
        if not isinstance(value, dict):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="itemDB", source_id="itemDB",
                message=f"itemDB is {type(value).__name__}, expected object", evidence=ctx["url"],
            )
            return
        for key, item in value.items():
            if key == "itemList":
                yield from self._parse_item_list(ctx, item)
            elif key == "affixList":
                yield from self._parse_affix_list(ctx, item)
            elif key == "uniqueList":
                yield from self._parse_unique_list(ctx, item)
            elif key == "setBonusList":
                yield from self._parse_set_bonus_list(ctx, item)
            elif key in _ITEMDB_SOURCE_MAPS:
                yield from self._emit_source_map(ctx, "itemDB", key, item, entity_kind="relation_candidate")
            else:
                yield self._make_record(
                    ctx, entity_kind="item_config", source_scope=f"itemDB.{key}",
                    source_id=key, window="itemDB", path=key, raw={"value": item},
                    notes=["itemDB config/auxiliary payload preserved as one record (not decomposed)"],
                )

    def _parse_item_list(self, ctx, item_list) -> Iterator[SourceRecord]:
        if not isinstance(item_list, dict):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="item_type", source_id="itemDB.itemList",
                message=f"itemList is {type(item_list).__name__}, expected object", evidence=ctx["url"],
            )
            return
        for group_name in ("equippable", "nonEquippable"):
            group = item_list.get(group_name)
            if not isinstance(group, dict):
                continue
            for base_key, base_type in group.items():
                if not isinstance(base_type, dict):
                    continue
                base_id = base_type.get("baseTypeId", base_key)
                yield self._make_record(
                    ctx, entity_kind="item_type",
                    source_scope=f"itemList.{group_name}", source_id=base_id,
                    window="itemDB", path=f"itemList.{group_name}[{base_key!r}]",
                    raw=base_type,
                )
                sub_items = base_type.get("subItems") or {}
                if not isinstance(sub_items, dict):
                    continue
                for sub_key, sub_type in sub_items.items():
                    if not isinstance(sub_type, dict):
                        continue
                    sub_id = sub_type.get("subTypeId", sub_key)
                    yield self._make_record(
                        ctx, entity_kind="item_base",
                        source_scope=f"itemList.{group_name}:{base_id}", source_id=sub_id,
                        window="itemDB",
                        path=f"itemList.{group_name}[{base_key!r}].subItems[{sub_key!r}]",
                        raw=sub_type,
                        notes=["item subtype/base; parent base type id is in source_scope"],
                    )
        for key, item in item_list.items():
            if key in ("equippable", "nonEquippable"):
                continue
            yield self._make_record(
                ctx, entity_kind="item_config", source_scope=f"itemList.{key}",
                source_id=key, window="itemDB", path=f"itemList.{key}",
                raw={"value": item},
            )

    def _parse_affix_list(self, ctx, affix_list) -> Iterator[SourceRecord]:
        if not isinstance(affix_list, dict):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="affix", source_id="itemDB.affixList",
                message=f"affixList is {type(affix_list).__name__}, expected object", evidence=ctx["url"],
            )
            return
        for category in ("singleAffixes", "multiAffixes"):
            group = affix_list.get(category)
            if not isinstance(group, dict):
                continue
            for key, affix in group.items():
                if not isinstance(affix, dict):
                    continue
                affix_id = affix.get("affixId", key)
                yield self._make_record(
                    ctx, entity_kind="affix", source_scope=f"affixList.{category}",
                    source_id=affix_id, window="itemDB",
                    path=f"affixList.{category}[{key!r}]", raw=affix,
                )
        for key, item in affix_list.items():
            if key in ("singleAffixes", "multiAffixes"):
                continue
            yield self._make_record(
                ctx, entity_kind="item_config", source_scope=f"affixList.{key}",
                source_id=key, window="itemDB", path=f"affixList.{key}",
                raw={"value": item},
            )

    def _parse_unique_list(self, ctx, unique_list) -> Iterator[SourceRecord]:
        if not isinstance(unique_list, dict):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="unique", source_id="itemDB.uniqueList",
                message=f"uniqueList is {type(unique_list).__name__}, expected object", evidence=ctx["url"],
            )
            return
        uniques = unique_list.get("uniques")
        for key, item in unique_list.items():
            if key == "uniques" and isinstance(item, dict):
                for unique_key, unique in item.items():
                    if not isinstance(unique, dict):
                        continue
                    unique_id = unique.get("uniqueId", unique_key)
                    yield self._make_record(
                        ctx, entity_kind="unique", source_scope="uniqueList.uniques",
                        source_id=unique_id, window="itemDB",
                        path=f"uniqueList.uniques[{unique_key!r}]", raw=unique,
                    )
                continue
            yield self._make_record(
                ctx, entity_kind="item_config", source_scope=f"uniqueList.{key}",
                source_id=key, window="itemDB", path=f"uniqueList.{key}",
                raw={"value": item},
            )
        if uniques is None:
            self.add_issue(
                issue_type="missing_expected_key", severity="warning",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="unique", source_id="itemDB.uniqueList",
                message="uniqueList has no 'uniques' object", evidence=ctx["url"],
            )

    def _parse_set_bonus_list(self, ctx, set_list) -> Iterator[SourceRecord]:
        if not isinstance(set_list, list):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="set_bonus", source_id="itemDB.setBonusList",
                message=f"setBonusList is {type(set_list).__name__}, expected array", evidence=ctx["url"],
            )
            return
        for index, entry in enumerate(set_list):
            if not isinstance(entry, dict):
                continue
            set_id = entry.get("setID", index)
            yield self._make_record(
                ctx, entity_kind="set_bonus", source_scope="setBonusList",
                source_id=set_id, window="itemDB", path=f"setBonusList[{index}]", raw=entry,
            )

    # =================================================================
    # planner bundle: coreDB, LEAbilities, class info, source maps
    # =================================================================
    def _parse_window_coreDB(self, ctx, value) -> Iterator[SourceRecord]:
        if not isinstance(value, dict):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="coreDB", source_id="coreDB",
                message=f"coreDB is {type(value).__name__}, expected object", evidence=ctx["url"],
            )
            return
        for key, item in value.items():
            path = key
            if key == "propertyList" and isinstance(item, list):
                for index, entry in enumerate(item):
                    yield self._make_record(
                        ctx, entity_kind="property", source_scope="coreDB.propertyList",
                        source_id=self._entry_id(entry, index, "property"),
                        window="coreDB", path=f"propertyList[{index}]", raw=entry,
                    )
            elif key == "playerPropertyList" and isinstance(item, list):
                for index, entry in enumerate(item):
                    yield self._make_record(
                        ctx, entity_kind="property", source_scope="coreDB.playerPropertyList",
                        source_id=index, window="coreDB", path=f"playerPropertyList[{index}]", raw=entry,
                        notes=["player-facing display config indexed by player property id"],
                    )
            elif key == "abilityPropertyList" and isinstance(item, list):
                for index, entry in enumerate(item):
                    yield self._make_record(
                        ctx, entity_kind="property", source_scope="coreDB.abilityPropertyList",
                        source_id=self._entry_id(entry, index, "abilityID"),
                        window="coreDB", path=f"abilityPropertyList[{index}]", raw=entry,
                        notes=["per-ability property display config; 'properties' is indexed by property index"],
                    )
            elif key == "coreAttributes" and isinstance(item, dict):
                for attr_key, entry in item.items():
                    yield self._make_record(
                        ctx, entity_kind="property", source_scope="coreDB.coreAttributes",
                        source_id=self._entry_id(entry, attr_key, "attributeName"),
                        window="coreDB", path=f"coreAttributes[{attr_key!r}]", raw=entry,
                    )
            elif key == "idolAltarPropertyList" and isinstance(item, dict):
                for sub_key, sub_item in item.items():
                    if sub_key == "list" and isinstance(sub_item, list):
                        for index, entry in enumerate(sub_item):
                            yield self._make_record(
                                ctx, entity_kind="property",
                                source_scope="coreDB.idolAltarPropertyList.list",
                                source_id=index, window="coreDB",
                                path=f"idolAltarPropertyList.list[{index}]", raw=entry,
                            )
                    else:
                        yield self._make_record(
                            ctx, entity_kind="property",
                            source_scope=f"coreDB.idolAltarPropertyList.{sub_key}",
                            source_id=sub_key, window="coreDB",
                            path=f"idolAltarPropertyList.{sub_key}", raw={"value": sub_item},
                            notes=["config object preserved as one record"],
                        )
            else:
                yield self._make_record(
                    ctx, entity_kind="property_config", source_scope=f"coreDB.{key}",
                    source_id=key, window="coreDB", path=path, raw={"value": item},
                    notes=["coreDB config/auxiliary payload preserved as one record"],
                )

    def _parse_window_LEAbilities(self, ctx, value) -> Iterator[SourceRecord]:
        if not isinstance(value, dict):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="LEAbilities", source_id="LEAbilities",
                message=f"LEAbilities is {type(value).__name__}, expected object", evidence=ctx["url"],
            )
            return
        for key, item in value.items():
            if key == "abilityList" and isinstance(item, dict):
                for ability_key, ability in item.items():
                    if not isinstance(ability, dict):
                        continue
                    ability_id = ability.get("id", ability_key)
                    yield self._make_record(
                        ctx, entity_kind="ability", source_scope="LEAbilities.abilityList",
                        source_id=ability_id, window="LEAbilities",
                        path=f"abilityList[{ability_key!r}]", raw=ability,
                    )
            elif key in ("allAbilityIdList", "playerAbilityIdList") and isinstance(item, list):
                yield self._make_record(
                    ctx, entity_kind="ability_id_index", source_scope="LEAbilities",
                    source_id=key, window="LEAbilities", path=key,
                    raw={key: item},
                    notes=["index -> ability id list; index 0 is null (no ability)"],
                )
            elif key == "ailmentList" and isinstance(item, dict):
                for ailment_key, ailment in item.items():
                    if not isinstance(ailment, dict):
                        continue
                    ailment_id = ailment.get("id", ailment_key)
                    yield self._make_record(
                        ctx, entity_kind="ailment", source_scope="LEAbilities.ailmentList",
                        source_id=ailment_id, window="LEAbilities",
                        path=f"ailmentList[{ailment_key!r}]", raw=ailment,
                    )
            elif key == "entityList" and isinstance(item, dict):
                for entity_key, entity in item.items():
                    yield self._make_record(
                        ctx, entity_kind="minion_entity", source_scope="LEAbilities.entityList",
                        source_id=entity_key, window="LEAbilities",
                        path=f"entityList[{entity_key!r}]", raw=entity,
                        notes=["summoned/minion actor template data"],
                    )
            elif key in ("ailmentTriggeredAbilities", "ailmentTriggeredAilments") and isinstance(item, dict):
                yield from self._emit_source_map(
                    ctx, "LEAbilities", key, item, entity_kind="relation_candidate"
                )
            elif key == "invocations" and isinstance(item, dict):
                for invocation_key, invocation in item.items():
                    yield self._make_record(
                        ctx, entity_kind="invocation", source_scope="LEAbilities.invocations",
                        source_id=invocation_key, window="LEAbilities",
                        path=f"invocations[{invocation_key!r}]", raw=invocation,
                        notes=["rune invocation -> ability/runes mapping"],
                    )
            elif key in ("runeIcons", "passivePointsFromQuests", "attributePointsFromQuests"):
                yield self._make_record(
                    ctx, entity_kind="ability_config", source_scope=f"LEAbilities.{key}",
                    source_id=key, window="LEAbilities", path=key,
                    raw={key: item},
                )
            else:
                yield self._make_record(
                    ctx, entity_kind="ability_config", source_scope=f"LEAbilities.{key}",
                    source_id=key, window="LEAbilities", path=key, raw={"value": item},
                )

    def _parse_window_classInfoForSources(self, ctx, value) -> Iterator[SourceRecord]:
        if not isinstance(value, list):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="class", source_id="classInfoForSources",
                message=f"classInfoForSources is {type(value).__name__}, expected array", evidence=ctx["url"],
            )
            return
        for index, entry in enumerate(value):
            if not isinstance(entry, dict):
                continue
            character_class = entry.get("characterClass") or {}
            class_id = character_class.get("classID", index)
            yield self._make_record(
                ctx, entity_kind="class", source_scope="classInfoForSources",
                source_id=class_id, window="classInfoForSources", path=f"[{index}]", raw=entry,
                notes=["full class entry incl. thirdMasteryUnlocked and characterClass"],
            )
            masteries = character_class.get("masteries") or []
            if isinstance(masteries, list):
                for mastery_index, mastery in enumerate(masteries):
                    if not isinstance(mastery, dict):
                        continue
                    yield self._make_record(
                        ctx, entity_kind="mastery", source_scope=f"class:{class_id}",
                        source_id=f"{class_id}.{mastery_index}", window="classInfoForSources",
                        path=f"[{index}].characterClass.masteries[{mastery_index}]", raw=mastery,
                        notes=[f"mastery index {mastery_index} of class {class_id}"],
                    )
            for ability in character_class.get("unlockableAbilities") or []:
                if not isinstance(ability, dict):
                    continue
                ability_id = ability.get("ability", "")
                yield self._make_record(
                    ctx, entity_kind="relation_candidate",
                    source_scope="classInfoForSources.unlockableAbilities",
                    source_id=f"{class_id}:{ability_id}", window="classInfoForSources",
                    path=f"[{index}].characterClass.unlockableAbilities",
                    raw={"classId": class_id, "entry": ability, "kind": "class_unlockable_ability"},
                    notes=["class -> ability with character level requirement"],
                )
            for known in character_class.get("knownAbilities") or []:
                yield self._make_record(
                    ctx, entity_kind="relation_candidate",
                    source_scope="classInfoForSources.knownAbilities",
                    source_id=f"{class_id}:{known}", window="classInfoForSources",
                    path=f"[{index}].characterClass.knownAbilities",
                    raw={"classId": class_id, "ability": known, "kind": "class_known_ability"},
                )
            for default_index, default_ability in enumerate(character_class.get("defaultAbilities") or []):
                # duplicates are meaningful here (default hotbar layout), so index
                # is part of the identity.
                yield self._make_record(
                    ctx, entity_kind="relation_candidate",
                    source_scope="classInfoForSources.defaultAbilities",
                    source_id=f"{class_id}:{default_index}", window="classInfoForSources",
                    path=f"[{index}].characterClass.defaultAbilities[{default_index}]",
                    raw={"classId": class_id, "index": default_index, "ability": default_ability,
                         "kind": "class_default_hotbar"},
                )
            starting_items = character_class.get("startingItems")
            if isinstance(starting_items, list):
                for item_index, item in enumerate(starting_items):
                    yield self._make_record(
                        ctx, entity_kind="relation_candidate",
                        source_scope="classInfoForSources.startingItems",
                        source_id=f"{class_id}:{item_index}", window="classInfoForSources",
                        path=f"[{index}].characterClass.startingItems[{item_index}]",
                        raw={"classId": class_id, "index": item_index, "item": item,
                             "kind": "class_starting_item"},
                    )
            masteries_raw = character_class.get("masteries") or []
            for mastery_index, mastery in enumerate(masteries_raw):
                if not isinstance(mastery, dict):
                    continue
                for ability in mastery.get("abilities") or []:
                    if not isinstance(ability, dict):
                        continue
                    ability_id = ability.get("ability", "")
                    yield self._make_record(
                        ctx, entity_kind="relation_candidate",
                        source_scope="classInfoForSources.masteryAbilities",
                        source_id=f"{class_id}.{mastery_index}:{ability_id}",
                        window="classInfoForSources",
                        path=f"[{index}].characterClass.masteries[{mastery_index}].abilities",
                        raw={"classId": class_id, "mastery": mastery_index, "masteryName": mastery.get("name"),
                             "entry": ability, "kind": "mastery_ability_unlock"},
                    )

    def _emit_source_map(
        self, ctx, window: str, map_name: str, value: Any, *, entity_kind: str
    ) -> Iterator[SourceRecord]:
        if isinstance(value, dict):
            for key, targets in value.items():
                yield self._make_record(
                    ctx, entity_kind=entity_kind, source_scope=f"{window}.{map_name}",
                    source_id=key, window=window, path=f"{map_name}[{key!r}]",
                    raw={"mapping": map_name, "key": key, "value": targets},
                    notes=[f"source relationship map {window}.{map_name}"],
                )
        else:
            yield self._make_record(
                ctx, entity_kind=entity_kind, source_scope=f"{window}.{map_name}",
                source_id=map_name, window=window, path=map_name,
                raw={"mapping": map_name, "value": value},
            )

    # =================================================================
    # skills bundle: skill trees, character trees, weaver tree, tooltip icons
    # =================================================================
    def _parse_window_LESkillTrees(self, ctx, value) -> Iterator[SourceRecord]:
        if not isinstance(value, dict):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="skill_tree", source_id="LESkillTrees",
                message=f"LESkillTrees is {type(value).__name__}, expected object", evidence=ctx["url"],
            )
            return
        for tree_key, tree in value.items():
            if not isinstance(tree, dict):
                yield self._make_record(
                    ctx, entity_kind="skill_tree", source_scope="LESkillTrees",
                    source_id=tree_key, window="LESkillTrees", path=f"[{tree_key!r}]",
                    raw={"value": tree}, parse_status=PARSE_UNPARSED,
                    notes=["tree value is not an object; raw preserved"],
                )
                continue
            tree_id = tree.get("treeID", tree_key)
            yield self._make_record(
                ctx, entity_kind="skill_tree", source_scope="LESkillTrees",
                source_id=tree_id, window="LESkillTrees", path=f"[{tree_key!r}]", raw=tree,
                notes=[f"nodes={len(tree.get('nodes') or {})}; ability={tree.get('ability')!r}"],
            )
            nodes = tree.get("nodes") or {}
            if not isinstance(nodes, dict):
                self.add_issue(
                    issue_type="unexpected_type", severity="error",
                    source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                    entity_kind="skill_tree_node", source_id=str(tree_id),
                    message=f"tree.nodes is {type(nodes).__name__}, expected object", evidence=ctx["url"],
                )
                continue
            for node_key, node in nodes.items():
                if not isinstance(node, dict):
                    continue
                node_id = node.get("id", node_key)
                yield self._make_record(
                    ctx, entity_kind="skill_tree_node", source_scope=str(tree_id),
                    source_id=node_id, window="LESkillTrees",
                    path=f"[{tree_key!r}].nodes[{node_key!r}]", raw=node,
                )

    def _parse_window_LESkillTreesUI(self, ctx, value) -> Iterator[SourceRecord]:
        if not isinstance(value, dict):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="skill_tree_ui", source_id="LESkillTreesUI",
                message=f"LESkillTreesUI is {type(value).__name__}, expected object", evidence=ctx["url"],
            )
            return
        for tree_key, ui in value.items():
            yield self._make_record(
                ctx, entity_kind="skill_tree_ui", source_scope="LESkillTreesUI",
                source_id=tree_key, window="LESkillTreesUI", path=f"[{tree_key!r}]", raw=ui,
                notes=["node layout (rect/icon/nodeSize) lives in children[]"],
            )

    def _parse_window_LECharTrees(self, ctx, value) -> Iterator[SourceRecord]:
        if not isinstance(value, dict):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="passive_tree", source_id="LECharTrees",
                message=f"LECharTrees is {type(value).__name__}, expected object", evidence=ctx["url"],
            )
            return
        trees = value.get("trees")
        if not isinstance(trees, list):
            self.add_issue(
                issue_type="missing_expected_key", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="passive_tree", source_id="LECharTrees",
                message="LECharTrees.trees is missing or not an array", evidence=ctx["url"],
            )
            return
        for index, entry in enumerate(trees):
            if not isinstance(entry, dict):
                continue
            character_tree = entry.get("characterTree") or {}
            character_class = character_tree.get("characterClass") or {}
            tree_id = character_class.get("treeID") or entry.get("ref") or f"index{index}"
            yield self._make_record(
                ctx, entity_kind="passive_tree", source_scope="LECharTrees.trees",
                source_id=tree_id, window="LECharTrees", path=f"trees[{index}]", raw=entry,
                notes=[f"class={character_class.get('classNameKey')!r}; nodes={len(character_tree.get('nodes') or {})}"],
            )
            nodes = character_tree.get("nodes") or {}
            if not isinstance(nodes, dict):
                self.add_issue(
                    issue_type="unexpected_type", severity="error",
                    source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                    entity_kind="passive_node", source_id=str(tree_id),
                    message=f"characterTree.nodes is {type(nodes).__name__}, expected object",
                    evidence=ctx["url"],
                )
                continue
            for node_key, node in nodes.items():
                if not isinstance(node, dict):
                    continue
                node_id = node.get("id", node_key)
                yield self._make_record(
                    ctx, entity_kind="passive_node", source_scope=str(tree_id),
                    source_id=node_id, window="LECharTrees",
                    path=f"trees[{index}].characterTree.nodes[{node_key!r}]", raw=node,
                )

    def _parse_window_LECharTreesUI(self, ctx, value) -> Iterator[SourceRecord]:
        if not isinstance(value, dict):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="passive_tree_ui", source_id="LECharTreesUI",
                message=f"LECharTreesUI is {type(value).__name__}, expected object", evidence=ctx["url"],
            )
            return
        for tree_key, ui in value.items():
            yield self._make_record(
                ctx, entity_kind="passive_tree_ui", source_scope="LECharTreesUI",
                source_id=tree_key, window="LECharTreesUI", path=f"[{tree_key!r}]", raw=ui,
                notes=["'info' holds per-mastery bonus description keys; 'nodes' holds node layout"],
            )

    def _parse_window_LEWeaverTree(self, ctx, value) -> Iterator[SourceRecord]:
        if not isinstance(value, dict):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="weaver_tree", source_id="LEWeaverTree",
                message=f"LEWeaverTree is {type(value).__name__}, expected object", evidence=ctx["url"],
            )
            return
        tree_id = value.get("treeID", "weaver")
        yield self._make_record(
            ctx, entity_kind="weaver_tree", source_scope="LEWeaverTree",
            source_id=tree_id, window="LEWeaverTree", path="", raw=value,
            notes=[f"version={value.get('version')}; nodes={len(value.get('nodes') or {})}"],
        )
        nodes = value.get("nodes") or {}
        if not isinstance(nodes, dict):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="weaver_node", source_id=str(tree_id),
                message=f"LEWeaverTree.nodes is {type(nodes).__name__}, expected object", evidence=ctx["url"],
            )
            return
        for node_key, node in nodes.items():
            if not isinstance(node, dict):
                continue
            node_id = node.get("id", node_key)
            yield self._make_record(
                ctx, entity_kind="weaver_node", source_scope=str(tree_id),
                source_id=node_id, window="LEWeaverTree", path=f"nodes[{node_key!r}]", raw=node,
            )

    def _parse_window_LEWeaverTreeUI(self, ctx, value) -> Iterator[SourceRecord]:
        if not isinstance(value, dict):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="weaver_tree_ui", source_id="LEWeaverTreeUI",
                message=f"LEWeaverTreeUI is {type(value).__name__}, expected object", evidence=ctx["url"],
            )
            return
        for tree_key, ui in value.items():
            yield self._make_record(
                ctx, entity_kind="weaver_tree_ui", source_scope="LEWeaverTreeUI",
                source_id=tree_key, window="LEWeaverTreeUI", path=f"[{tree_key!r}]", raw=ui,
                notes=["weaver node layout (children[] includes a background pseudo-node)"],
            )

    def _parse_window_LETooltipIcons(self, ctx, value) -> Iterator[SourceRecord]:
        yield self._make_record(
            ctx, entity_kind="skill_config", source_scope="LETooltipIcons",
            source_id="LETooltipIcons", window="LETooltipIcons", path="",
            raw={"value": value},
            notes=["property/tag -> sprite mapping used by tooltips"],
        )

    # =================================================================
    # skills bundle: historical diffs (version evidence only)
    # =================================================================
    def _parse_window_skillTreeDiffs(self, ctx, value):
        yield from self._emit_history(ctx, "skillTreeDiffs", value)

    def _parse_window_passiveTreeDiffs(self, ctx, value):
        yield from self._emit_history(ctx, "passiveTreeDiffs", value)

    def _parse_window_abilityDiffs(self, ctx, value):
        yield from self._emit_history(ctx, "abilityDiffs", value)

    def _parse_window_ailmentDiffs(self, ctx, value):
        yield from self._emit_history(ctx, "ailmentDiffs", value)

    def _parse_window_diffs(self, ctx, value):
        yield from self._emit_history(ctx, "diffs", value, scope="item-history")

    def _emit_history(self, ctx, name: str, value: Any, scope: str = "skills-history") -> Iterator[SourceRecord]:
        count = len(value) if isinstance(value, dict) else 0
        self.add_issue(
            issue_type="payload_retained_not_expanded",
            severity="info",
            source_snapshot_id=ctx["snapshot_id"],
            resource_name=ctx["resource_name"],
            entity_kind="version_history",
            source_id=name,
            field_path=f"window.{name}",
            message=(
                f"historical change log with {count} version keys kept as one version-evidence "
                "record; not expanded into per-entity records (workguide 6.3)"
            ),
            evidence=ctx["url"],
        )
        yield self._make_record(
            ctx, entity_kind="version_history", source_scope=scope,
            source_id=name, window=name, path="", raw={"value": value},
            parse_status=PARSE_SKIPPED,
            notes=[
                "historical diff payload: retained verbatim as version evidence; not normalised "
                "into entities (large, superseded snapshots)",
            ],
        )

    # =================================================================
    # planner page: buff definitions, bonus tables, HUD icons
    # =================================================================
    def _parse_window_UIBuffs(self, ctx, value) -> Iterator[SourceRecord]:
        if not isinstance(value, dict):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="buff", source_id="UIBuffs",
                message=f"UIBuffs is {type(value).__name__}, expected object", evidence=ctx["url"],
            )
            return
        for buff_key, buff in value.items():
            if not isinstance(buff, dict):
                continue
            buff_id = buff.get("id", buff_key)
            yield self._make_record(
                ctx, entity_kind="buff", source_scope="UIBuffs",
                source_id=buff_id, window="UIBuffs", path=f"[{buff_key!r}]", raw=buff,
                notes=["UI buff/debuff definition: display keys, maxInstances, duration flags, positive flag"],
            )

    def _parse_window_UIBuffIds(self, ctx, value):
        yield from self._emit_enum(ctx, "UIBuffIds", value)

    def _parse_window_BuffId(self, ctx, value):
        yield from self._emit_enum(ctx, "BuffId", value)

    def _parse_window_BuffPropId(self, ctx, value):
        yield from self._emit_enum(ctx, "BuffPropId", value)

    def _parse_window_CustomConditionType(self, ctx, value):
        yield from self._emit_enum(ctx, "CustomConditionType", value)

    def _parse_window_LEHUD(self, ctx, value) -> Iterator[SourceRecord]:
        yield self._make_record(
            ctx, entity_kind="planner_config", source_scope="planner.LEHUD",
            source_id="LEHUD", window="LEHUD", path="", raw={"value": value},
            notes=["HUD icon mapping (move/potion/portal/rune icons)"],
        )

    def _parse_window_skillBonuses(self, ctx, value):
        yield from self._emit_bonus_table(ctx, "skillBonuses", "skill_bonus", value)

    def _parse_window_partialSkillBonuses(self, ctx, value):
        yield from self._emit_bonus_table(ctx, "partialSkillBonuses", "skill_bonus", value)

    def _parse_window_passiveBonuses(self, ctx, value):
        yield from self._emit_bonus_table(ctx, "passiveBonuses", "passive_bonus", value)

    def _emit_bonus_table(self, ctx, window: str, entity_kind: str, value: Any) -> Iterator[SourceRecord]:
        if not isinstance(value, dict):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind=entity_kind, source_id=window,
                message=f"{window} is {type(value).__name__}, expected object", evidence=ctx["url"],
            )
            return
        for key, table in value.items():
            yield self._make_record(
                ctx, entity_kind=entity_kind, source_scope=f"planner.{window}",
                source_id=key, window=window, path=f"[{key!r}]", raw=table,
                notes=["stat/damage-bonus table aggregated by the planner at runtime"],
            )

    # =================================================================
    # checklist bundle: quests and quest rewards
    # =================================================================
    def _parse_window_checklist(self, ctx, value) -> Iterator[SourceRecord]:
        if not isinstance(value, dict):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="quest", source_id="checklist",
                message=f"checklist is {type(value).__name__}, expected object", evidence=ctx["url"],
            )
            return
        for key, item in value.items():
            if key == "questList" and isinstance(item, dict):
                for quest_key, quest in item.items():
                    if not isinstance(quest, dict):
                        continue
                    quest_id = quest.get("id", quest_key)
                    yield self._make_record(
                        ctx, entity_kind="quest", source_scope="checklist.questList",
                        source_id=quest_id, window="checklist",
                        path=f"questList[{quest_key!r}]", raw=quest,
                        notes=["quest steps and rewards incl. passive/idol/attribute rewards"],
                    )
                continue
            yield self._make_record(
                ctx, entity_kind="quest_config", source_scope=f"checklist.{key}",
                source_id=key, window="checklist", path=key,
                raw={"value": item},
            )

    # =================================================================
    # endgame bundle
    # =================================================================
    def _parse_window_monsterMods(self, ctx, value):
        yield from self._emit_endgame_window(ctx, "monsterMods", value)

    def _parse_window_monolithRewards(self, ctx, value):
        yield from self._emit_endgame_window(ctx, "monolithRewards", value)

    def _parse_window_timelineData(self, ctx, value):
        yield from self._emit_endgame_window(ctx, "timelineData", value)

    def _parse_window_arenaData(self, ctx, value):
        yield from self._emit_endgame_window(ctx, "arenaData", value)

    def _parse_window_dungeonData(self, ctx, value):
        yield from self._emit_endgame_window(ctx, "dungeonData", value)

    def _parse_window_vaultsOfUncertainFate(self, ctx, value):
        yield from self._emit_endgame_window(ctx, "vaultsOfUncertainFate", value)

    def _parse_window_dropBossLoot(self, ctx, value):
        yield from self._emit_endgame_window(ctx, "dropBossLoot", value)

    def _parse_window_wovenEchoList(self, ctx, value):
        yield from self._emit_endgame_window(ctx, "wovenEchoList", value)

    def _emit_endgame_window(self, ctx, name: str, value: Any) -> Iterator[SourceRecord]:
        yield self._make_record(
            ctx, entity_kind="endgame_config", source_scope="endgame",
            source_id=name, window=name, path="", raw={"value": value},
            notes=["endgame/monolith auxiliary payload kept as one complete record"],
        )

    # =================================================================
    # factions bundle
    # =================================================================
    def _parse_window_factionList(self, ctx, value) -> Iterator[SourceRecord]:
        if not isinstance(value, list):
            self.add_issue(
                issue_type="unexpected_type", severity="error",
                source_snapshot_id=ctx["snapshot_id"], resource_name=ctx["resource_name"],
                entity_kind="faction", source_id="factionList",
                message=f"factionList is {type(value).__name__}, expected array", evidence=ctx["url"],
            )
            return
        for index, entry in enumerate(value):
            if not isinstance(entry, dict):
                continue
            faction_id = entry.get("factionId", index)
            yield self._make_record(
                ctx, entity_kind="faction", source_scope="factionList",
                source_id=faction_id, window="factionList", path=f"[{index}]", raw=entry,
            )

    def _parse_window_prophecyData(self, ctx, value):
        yield from self._emit_faction_config(ctx, "prophecyData", value)

    def _parse_window_merchantsGuildBagData(self, ctx, value):
        yield from self._emit_faction_config(ctx, "merchantsGuildBagData", value)

    def _emit_faction_config(self, ctx, name: str, value: Any) -> Iterator[SourceRecord]:
        yield self._make_record(
            ctx, entity_kind="faction_config", source_scope="factions",
            source_id=name, window=name, path="", raw={"value": value},
            notes=["faction auxiliary payload kept as one complete record"],
        )

    # =================================================================
    # issue helpers kept for subclass/test use
    # =================================================================
    def add_source_issue(self, issue: SourceIssue) -> None:
        self.issues.append(issue)
