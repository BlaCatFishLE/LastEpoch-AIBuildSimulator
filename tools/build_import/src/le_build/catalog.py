"""Read-only catalog reader over the phase-1 normalized dataset.

The build importer never opens the catalog for writing and never looks up the live
website.  Every entity reference is resolved through ``source_id_map`` using the full
namespace ``source_snapshot_id + source + kind + source_scope + source_id``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .paths import load_json, sha256_file

DISPLAY_META_VERSION = "letools-display-meta-v1"
_DISPLAY_META_PATH = Path(__file__).resolve().parent / "data" / "letools_display_meta_v152.json"


def _load_display_meta() -> dict:
    try:
        with open(_DISPLAY_META_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):  # pragma: no cover - defensive
        return {}


DISPLAY_META = _load_display_meta()
SP_ENUM: dict = DISPLAY_META.get("sp_enum") or {}
AILMENT_IDS: dict = DISPLAY_META.get("ailment_ids") or {}
SP_BY_VALUE = {value: name for name, value in SP_ENUM.items()}
AILMENT_BY_VALUE = {value: name for name, value in AILMENT_IDS.items()}
_DISPLAY_META_SPECIAL = DISPLAY_META.get("special_property_display") or {}
SPECIAL_PERCENT_IDS = set(_DISPLAY_META_SPECIAL.get("percent_ids") or [])
SPECIAL_ROUND_IDS = set(_DISPLAY_META_SPECIAL.get("round_added_to_int_ids") or [])
SPECIAL_TEMPLATES = _DISPLAY_META_SPECIAL.get("templates") or {}
DISPLAY_FLAG_OVERRIDES = DISPLAY_META.get("display_flag_overrides") or []
_SNAKE_FLAG_KEYS = {
    "displayAddedAsPercentage": "display_added_as_percentage",
    "displayAsPercentageOf": "display_as_percentage_of",
    "displayAddedAsTenthOfValue": "display_added_as_tenth",
    "roundAddedToInt": "round_added_to_int",
    "dontDisplayPlus": "dont_display_plus",
    "displayAsAddedTo": "display_as_added_to",
    "nameIncludesModifier": "name_includes_modifier",
    "roundingForMore": "rounding_for_more",
}


def _prettify_property_name(name: str) -> str:
    """Humanise an SP enum identifier (``EffectOfAilmentOnYou`` -> ``Effect Of Ailment On You``)."""
    import re as _re

    spaced = _re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return " ".join(word.capitalize() if word.islower() else word for word in spaced.split())



class CatalogError(RuntimeError):
    """Raised when the catalog cannot be opened or fails its fingerprint check."""


# Property-type markers observed in the archived LE Tools runtime script
# (planner-runtime.js, ``window.AT`` enum + ``getProperty``/``ce`` implementation).
# When an item/affix mod carries one of these markers, ``tags``/``specialTag`` select
# the concrete property instead of ``property`` being a coreDB.propertyList id.
PROPERTY_MARKER_ABILITY = 58  # window.AT.AbilityProperty -> coreDB.abilityPropertyList[tags].properties[specialTag]
PROPERTY_MARKER_PLAYER = 98  # window.AT.PlayerProperty -> coreDB.playerPropertyList[tags]
PROPERTY_MARKER_IDOL_ALTAR = 130  # SP.IdolAltarProperty -> coreDB.idolAltarPropertyList.list[tags]
# LE Tools unique ``tooltipEntries[].modDisplay``: values below 128 address the mod array,
# values at/above 128 address ``tooltipDescriptions[modDisplay - 128]`` (see
# Catalog.unique_tooltip for the per-unique evidence).
MOD_DISPLAY_DESCRIPTION_BASE = 128
# Archived HitEventTag enum: planner-runtime.js `const rrli={}; rrl$a="None Hit Crit Kill
# Freeze Stun Block MeleeHit Parry"` (indices 0..8). SP.WardGain (propertyList 39) appends
# the event suffix through Tb(specialTag) + translation
# `planner.stats.extra.ward_gain.<event>` (archived runtime line `case SP.WardGain:...`).
HIT_EVENT_TAG_BY_VALUE = {
    1: "hit",
    2: "crit",
    3: "kill",
    4: "freeze",
    5: "stun",
    6: "block",
    7: "melee_hit",
    8: "parry",
}
HIT_EVENT_PROPERTY_SLUGS = {39: "ward_gain"}
# User scope 2026-11-04 (re-review R1): drop-rate modifiers are not part of the BD / DPS
# input.  They are filtered from the default player display and calculator export by their
# source property identity (coreDB.propertyList id 104 = Increased Drop Rate), never by text.
NON_DPS_PROPERTIES = frozenset({104})
PROPERTY_RULE_VERSION = "letools-property-resolution-v1"

_TABLE_COLUMNS_CACHE: dict[tuple[str, str], list[str]] = {}


class Catalog:
    """Thin, cached, read-only access layer over one released dataset."""

    def __init__(
        self,
        db_path: Path,
        *,
        dataset_id: str | None = None,
        pointer: dict | None = None,
        manifest: dict | None = None,
        verify: bool = True,
    ):
        self.db_path = Path(db_path)
        if not self.db_path.is_file():
            raise CatalogError(f"catalog database not found: {self.db_path}")
        self.dataset_id = dataset_id
        self.pointer = pointer or {}
        self.manifest = manifest or {}
        self.verification: dict = {"verified": False, "checks": []}
        try:
            uri = f"file:{self.db_path.as_posix()}?mode=ro&immutable=1"
            self._conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            raise CatalogError(f"cannot open catalog read-only: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._cache: dict = {}
        self._meta = self._read_dataset_meta()
        if dataset_id is None:
            self.dataset_id = self._meta.get("dataset_id")
        if verify:
            self.verification = self._verify_fingerprints()

    # ------------------------------------------------------------------ opening
    @classmethod
    def open(
        cls,
        *,
        root: Path,
        dataset: str | None = None,
        db: str | Path | None = None,
        verify: bool = True,
    ) -> "Catalog":
        pointer = None
        manifest = None
        current_path = Path(root) / "data" / "current.json"
        if current_path.is_file():
            pointer = load_json(current_path)
        if db is not None:
            db_path = Path(db)
            if not db_path.is_absolute():
                db_path = Path(root) / db_path
            dataset_id = dataset or (pointer or {}).get("dataset_id")
            manifest = cls._manifest_for(db_path)
            return cls(db_path, dataset_id=dataset_id, pointer=pointer, manifest=manifest, verify=verify)
        dataset_id = dataset or (pointer or {}).get("dataset_id")
        if not dataset_id:
            raise CatalogError("no dataset selected: pass --dataset or provide data/current.json")
        release_dir = Path(root) / "data" / "releases" / dataset_id
        db_path = release_dir / "data.sqlite"
        if not db_path.is_file():
            raise CatalogError(f"dataset release database not found: {db_path}")
        manifest = {}
        manifest_path = release_dir / "manifest.json"
        if manifest_path.is_file():
            manifest = load_json(manifest_path)
        return cls(db_path, dataset_id=dataset_id, pointer=pointer, manifest=manifest, verify=verify)

    @staticmethod
    def _manifest_for(db_path: Path) -> dict:
        candidate = db_path.parent / "manifest.json"
        if candidate.is_file():
            return load_json(candidate)
        return {}

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ generic
    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(row) for row in self._conn.execute(sql, args)]

    def _row(self, sql: str, args: tuple = ()) -> dict | None:
        row = self._conn.execute(sql, args).fetchone()
        return dict(row) if row is not None else None

    def _columns(self, table: str) -> list[str]:
        key = (str(self.db_path), table)
        cached = _TABLE_COLUMNS_CACHE.get(key)
        if cached is None:
            cached = [row["name"] for row in self._rows(f"PRAGMA table_info({table})")]
            _TABLE_COLUMNS_CACHE[key] = cached
        return cached

    def table_count(self, table: str, where: str = "", args: tuple = ()) -> int:
        sql = f"SELECT COUNT(*) AS n FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return int(self._row(sql, args)["n"])

    # ------------------------------------------------------------------ metadata
    def _read_dataset_meta(self) -> dict:
        row = self._row("SELECT * FROM dataset_meta LIMIT 1")
        return row or {}

    def meta(self) -> dict:
        meta = dict(self._meta)
        for key in ("compatibility_conclusion",):
            if isinstance(meta.get(key), str):
                try:
                    meta[key] = json.loads(meta[key])
                except json.JSONDecodeError:
                    pass
        return meta

    def fingerprint(self) -> dict:
        row = self._row("SELECT * FROM dataset_fingerprints LIMIT 1")
        return row or {}

    def source_snapshot(self, source: str = "letools") -> dict | None:
        rows = self._rows(
            "SELECT * FROM source_snapshots WHERE source = ? ORDER BY source_snapshot_id LIMIT 1",
            (source,),
        )
        return rows[0] if rows else None

    def snapshot_id(self, source: str = "letools") -> str:
        snapshot = self.source_snapshot(source)
        if not snapshot:
            raise CatalogError(f"catalog has no source snapshot for source={source!r}")
        return snapshot["source_snapshot_id"]

    def dataset_source_snapshots(self) -> list[dict]:
        return self._rows(
            "SELECT * FROM dataset_source_snapshots WHERE dataset_id = ?", (self.dataset_id,)
        )

    def counts(self) -> dict:
        tables = [
            "source_snapshots",
            "raw_resources",
            "source_records",
            "entities",
            "entity_names",
            "source_id_map",
            "classes",
            "masteries",
            "skills",
            "trees",
            "nodes",
            "node_edges",
            "stats",
            "effects",
            "effect_values",
            "buffs",
            "ailments",
            "items",
            "affixes",
            "affix_tiers",
            "idols",
            "blessings",
            "minions",
            "entity_relationships",
            "field_evidence",
            "data_issues",
            "translations",
        ]
        return {table: self.table_count(table) for table in tables}

    def _verify_fingerprints(self) -> dict:
        """Verify the DB file against the pointer/manifest and its own fingerprint rows."""
        result: dict = {"verified": False, "checks": [], "file_sha256": None, "dataset_id": None}
        problems: list[str] = []
        file_sha = sha256_file(self.db_path)
        result["file_sha256"] = file_sha
        result["dataset_id"] = self.dataset_id

        own = self.fingerprint()
        meta = self._meta
        result["logical_sha256"] = own.get("logical_sha256")
        if meta.get("dataset_id") and self.dataset_id and meta["dataset_id"] != self.dataset_id:
            problems.append(
                f"dataset id mismatch: database has {meta['dataset_id']!r}, requested {self.dataset_id!r}"
            )
        result["checks"].append(
            {"check": "dataset_meta.dataset_id", "expected": self.dataset_id, "actual": meta.get("dataset_id")}
        )

        expected_file = (self.manifest or {}).get("database", {}).get("sha256")
        expected_logical = (self.manifest or {}).get("database", {}).get("logical_content_sha256")
        pointer_matches_db = bool(
            self.pointer
            and self.pointer.get("dataset_id") == self.dataset_id
            and (self.pointer.get("file_sha256") in (None, file_sha))
        )
        if expected_file is None and self.pointer and pointer_matches_db:
            expected_file = self.pointer.get("file_sha256")
        if expected_logical is None and self.pointer and pointer_matches_db:
            expected_logical = self.pointer.get("logical_content_sha256")

        if expected_file:
            result["checks"].append(
                {"check": "file_sha256", "expected": expected_file, "actual": file_sha}
            )
            if expected_file != file_sha:
                problems.append(f"file sha256 mismatch: expected {expected_file}, got {file_sha}")
        else:
            result["checks"].append({"check": "file_sha256", "expected": None, "actual": file_sha, "note": "no manifest/pointer hash available"})

        if expected_logical:
            result["checks"].append(
                {"check": "logical_sha256", "expected": expected_logical, "actual": own.get("logical_sha256")}
            )
            if expected_logical != own.get("logical_sha256"):
                problems.append(
                    "logical content sha256 mismatch: expected "
                    f"{expected_logical}, got {own.get('logical_sha256')}"
                )

        result["problems"] = problems
        result["verified"] = not problems
        if problems:
            raise CatalogError(
                "catalog fingerprint verification failed: " + "; ".join(problems)
            )
        return result

    # ------------------------------------------------------------- source lookup
    def source_lookup(
        self, kind: str, source_scope: str, source_id: str, *, source: str = "letools",
        snapshot_id: str | None = None,
    ) -> list[dict]:
        snapshot = snapshot_id or self.snapshot_id(source)
        return self._rows(
            """
            SELECT m.mapping_id, m.source_snapshot_id, m.source, m.kind, m.source_scope,
                   m.source_id, m.entity_id, m.mapping_method, m.evidence,
                   e.kind AS entity_kind, e.canonical_name, e.record_status
            FROM source_id_map AS m
            LEFT JOIN entities AS e ON e.entity_id = m.entity_id
            WHERE m.source_snapshot_id = ? AND m.source = ? AND m.kind = ?
              AND m.source_scope = ? AND m.source_id = ?
            """,
            (snapshot, source, kind, source_scope, str(source_id)),
        )

    def source_lookup_any_scope(
        self, kind: str, source_id: str, *, source: str = "letools"
    ) -> list[dict]:
        snapshot = self.snapshot_id(source)
        return self._rows(
            """
            SELECT m.mapping_id, m.source_snapshot_id, m.source, m.kind, m.source_scope,
                   m.source_id, m.entity_id, m.mapping_method, m.evidence,
                   e.kind AS entity_kind, e.canonical_name
            FROM source_id_map AS m
            LEFT JOIN entities AS e ON e.entity_id = m.entity_id
            WHERE m.source_snapshot_id = ? AND m.source = ? AND m.kind = ? AND m.source_id = ?
            """,
            (snapshot, source, kind, str(source_id)),
        )

    def scopes_for_kind(self, kind: str, *, source: str = "letools") -> list[dict]:
        snapshot = self.snapshot_id(source)
        return self._rows(
            """
            SELECT source_scope, COUNT(*) AS n FROM source_id_map
            WHERE source_snapshot_id = ? AND source = ? AND kind = ?
            GROUP BY source_scope ORDER BY source_scope
            """,
            (snapshot, source, kind),
        )

    # ----------------------------------------------------------------- entities
    def entity(self, entity_id: str) -> dict | None:
        return self._row("SELECT * FROM entities WHERE entity_id = ?", (entity_id,))

    def names(self, entity_id: str) -> dict:
        key = ("names", entity_id)
        if key not in self._cache:
            rows = self._rows(
                "SELECT language, name, is_alias FROM entity_names WHERE entity_id = ? "
                "ORDER BY is_alias, language, name",
                (entity_id,),
            )
            result: dict = {}
            for row in rows:
                result.setdefault(row["language"], []).append(row["name"])
            self._cache[key] = result
        return self._cache[key]

    def name(self, entity_id: str, language: str = "en") -> str | None:
        names = self.names(entity_id)
        if names.get(language):
            return names[language][0]
        entity = self.entity(entity_id)
        if entity and entity.get("canonical_name"):
            return entity["canonical_name"]
        for values in names.values():
            if values:
                return values[0]
        return None

    def display_names(self, entity_id: str) -> dict:
        names = self.names(entity_id)
        return {lang: values[0] for lang, values in names.items() if values}

    # ------------------------------------------------------- character / trees
    def class_by_source_id(self, class_source_id) -> list[dict]:
        return self.source_lookup("class", "classInfoForSources", str(class_source_id))

    def mastery_by_source_id(self, class_source_id, mastery_id) -> list[dict]:
        return self.source_lookup(
            "mastery", f"class:{class_source_id}", f"{class_source_id}.{mastery_id}"
        )

    def mastery(self, entity_id: str) -> dict | None:
        return self._row("SELECT * FROM masteries WHERE entity_id = ?", (entity_id,))

    def passive_tree_for_class(self, class_entity_id: str) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM trees WHERE tree_kind = 'passive' AND class_entity_id = ?",
            (class_entity_id,),
        )
        return rows

    def weaver_trees(self) -> list[dict]:
        return self._rows("SELECT * FROM trees WHERE tree_kind = 'weaver'")

    def skill_tree_by_source(self, tree_id: str) -> list[dict]:
        return self.source_lookup("skill_tree", "LESkillTrees", tree_id)

    def tree(self, entity_id: str) -> dict | None:
        return self._row("SELECT * FROM trees WHERE entity_id = ?", (entity_id,))

    def nodes_of_tree(self, tree_entity_id: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM nodes WHERE tree_entity_id = ? ORDER BY node_index", (tree_entity_id,)
        )

    def node_by_source(self, kind: str, scope: str, source_id) -> list[dict]:
        lookups = self.source_lookup(kind, scope, str(source_id))
        return lookups

    def node(self, entity_id: str) -> dict | None:
        return self._row("SELECT * FROM nodes WHERE entity_id = ?", (entity_id,))

    def skill(self, entity_id: str) -> dict | None:
        return self._row("SELECT * FROM skills WHERE entity_id = ?", (entity_id,))

    # ------------------------------------------------------------ items/affixes
    def item(self, entity_id: str) -> dict | None:
        row = self._row(
            """
            SELECT e.entity_id, e.kind, e.canonical_name, e.record_status,
                   i.item_category, i.item_type, i.base_type, i.is_unique, i.is_set,
                   i.level_requirement, i.class_requirement, i.implicit_text,
                   i.gear_category, i.equip_slot, i.extensions
            FROM entities AS e LEFT JOIN items AS i ON i.entity_id = e.entity_id
            WHERE e.entity_id = ?
            """,
            (entity_id,),
        )
        return row

    def item_entity(self, entity_id: str) -> dict | None:
        row = self.entity(entity_id)
        if row is None:
            return None
        for table in ("items", "idols", "blessings"):
            extra = self._row(f"SELECT * FROM {table} WHERE entity_id = ?", (entity_id,))
            if extra:
                merged = dict(row)
                for key, value in extra.items():
                    merged.setdefault(key, value)
                return merged
        return row

    def item_implicits(self, entity_id: str) -> list[dict]:
        """Backwards-compatible accessor: implicit definitions only."""
        return self.item_definition(entity_id)["implicits"]

    def item_definition(self, entity_id: str) -> dict:
        """Unified item/idol/blessing definition accessor with provenance.

        The phase-1 normalizer stores item implicits in ``items.implicit_text`` but
        keeps blessing/idol rows only as identity/category rows (see the 2026-11-03
        review P1-4).  For those entity kinds the original ``item_base`` payload is
        still preserved in ``source_records``; when the normalized field is missing we
        fall back to that archived record via the entity's ``source_id_map`` binding
        and record the provenance so importers never query the DB from the viewer.
        """
        key = ("item_definition", entity_id)
        if key in self._cache:
            return self._cache[key]
        row = self.item(entity_id) or {}
        entity = self.entity(entity_id) or {}
        kind = entity.get("kind") or row.get("kind")
        extensions: dict = {}
        if row.get("extensions"):
            try:
                extensions = json.loads(row["extensions"])
            except (TypeError, json.JSONDecodeError):
                extensions = {}
        implicits: list = []
        provenance: dict = {"source": "unavailable"}
        if kind == "item" and row:
            raw = row.get("implicit_text")
            if raw is None:
                implicits = []
                provenance = {"source": "items.implicit_text", "status": "absent"}
            else:
                try:
                    parsed = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    parsed = []
                implicits = parsed if isinstance(parsed, list) else []
                provenance = {"source": "items.implicit_text", "status": "normalized"}
        else:
            fallback = self._item_base_source_implicits(entity_id)
            if fallback is not None:
                implicits = fallback["implicits"]
                provenance = fallback["provenance"]
        result = {
            "entity_id": entity_id,
            "kind": kind,
            "name": self.name(entity_id),
            "display_name_key": extensions.get("displayNameKey"),
            "item_category": row.get("item_category"),
            "item_type": row.get("item_type"),
            "base_type": row.get("base_type"),
            "gear_category": row.get("gear_category"),
            "equip_slot": row.get("equip_slot"),
            "is_unique": row.get("is_unique"),
            "level_requirement": row.get("level_requirement"),
            "implicits": implicits,
            "implicits_provenance": provenance,
            "extensions": extensions,
        }
        self._cache[key] = result
        return result

    def _item_base_source_implicits(self, entity_id: str) -> dict | None:
        """Read ``implicits`` from the archived item_base source record (read-only)."""
        snapshot = self.snapshot_id("letools")
        mappings = self._rows(
            """
            SELECT source_scope, source_id, mapping_method, evidence
            FROM source_id_map
            WHERE source_snapshot_id = ? AND source = 'letools' AND entity_id = ?
              AND kind = 'item_base'
            ORDER BY source_scope, source_id
            """,
            (snapshot, entity_id),
        )
        for mapping in mappings:
            records = self._rows(
                """
                SELECT record_id, raw_payload, locator, resource_name
                FROM source_records
                WHERE source = 'letools' AND entity_kind = 'item_base'
                  AND source_scope = ? AND source_id = ?
                ORDER BY CASE parse_status WHEN 'parsed' THEN 0 ELSE 1 END, record_id
                LIMIT 1
                """,
                (mapping["source_scope"], mapping["source_id"]),
            )
            if not records:
                continue
            record = records[0]
            try:
                payload = json.loads(record["raw_payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            implicits = payload.get("implicits") if isinstance(payload, dict) else None
            if not isinstance(implicits, list):
                implicits = []
            return {
                "implicits": implicits,
                "provenance": {
                    "source": "source_records:item_base",
                    "status": "fallback_from_archived_source",
                    "record_id": record["record_id"],
                    "resource_name": record["resource_name"],
                    "source_scope": mapping["source_scope"],
                    "source_id": mapping["source_id"],
                    "locator": record["locator"],
                    "note": (
                        "normalized phase-1 tables keep only blessing/idol identity rows; "
                        "the implicit definition is read from the preserved item_base record, "
                        "not from a new dataset"
                    ),
                },
            }
        return None


    def unique_mods(self, entity_id: str) -> list[dict]:
        row = self.item(entity_id)
        if not row or not row.get("extensions"):
            return []
        try:
            ext = json.loads(row["extensions"])
        except (TypeError, json.JSONDecodeError):
            return []
        mods = ext.get("mods") or []
        return [mod for mod in mods if isinstance(mod, dict)]

    def unique_definition(self, entity_id: str) -> dict:
        row = self.item(entity_id)
        if not row or not row.get("extensions"):
            return {}
        try:
            return json.loads(row["extensions"])
        except (TypeError, json.JSONDecodeError):
            return {}

    def affix(self, entity_id: str) -> dict | None:
        row = self._row(
            """
            SELECT e.entity_id, e.canonical_name, e.record_status, a.affix_type,
                   a.source_affix_id, a.affix_title, a.group_id, a.levels, a.can_roll_on,
                   a.affix_display_name, a.affix_slot, a.extensions
            FROM entities AS e LEFT JOIN affixes AS a ON a.entity_id = e.entity_id
            WHERE e.entity_id = ?
            """,
            (entity_id,),
        )
        return row

    def affix_tiers(self, affix_entity_id: str) -> list[dict]:
        key = ("affix_tiers", affix_entity_id)
        if key not in self._cache:
            rows = self._rows(
                "SELECT tier_id, affix_entity_id, tier_index, roll_min, roll_max, extra_rolls, "
                "level_requirement, extensions FROM affix_tiers WHERE affix_entity_id = ? "
                "ORDER BY tier_index",
                (affix_entity_id,),
            )
            self._cache[key] = rows
        return self._cache[key]

    def affix_tier(self, affix_entity_id: str, tier_index: int) -> dict | None:
        for row in self.affix_tiers(affix_entity_id):
            if row["tier_index"] == tier_index:
                return row
        return None

    def affix_tier_count(self, affix_entity_id: str) -> int:
        return len(self.affix_tiers(affix_entity_id))

    # -------------------------------------------------------------------- stats
    def stat_by_property(
        self, property_id, tags=0, special_tag=0
    ) -> dict | None:
        """Resolve an item/affix mod ``property`` to a stats row.

        Follows the archived LE Tools ``getProperty(property, tags, specialTag)``
        dispatch: two marker values address the ability/player property lists; every
        other value is a ``coreDB.propertyList`` id.  Returns the primary ``stats``
        row (with a ``resolution`` note) or ``None`` when it cannot be resolved
        unambiguously.
        """
        try:
            prop = int(property_id)
        except (TypeError, ValueError):
            return None
        tags = int(tags or 0)
        special_tag = int(special_tag or 0)
        row: dict | None = None
        resolution = None
        if prop == PROPERTY_MARKER_ABILITY:
            row = self._row(
                "SELECT * FROM stats WHERE source_scope = 'coreDB.abilityPropertyList' "
                "AND raw_enum = ?",
                (f"{tags}.{special_tag}",),
            )
            resolution = f"marker {PROPERTY_MARKER_ABILITY} -> abilityPropertyList[{tags}].properties[{special_tag}]"
        elif prop == PROPERTY_MARKER_PLAYER:
            row = self._row(
                "SELECT * FROM stats WHERE source_scope = 'coreDB.playerPropertyList' "
                "AND raw_enum = ?",
                (str(tags),),
            )
            resolution = f"marker {PROPERTY_MARKER_PLAYER} -> playerPropertyList[{tags}]"
        elif prop == PROPERTY_MARKER_IDOL_ALTAR:
            row = self._row(
                "SELECT * FROM stats WHERE source_scope = 'coreDB.idolAltarPropertyList.list' "
                "AND raw_enum = ?",
                (str(tags),),
            )
            resolution = f"marker {PROPERTY_MARKER_IDOL_ALTAR} -> idolAltarPropertyList.list[{tags}]"
        else:
            row = self._row(
                "SELECT * FROM stats WHERE source_scope = 'coreDB.propertyList' "
                "AND raw_enum = ?",
                (str(prop),),
            )
            resolution = f"propertyList[{prop}]"
        if not row:
            # Special property ids live in ``window.SP`` (archived db bundle) rather than
            # the coreDB property lists; the archived ``ce``/``hb`` functions still give
            # them a synthetic default property (name + display flags).  See the 2026-11-03
            # review P2-1/P1-2 residual: property 120 = EffectOfAilmentOnYou.
            special = self._special_property(prop, tags, special_tag)
            if special is None:
                return None
            result = special
            overrides = self._display_overrides(prop, tags, special_tag)
            for override in overrides:
                result[override["flag_key"]] = override["value"]
            return result
        try:
            extensions = json.loads(row.get("extensions") or "{}")
        except json.JSONDecodeError:
            extensions = {}
        for override in self._display_overrides(prop, tags, special_tag):
            extensions[override["flag"]] = override["value"]
        result = dict(row)
        result["property_resolution"] = resolution
        result["property_rule_version"] = PROPERTY_RULE_VERSION
        result["stat_extensions"] = extensions
        result["property_name_key"] = extensions.get("propertyNameKey")
        result["alt_text_key"] = extensions.get("altTextKey")
        result["alt_text_overrides"] = extensions.get("altTextOverrides") or []
        result["special_tag_display"] = extensions.get("special_tag_display") or {}
        result["special_tag_display_source"] = extensions.get("special_tag_display_source") or []
        result["hide_modifier_value"] = int(extensions.get("hideModifierValue") or 0)
        result["dont_display_plus"] = int(extensions.get("dontDisplayPlus") or 0)
        result["display_as_added_to"] = int(extensions.get("displayAsAddedTo") or 0)
        result["name_includes_modifier"] = int(extensions.get("nameIncludesModifier") or 0)
        result["display_added_as_tenth"] = int(extensions.get("displayAddedAsTenthOfValue") or 0)
        result["rounding_for_more"] = int(extensions.get("roundingForMore") or 0)
        result["round_added_to_int"] = int(extensions.get("roundAddedToInt") or 0)
        result["display_added_as_percentage"] = int(extensions.get("displayAddedAsPercentage") or 0)
        result["display_as_percentage_of"] = int(extensions.get("displayAsPercentageOf") or 0)
        special_description = self._ailment_special_description(result, prop, special_tag)
        if not special_description:
            special_description = self._hit_event_special_description(result, prop, special_tag)
        if special_description:
            result["special_description"] = special_description
        if prop in NON_DPS_PROPERTIES:
            result["non_dps"] = True
        return result

    def _display_overrides(self, prop: int, tags: int, special_tag: int) -> list[dict]:
        """Archived ``getProperty`` display-flag overrides for marker properties."""
        applied = []
        for override in DISPLAY_FLAG_OVERRIDES:
            if int(override.get("marker_id") or -1) != prop:
                continue
            if int(override.get("index") or -1) != tags:
                continue
            expected_special = override.get("special_tag")
            if expected_special is not None and int(expected_special) != special_tag:
                continue
            flag = override.get("flag") or ""
            flag_key = _SNAKE_FLAG_KEYS.get(flag)
            if not flag_key:
                continue
            applied.append({"flag": flag, "flag_key": flag_key, "value": int(override.get("value") or 0)})
        return applied

    def _special_property(self, prop: int, tags: int, special_tag: int) -> dict | None:
        name = SP_BY_VALUE.get(prop)
        if not name:
            return None
        extensions: dict = {}
        if prop in SPECIAL_PERCENT_IDS:
            extensions["displayAddedAsPercentage"] = 1
        if prop in SPECIAL_ROUND_IDS:
            extensions["roundAddedToInt"] = 1
        template = SPECIAL_TEMPLATES.get(str(prop))
        description = None
        if template:
            text = self.translation(template.get("translation_key"))
            if text:
                ailment = AILMENT_BY_VALUE.get(special_tag, str(special_tag))
                description = text.replace("{ailment}", ailment)
        result = {
            "stat_id": f"letools:property-enum:{prop}",
            "name": _prettify_property_name(name),
            "unit": None,
            "description": description,
            "raw_enum": str(prop),
            "source_scope": "letools:property-enum",
            "extensions": json.dumps(extensions),
            "property_resolution": f"SP enum {name} ({prop})",
            "property_rule_version": PROPERTY_RULE_VERSION,
            "display_meta_version": DISPLAY_META_VERSION,
            "stat_extensions": extensions,
            "property_name_key": None,
            "alt_text_key": None,
            "alt_text_overrides": [],
            "hide_modifier_value": 0,
            "dont_display_plus": 0,
            "display_as_added_to": 0,
            "name_includes_modifier": 0,
            "display_added_as_tenth": 0,
            "rounding_for_more": 0,
            "round_added_to_int": int(extensions.get("roundAddedToInt") or 0),
            "display_added_as_percentage": int(extensions.get("displayAddedAsPercentage") or 0),
            "display_as_percentage_of": 0,
            "special_description": description,
        }
        return result

    def _hit_event_special_description(self, stat: dict, prop: int, special_tag: int) -> str | None:
        """Event-qualified description for hit-event-taggable properties.

        The archived runtime resolves e.g. ``18 Ward Gain`` with ``specialTag=3`` through
        ``Tb(3) == "kill"`` and ``planner.stats.extra.ward_gain.kill`` = "Ward Gain on Kill".
        Keeping the event suffix in the description makes the player text state the trigger
        (2026-11-04 re-review R1) without changing the structured value.
        """
        try:
            slug = HIT_EVENT_PROPERTY_SLUGS.get(int(prop))
            event = HIT_EVENT_TAG_BY_VALUE.get(int(special_tag or 0))
        except (TypeError, ValueError):
            return None
        if not slug or not event:
            return None
        return self.translation(f"planner.stats.extra.{slug}.{event}")

    def _ailment_special_description(self, stat: dict, prop: int, special_tag: int) -> str | None:
        """Differentiate ailment-scoped properties by their AilmentID specialTag.

        Multi-affixes such as ``Ignite And Spreading Flames Duration`` carry the same
        property (42 = Increased Ailment Duration) twice with different ``specialTag``
        values.  The archived site resolves the concrete ailment through its alt-text
        overrides; where those translations are missing in the catalog, the AilmentID
        enum from the archived bundle is used to keep the lines distinguishable
        (2026-11-03 review P1-2: generic property names must not collapse distinct
        effects).  Only applies to non-marker coreDB properties.
        """
        if prop in (PROPERTY_MARKER_ABILITY, PROPERTY_MARKER_PLAYER, PROPERTY_MARKER_IDOL_ALTAR):
            return None
        if not special_tag:
            return None
        # dataset-level specialTag->display mapping (phase1-dataset-003+, derived
        # from single-property affix display names; e.g. property 43 + specialTag 1
        # is "Fire Penetration with Ignite" in 1.5.2).  When available it takes
        # precedence over the generic "Ailment" replacement so the player text
        # matches the game rather than the legacy property name.
        mapped = (stat.get("special_tag_display") or {}).get(str(special_tag))
        if mapped:
            return mapped
        name = stat.get("name") or ""
        if "Ailment" not in name:
            return None
        ailment = AILMENT_BY_VALUE.get(int(special_tag))
        if not ailment:
            return None
        return name.replace("Ailment", _prettify_property_name(ailment))

    def ailment_name(self, ailment_id) -> str | None:
        try:
            return AILMENT_BY_VALUE.get(int(ailment_id))
        except (TypeError, ValueError):
            return None

    def stat_display_name(self, stat: dict | None) -> str | None:
        """Player-facing property name with the archived alt-text-override fallback."""
        if not stat:
            return None
        key = stat.get("property_name_key")
        text = self.translation(key) if key else None
        return text or stat.get("name")

    def stat_description(self, stat: dict, modifier_type, tags, special_tag) -> str | None:
        """Modifier description text for one mod (``Fa``/``hb`` in the archived runtime).

        Priority: translated alt-text override matching (modType, tags, specialTag)
        with the archived specialTag-only fallback, then the translated property name.
        """
        if not stat:
            return None
        special = stat.get("special_description")
        if special:
            return special
        overrides = stat.get("alt_text_overrides") or []
        modifier_type = int(modifier_type or 0)
        tags = int(tags or 0)
        special_tag = int(special_tag or 0)
        for override in overrides:
            if (
                int(override.get("modType") or 0) == modifier_type
                and int(override.get("tags") or 0) == tags
                and int(override.get("specialTag") or 0) == special_tag
            ):
                text = self.translation(override.get("altTextKey"))
                if text:
                    return text
        if special_tag:
            for override in overrides:
                if int(override.get("specialTag") or 0) == special_tag:
                    text = self.translation(override.get("altTextKey"))
                    if text:
                        return text
        return self.stat_display_name(stat)

    def tag_names(self, bitmask) -> list[str]:
        """Decode an LE Tools tag bitmask into tag names (from the tags table)."""
        key = ("tag_names", int(bitmask or 0))
        if key in self._cache:
            return list(self._cache[key])
        names: list[str] = []
        try:
            mask = int(bitmask or 0)
        except (TypeError, ValueError):
            return names
        if mask:
            for row in self._rows("SELECT name, raw_value FROM tags"):
                try:
                    bit = int(row["raw_value"])
                except (TypeError, ValueError):
                    continue
                if mask & bit:
                    names.append(row["name"])
        self._cache[key] = names
        return list(names)

    def tag_name_by_bit(self) -> dict[int, str]:
        key = ("tag_name_by_bit",)
        if key not in self._cache:
            mapping = {}
            for row in self._rows("SELECT name, raw_value FROM tags"):
                try:
                    mapping[int(row["raw_value"])] = row["name"]
                except (TypeError, ValueError):
                    continue
            self._cache[key] = mapping
        return self._cache[key]

    def unique_tooltip(self, entity_id: str) -> dict:
        """Ordered unique tooltip composition data (LE Tools ``tooltipEntries`` convention).

        Observed convention across the fixed sample: each ``tooltipEntries[i].modDisplay``
        is either the index of a mod (``< 128``) or a tooltip-description index
        (``modDisplay - 128``); the list order is the display order.  The threshold and
        both branches are evidenced by every unique in the sample (e.g. Apex of Thought
        entries [0,1,128,129,3], Unstable Core [128,1,2,3,4,5], Spine of Malatros
        [0,1,2,128,129,6,5], Fiery Dragon Shoes [0,1,4,3,2,5]).
        """
        extensions = self.unique_definition(entity_id)
        mods = [mod for mod in (extensions.get("mods") or []) if isinstance(mod, dict)]
        raw_entries = extensions.get("tooltipEntries") or []
        descriptions = []
        for index, description in enumerate(extensions.get("tooltipDescriptions") or []):
            description = description if isinstance(description, dict) else {}
            key = description.get("descriptionKey")
            descriptions.append(
                {
                    "index": index,
                    "key": key,
                    "raw": description,
                    "text": self.translation(key) if key else None,
                }
            )
        ordered = []
        for position, entry in enumerate(raw_entries):
            display = entry.get("modDisplay") if isinstance(entry, dict) else None
            try:
                display = int(display)
            except (TypeError, ValueError):
                continue
            if display >= MOD_DISPLAY_DESCRIPTION_BASE:
                kind = "description"
                index = display - MOD_DISPLAY_DESCRIPTION_BASE
                text = descriptions[index]["text"] if index < len(descriptions) else None
                ordered.append(
                    {"position": position, "mod_display": display, "kind": kind,
                     "description_index": index, "text": text}
                )
            else:
                kind = "mod"
                ordered.append(
                    {"position": position, "mod_display": display, "kind": kind,
                     "mod_index": display, "hidden": bool(mods[display].get("hideInTooltip")) if display < len(mods) else None}
                )
        return {
            "mods": mods,
            "tooltip_entries": ordered,
            "tooltip_descriptions": descriptions,
            "description_base": MOD_DISPLAY_DESCRIPTION_BASE,
            "raw_tooltip_entries": raw_entries,
        }

    def stat(self, stat_id: str) -> dict | None:
        return self._row("SELECT * FROM stats WHERE stat_id = ?", (stat_id,))

    # ------------------------------------------------------------------- effects
    def effects_for(self, entity_id: str) -> list[dict]:
        return self._rows(
            """
            SELECT effect_id, owner_entity_id, source_entity_id, effect_category,
                   raw_text, parse_status, effect_scope
            FROM effects
            WHERE owner_entity_id = ? OR source_entity_id = ?
            """,
            (entity_id, entity_id),
        )

    def effect_value_summary(self, effect_ids: list[str]) -> dict:
        if not effect_ids:
            return {"effects": 0, "values": 0, "values_with_stat": 0, "values_without_stat": 0}
        placeholders = ",".join("?" for _ in effect_ids)
        rows = self._rows(
            f"SELECT COUNT(*) AS n, "
            f"SUM(CASE WHEN stat_id IS NOT NULL THEN 1 ELSE 0 END) AS linked, "
            f"SUM(CASE WHEN stat_link_status = 'linked' THEN 1 ELSE 0 END) AS status_linked "
            f"FROM effect_values WHERE effect_id IN ({placeholders})",
            tuple(effect_ids),
        )
        row = rows[0] if rows else {}
        total = int(row.get("n") or 0)
        linked = int(row.get("status_linked") or row.get("linked") or 0)
        return {
            "effects": len(effect_ids),
            "values": total,
            "values_with_stat": int(row.get("linked") or 0),
            "values_stat_linked": linked,
            "values_without_stat": max(0, total - int(row.get("linked") or 0)),
        }

    def relationships_for(self, entity_id: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM entity_relationships WHERE from_entity_id = ? OR to_entity_id = ?",
            (entity_id, entity_id),
        )

    def buff(self, entity_id: str) -> dict | None:
        return self._row("SELECT * FROM buffs WHERE entity_id = ?", (entity_id,))

    # ------------------------------------------------------------------- sources
    def raw_resource(self, source: str, resource_name: str) -> dict | None:
        return self._row(
            "SELECT * FROM raw_resources WHERE source = ? AND resource_name = ?",
            (source, resource_name),
        )

    def raw_resource_path(self, row: dict, root: Path) -> Path | None:
        local = row.get("local_path")
        if not local:
            return None
        path = Path(root) / local
        return path if path.is_file() else None

    def translation(self, key: str, language: str = "en") -> str | None:
        row = self._row(
            "SELECT value FROM translations WHERE key = ? AND language = ? LIMIT 1",
            (key, language),
        )
        return row["value"] if row else None

    def data_issues_summary(self) -> dict:
        rows = self._rows(
            "SELECT issue_type, severity, COUNT(*) AS n FROM data_issues GROUP BY issue_type, severity"
        )
        return {"rows": rows, "total": sum(row["n"] for row in rows)}
