"""Read-only access layer over the phase-1 SQLite dataset.

Owner: input-ledger (phase-3 stage-1).  Consumers: ``inputs``, ``stats``,
``ledger`` and tests.

Design notes
------------
* The database is opened with ``mode=ro`` (SQLite URI) and never written.
  Every method is a query helper; no game data is copied into this module.
* JSON columns (``extensions``, ``restrictions``, ...) are parsed on demand and
  returned as Python objects; malformed JSON raises :class:`CatalogError`
  instead of being silently dropped.
* Path resolution starts from ``data/current.json`` so a re-published dataset is
  never silently mixed with an older pointer.  The pointer records both the
  logical content hash and the SQLite file hash; ``Catalog`` can verify the
  file hash (once per process) to prove it is reading the pinned release.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

# ``.../tools/phase3/src/le_calc/catalog.py`` -> repo root
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DATA_CURRENT = REPO_ROOT / "data" / "current.json"

#: process cache: (resolved path, size, mtime) -> sha256 of the SQLite file
_DB_SHA256_CACHE: dict[tuple[str, int, int], str] = {}


class CatalogError(RuntimeError):
    """Raised when the catalog cannot be read or contains unexpected data."""


def file_sha256(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return the sha256 of a file (deterministic, read-only)."""
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def cached_file_sha256(path: str | Path) -> str:
    """File sha256 with a process-level cache keyed by (path, size, mtime)."""
    resolved = Path(path).resolve()
    stat = resolved.stat()
    key = (str(resolved), stat.st_size, stat.st_mtime_ns)
    cached = _DB_SHA256_CACHE.get(key)
    if cached is None:
        cached = file_sha256(resolved)
        _DB_SHA256_CACHE[key] = cached
    return cached


def resolved_data_current(path: str | Path | None = None) -> dict[str, Any]:
    """Read ``data/current.json`` (dataset pointer) without interpreting it."""
    current_path = Path(path) if path is not None else DEFAULT_DATA_CURRENT
    return _read_json(current_path, what="data current pointer")


def resolve_database_path(
    current: dict[str, Any] | None = None,
    *,
    root: str | Path | None = None,
    current_path: str | Path | None = None,
) -> Path:
    """Resolve the pinned SQLite path recorded by ``data/current.json``."""
    base = Path(root) if root is not None else REPO_ROOT
    document = current if current is not None else resolved_data_current(
        current_path if current_path is not None else base / "data" / "current.json"
    )
    raw = document.get("database")
    if not raw:
        raise CatalogError("data current pointer has no 'database' path")
    database = Path(raw)
    if not database.is_absolute():
        database = base / database
    return database.resolve()


def _read_json(path: Path, *, what: str) -> dict[str, Any]:
    if not path.is_file():
        raise CatalogError(f"{what} not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise CatalogError(f"{what} is not valid JSON ({path}): {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogError(f"{what} must be a JSON object: {path}")
    return data


def _parse_json_column(value: Any, *, column: str, owner: str) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise CatalogError(f"invalid JSON in {owner}.{column}: {exc}") from exc


class Catalog:
    """Queries over one pinned phase-1 dataset release.

    Parameters
    ----------
    database_path:
        Path to ``data.sqlite``.  Defaults to the database referenced by
        ``data/current.json``.
    verify_file_sha256:
        When true, compare the SQLite file hash against
        ``data/current.json.file_sha256``.  The hash is cached per process.
    current_path / root:
        Pointer/root overrides used for default path resolution and the file
        hash cross-check.
    """

    def __init__(
        self,
        database_path: str | Path | None = None,
        *,
        verify_file_sha256: bool = False,
        current_path: str | Path | None = None,
        root: str | Path | None = None,
    ) -> None:
        self.root = Path(root).resolve() if root is not None else REPO_ROOT
        self._current_path = (
            Path(current_path) if current_path is not None else self.root / "data" / "current.json"
        )
        self._current = resolved_data_current(self._current_path)
        self.database_path = (
            Path(database_path).resolve()
            if database_path is not None
            else resolve_database_path(self._current, root=self.root, current_path=self._current_path)
        )
        if not self.database_path.is_file():
            raise CatalogError(f"dataset database not found: {self.database_path}")
        self.verified_file_sha256: Optional[str] = None
        if verify_file_sha256:
            expected = self._current.get("file_sha256")
            if expected:
                actual = cached_file_sha256(self.database_path)
                if actual != expected:
                    raise CatalogError(
                        "dataset database hash mismatch: "
                        f"expected {expected}, got {actual} ({self.database_path})"
                    )
                self.verified_file_sha256 = actual
        self._connection: Optional[sqlite3.Connection] = None

    # -- connection management ---------------------------------------------
    def connect(self) -> sqlite3.Connection:
        """Open (or reuse) the read-only SQLite connection."""
        if self._connection is None:
            uri = f"{self.database_path.as_uri()}?mode=ro"
            try:
                connection = sqlite3.connect(uri, uri=True)
            except sqlite3.Error as exc:  # pragma: no cover - defensive
                raise CatalogError(f"cannot open dataset read-only: {exc}") from exc
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            self._connection = connection
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "Catalog":
        self.connect()
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.close()

    # -- generic query helpers ---------------------------------------------
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            cursor = connection.execute(sql, tuple(params))
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as exc:
            raise CatalogError(f"dataset query failed: {exc}") from exc

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- dataset identity ---------------------------------------------------
    @property
    def current_pointer(self) -> dict[str, Any]:
        """The parsed ``data/current.json`` used for path resolution."""
        return dict(self._current)

    def dataset_id(self) -> Optional[str]:
        row = self.one("SELECT dataset_id FROM dataset_meta LIMIT 1")
        return row["dataset_id"] if row else None

    def dataset_meta(self) -> dict[str, Any]:
        row = self.one("SELECT * FROM dataset_meta ORDER BY dataset_id LIMIT 1")
        if row is None:
            raise CatalogError("dataset_meta table is empty")
        return row

    def dataset_fingerprint(self) -> dict[str, Any]:
        row = self.one("SELECT * FROM dataset_fingerprints ORDER BY dataset_id LIMIT 1")
        if row is None:
            raise CatalogError("dataset_fingerprints table is empty")
        row["table_counts"] = _parse_json_column(
            row.get("table_counts"), column="table_counts", owner="dataset_fingerprints"
        )
        return row

    # -- classes / masteries ------------------------------------------------
    def classes(self) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT c.entity_id, c.source_class_id, c.unlock_notes, c.extensions,
                   e.canonical_name
              FROM classes c
              JOIN entities e ON e.entity_id = c.entity_id
             ORDER BY c.source_class_id
            """
        )
        for row in rows:
            row["extensions"] = _parse_json_column(
                row.get("extensions"), column="extensions", owner=row["entity_id"]
            )
        return rows

    def class_by_name(self, name: str) -> dict[str, Any]:
        rows = [row for row in self.classes() if row.get("canonical_name") == name]
        if len(rows) != 1:
            raise CatalogError(f"expected exactly one class named {name!r}, found {len(rows)}")
        return rows[0]

    def class_by_entity_id(self, entity_id: str) -> Optional[dict[str, Any]]:
        for row in self.classes():
            if row["entity_id"] == entity_id:
                return row
        return None

    def masteries(self) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT m.entity_id, m.class_entity_id, m.source_mastery_id,
                   m.unlock_notes, m.extensions, m.branch_kind, m.is_base,
                   e.canonical_name
              FROM masteries m
              JOIN entities e ON e.entity_id = m.entity_id
             ORDER BY m.entity_id
            """
        )
        for row in rows:
            row["extensions"] = _parse_json_column(
                row.get("extensions"), column="extensions", owner=row["entity_id"]
            )
        return rows

    def mastery_by_name(self, name: str) -> dict[str, Any]:
        rows = [row for row in self.masteries() if row.get("canonical_name") == name]
        if len(rows) != 1:
            raise CatalogError(f"expected exactly one mastery named {name!r}, found {len(rows)}")
        return rows[0]

    # -- trees / nodes / effects -------------------------------------------
    def trees(self) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT t.entity_id, t.tree_kind, t.owner_entity_id, t.class_entity_id,
                   t.source_tree_id, t.node_count, t.extensions, e.canonical_name
              FROM trees t
              JOIN entities e ON e.entity_id = t.entity_id
             ORDER BY t.entity_id
            """
        )
        for row in rows:
            row["extensions"] = _parse_json_column(
                row.get("extensions"), column="extensions", owner=row["entity_id"]
            )
        return rows

    def tree_by_source_id(self, source_tree_id: str, *, tree_kind: str | None = None) -> dict[str, Any]:
        rows = [
            row
            for row in self.trees()
            if row.get("source_tree_id") == source_tree_id
            and (tree_kind is None or row.get("tree_kind") == tree_kind)
        ]
        if len(rows) != 1:
            raise CatalogError(
                f"expected exactly one tree {source_tree_id!r} (kind={tree_kind!r}), "
                f"found {len(rows)}"
            )
        return rows[0]

    def tree_by_entity_id(self, entity_id: str) -> dict[str, Any]:
        rows = [row for row in self.trees() if row["entity_id"] == entity_id]
        if len(rows) != 1:
            raise CatalogError(f"expected exactly one tree {entity_id!r}, found {len(rows)}")
        return rows[0]

    def nodes_for_tree(self, tree_entity_id: str) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT node_index, entity_id, tree_entity_id, node_type, max_points,
                   description, extensions
              FROM nodes
             WHERE tree_entity_id = ?
             ORDER BY node_index
            """,
            (tree_entity_id,),
        )
        for row in rows:
            row["extensions"] = _parse_json_column(
                row.get("extensions"), column="extensions", owner=row["entity_id"]
            )
        return rows

    def node_by_index(self, tree_entity_id: str, node_index: int | str) -> dict[str, Any]:
        row = self.one(
            """
            SELECT node_index, entity_id, tree_entity_id, node_type, max_points,
                   description, extensions
              FROM nodes
             WHERE tree_entity_id = ? AND node_index = ?
            """,
            (tree_entity_id, int(node_index)),
        )
        if row is None:
            raise CatalogError(f"node {node_index!r} not found in tree {tree_entity_id}")
        row["extensions"] = _parse_json_column(
            row.get("extensions"), column="extensions", owner=row["entity_id"]
        )
        return row

    def effects_for_owner(self, owner_entity_id: str) -> list[dict[str, Any]]:
        """Effects of one owner plus their ``effect_values`` rows."""
        rows = self.query(
            """
            SELECT e.effect_id, e.owner_entity_id, e.owner_component_id,
                   e.source_entity_id, e.effect_category, e.raw_text,
                   e.parse_status, e.effect_scope, e.extensions
              FROM effects e
             WHERE e.owner_entity_id = ?
             ORDER BY e.effect_id
            """,
            (owner_entity_id,),
        )
        values = self.query(
            """
            SELECT v.value_id, v.effect_id, v.stat_id, v.value_kind,
                   v.value_numeric, v.value_min, v.value_max, v.per_level,
                   v.level, v.tier, v.unit, v.expression, v.raw,
                   v.stat_link_status, v.stat_link_evidence
              FROM effect_values v
              JOIN effects e ON e.effect_id = v.effect_id
             WHERE e.owner_entity_id = ?
             ORDER BY v.effect_id, v.value_id
            """,
            (owner_entity_id,),
        )
        by_effect: dict[str, list[dict[str, Any]]] = {}
        for value in values:
            by_effect.setdefault(value["effect_id"], []).append(value)
        for row in rows:
            row["extensions"] = _parse_json_column(
                row.get("extensions"), column="extensions", owner=row["effect_id"]
            )
            row["values"] = by_effect.get(row["effect_id"], [])
        return rows

    def effects_for_owners(self, owner_entity_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        return {owner: self.effects_for_owner(owner) for owner in owner_entity_ids}

    # -- raw source records (planner runtime tables) -----------------------
    def source_record(self, entity_kind: str, source_scope: str, source_id: str) -> Optional[dict[str, Any]]:
        row = self.one(
            """
            SELECT record_id, source_snapshot_id, source, resource_name,
                   entity_kind, source_scope, source_id, locator, raw_payload,
                   parse_status, notes
              FROM source_records
             WHERE entity_kind = ? AND source_scope = ? AND source_id = ?
            """,
            (entity_kind, source_scope, source_id),
        )
        if row is None:
            return None
        payload = row.get("raw_payload")
        if isinstance(payload, str):
            try:
                row["raw_payload"] = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise CatalogError(f"invalid raw_payload for {entity_kind}/{source_scope}/{source_id}: {exc}") from exc
        return row

    def passive_bonus_stats(self, tree_id: str = "mg-1") -> dict[str, list[dict[str, Any]]]:
        """Per-point passive-node stat entries from LE Tools ``passiveBonuses``.

        The payload's ``stats`` maps node id -> list of stat objects.  Values are
        the per-allocated-point values (``noScaling`` entries apply once above
        ``noScalingPointThreshold``); callers must scale by the allocated points.
        """
        record = self.source_record("passive_bonus", "planner.passiveBonuses", tree_id)
        if record is None:
            raise CatalogError(f"passive_bonus record not found for tree {tree_id!r}")
        payload = record.get("raw_payload") or {}
        stats = payload.get("stats")
        if not isinstance(stats, dict):
            raise CatalogError(f"passive_bonus payload for {tree_id!r} has no stats table")
        return {str(key): list(value or []) for key, value in stats.items()}

    def passive_bonus_payload(self, tree_id: str = "mg-1") -> dict[str, Any]:
        record = self.source_record("passive_bonus", "planner.passiveBonuses", tree_id)
        if record is None:
            raise CatalogError(f"passive_bonus record not found for tree {tree_id!r}")
        payload = record.get("raw_payload")
        if not isinstance(payload, dict):
            raise CatalogError(f"passive_bonus payload for {tree_id!r} is not an object")
        return payload

    def skill_bonus_payload(self, ability_id: str) -> dict[str, Any]:
        record = self.source_record("skill_bonus", "planner.partialSkillBonuses", ability_id)
        if record is None:
            raise CatalogError(f"skill_bonus record not found for ability {ability_id!r}")
        payload = record.get("raw_payload")
        if not isinstance(payload, dict):
            raise CatalogError(f"skill_bonus payload for {ability_id!r} is not an object")
        return payload

    # -- skills -------------------------------------------------------------
    def skills(self) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT s.entity_id, s.skill_type, s.class_entity_id, s.mastery_entity_id,
                   s.source_skill_id, s.cast_time_ms, s.mana_cost, s.cooldown_ms,
                   s.channel_time_ms, s.tags_raw, s.extensions, e.canonical_name
              FROM skills s
              JOIN entities e ON e.entity_id = s.entity_id
             ORDER BY s.entity_id
            """
        )
        for row in rows:
            row["extensions"] = _parse_json_column(
                row.get("extensions"), column="extensions", owner=row["entity_id"]
            )
        return rows

    def skill_by_name(self, name: str) -> dict[str, Any]:
        rows = [row for row in self.skills() if row.get("canonical_name") == name]
        if len(rows) != 1:
            raise CatalogError(f"expected exactly one skill named {name!r}, found {len(rows)}")
        return rows[0]

    def skill_components(self, skill_entity_id: str) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT component_id, skill_entity_id, component_type, component_index,
                   locator, damage_type, base_damage, added_damage_effectiveness,
                   config, extensions
              FROM skill_components
             WHERE skill_entity_id = ?
             ORDER BY component_index
            """,
            (skill_entity_id,),
        )
        for row in rows:
            row["config"] = _parse_json_column(
                row.get("config"), column="config", owner=row["component_id"]
            )
        return rows

    # -- minions（actor 声明：基础属性/继承/能力列表） ----------------------
    def minions(self) -> list[dict[str, Any]]:
        """全部 minion 实体（``base_stats``/``extensions`` 已解码为对象）。"""
        rows = self.query(
            """
            SELECT m.entity_id, m.minion_type, m.base_stats, m.inherits, m.extensions,
                   e.canonical_name
              FROM minions m
              JOIN entities e ON e.entity_id = m.entity_id
             ORDER BY m.entity_id
            """
        )
        for row in rows:
            row["base_stats"] = _parse_json_column(
                row.get("base_stats"), column="base_stats", owner=row["entity_id"]
            )
            row["inherits"] = _parse_json_column(
                row.get("inherits"), column="inherits", owner=row["entity_id"]
            )
            row["extensions"] = _parse_json_column(
                row.get("extensions"), column="extensions", owner=row["entity_id"]
            )
        return rows

    def minion(self, entity_id: str) -> Optional[dict[str, Any]]:
        """单个 minion 实体；不存在返回 ``None``（不猜、不建占位）。"""
        row = self.one(
            """
            SELECT m.entity_id, m.minion_type, m.base_stats, m.inherits, m.extensions,
                   e.canonical_name
              FROM minions m
              JOIN entities e ON e.entity_id = m.entity_id
             WHERE m.entity_id = ?
            """,
            (entity_id,),
        )
        if row is None:
            return None
        for column in ("base_stats", "inherits", "extensions"):
            row[column] = _parse_json_column(row.get(column), column=column, owner=entity_id)
        return row

    def minion_abilities(self, entity_id: str) -> list[str]:
        """minion 自带 abilityList（dataset ``extensions.remaining_keys``）；缺失返回空数组。"""
        row = self.minion(entity_id)
        if row is None:
            return []
        extensions = row.get("extensions") or {}
        remaining = (extensions.get("remaining_keys") or {}) if isinstance(extensions, dict) else {}
        abilities = (remaining.get("abilityList") or {}).get("abilities") or []
        return [str(item) for item in abilities]

    # -- entities / names / buffs / ailments / translations ----------------
    def entity(self, entity_id: str) -> Optional[dict[str, Any]]:
        row = self.one(
            """
            SELECT entity_id, kind, canonical_name, record_status, primary_source,
                   extensions
              FROM entities
             WHERE entity_id = ?
            """,
            (entity_id,),
        )
        if row is not None:
            row["extensions"] = _parse_json_column(
                row.get("extensions"), column="extensions", owner=entity_id
            )
        return row

    def entity_names(self, entity_id: str, *, language: str | None = "en") -> list[dict[str, Any]]:
        if language is None:
            return self.query(
                "SELECT entity_id, language, name, is_alias, source, name_key "
                "FROM entity_names WHERE entity_id = ? ORDER BY language, name",
                (entity_id,),
            )
        return self.query(
            "SELECT entity_id, language, name, is_alias, source, name_key "
            "FROM entity_names WHERE entity_id = ? AND language = ? ORDER BY name",
            (entity_id, language),
        )

    def display_name(self, entity_id: str) -> Optional[str]:
        entity = self.entity(entity_id)
        if entity and entity.get("canonical_name"):
            return entity["canonical_name"]
        names = self.entity_names(entity_id)
        return names[0]["name"] if names else None

    def buffs(self) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT b.entity_id, b.buff_category, b.duration_ms, b.max_stacks,
                   b.target_scope, b.lifecycle, b.extensions, b.ui_max_instances,
                   b.max_stacks_basis, b.base_duration_ms, b.duration_basis,
                   b.ui_flags, e.canonical_name
              FROM buffs b
              JOIN entities e ON e.entity_id = b.entity_id
             ORDER BY b.entity_id
            """
        )
        for row in rows:
            row["extensions"] = _parse_json_column(
                row.get("extensions"), column="extensions", owner=row["entity_id"]
            )
        return rows

    def buffs_named(self, name: str) -> list[dict[str, Any]]:
        return [row for row in self.buffs() if row.get("canonical_name") == name]

    def buff_by_entity_id(self, entity_id: str) -> Optional[dict[str, Any]]:
        for row in self.buffs():
            if row["entity_id"] == entity_id:
                return row
        return None

    def ailments(self) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT a.entity_id, a.damage_type, a.base_damage, a.duration_ms,
                   a.stack_rule, a.scaling_tags, a.extensions, e.canonical_name
              FROM ailments a
              JOIN entities e ON e.entity_id = a.entity_id
             ORDER BY a.entity_id
            """
        )
        for row in rows:
            row["extensions"] = _parse_json_column(
                row.get("extensions"), column="extensions", owner=row["entity_id"]
            )
            row["stack_rule"] = _parse_json_column(
                row.get("stack_rule"), column="stack_rule", owner=row["entity_id"]
            )
        return rows

    def ailment_by_name(self, name: str) -> dict[str, Any]:
        rows = [row for row in self.ailments() if row.get("canonical_name") == name]
        if len(rows) != 1:
            raise CatalogError(f"expected exactly one ailment named {name!r}, found {len(rows)}")
        return rows[0]

    def translation_keys(self, key: str, *, language: str = "en") -> list[dict[str, Any]]:
        return self.query(
            "SELECT translation_id, source, source_snapshot_id, namespace, language, key, value "
            "FROM translations WHERE key = ? AND language = ? ORDER BY namespace",
            (key, language),
        )

    def translation_by_key(self, key: str, *, language: str = "en") -> Optional[str]:
        rows = self.translation_keys(key, language=language)
        return rows[0]["value"] if rows else None

    def translations_containing(
        self, value: str, *, language: str = "en", limit: int = 50
    ) -> list[dict[str, Any]]:
        return self.query(
            "SELECT translation_id, source, source_snapshot_id, namespace, language, key, value "
            "FROM translations WHERE language = ? AND value LIKE ? ORDER BY key LIMIT ?",
            (language, f"%{value}%", int(limit)),
        )

    # -- idol layout --------------------------------------------------------
    def idol_sizes(self) -> dict[str, str]:
        """Map ``item:...`` base entity id -> idol size label (Grand, Minor...)."""
        rows = self.query(
            """
            SELECT i.entity_id, i.idol_size, i.restrictions
              FROM idols i
             ORDER BY i.entity_id
            """
        )
        return {row["entity_id"]: row["idol_size"] for row in rows if row.get("idol_size")}

    def idol_size_for_item(self, entity_id: str) -> Optional[str]:
        sizes = self.idol_sizes()
        if entity_id in sizes:
            return sizes[entity_id]
        entity = self.entity(entity_id)
        if entity is None:
            return None
        extensions = entity.get("extensions") or {}
        if isinstance(extensions, dict):
            item_type_name = extensions.get("item_type_name")
            if isinstance(item_type_name, str) and item_type_name.endswith(" Idol"):
                return item_type_name[: -len(" Idol")]
        return None


def open_catalog(
    database_path: str | Path | None = None,
    *,
    verify_file_sha256: bool = False,
    root: str | Path | None = None,
) -> Catalog:
    """Convenience factory (kept small so tests can monkeypatch paths)."""
    return Catalog(database_path, verify_file_sha256=verify_file_sha256, root=root)


__all__ = [
    "Catalog",
    "CatalogError",
    "DEFAULT_DATA_CURRENT",
    "REPO_ROOT",
    "cached_file_sha256",
    "file_sha256",
    "open_catalog",
    "resolve_database_path",
    "resolved_data_current",
]
