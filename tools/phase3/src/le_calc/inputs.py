"""Input adapter: frozen calculator export + pinned build revision + dataset.

Owner: input-ledger (phase-3 stage-1).

``load_bundle()`` is the entry point used by ``le_calc.cli`` and
``le_calc.stats``.  It reads four frozen inputs and returns them with their
sha256 fingerprints:

* the ``calculation-input-v1`` JSON export (explicit ``calc_input_path``);
* ``<build_dir>/revisions/<rev>/build.json`` (revision selected by the export);
* ``<build_dir>/current.json`` (moving pointer: cross-check only);
* the dataset pointer + catalog SQLite (default ``data/current.json``).

**No case-specific defaults**: the public release ships neither a build revision
nor a calculation-input export, so both paths must be supplied by the caller.

Revision selection is pinned to ``calculation-input.revision_id`` rather than
the moving ``current.json`` pointer: a concurrent workflow may upgrade the
delivered build while a calculator run must stay reproducible.  The adapter
requires ``revisions/<pinned>/build.json`` to agree with the export
(revision_id / content_sha256 / revision_seq).  If ``current.json`` points at a
different revision, that drift is recorded as a non-fatal ``info`` check, never
as a fatal mismatch.

The adapter never writes anything and never touches the network.  Fatal
mismatches raise :class:`InputError`; non-fatal observations are collected in
``InputBundle.checks`` so the snapshot/report can show them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .catalog import REPO_ROOT, cached_file_sha256, file_sha256, resolved_data_current

#: The public release ships no case build revision or frozen calculation-input
#: export.  ``load_bundle()`` therefore requires explicit ``build_dir`` and
#: ``calc_input_path``; the legacy no-argument shortcut is gone.
DEFAULT_BUILD_DIR: Optional[Path] = None
DEFAULT_CALC_INPUT: Optional[Path] = None
DEFAULT_DATASET_CURRENT = Path("data") / "current.json"

CALC_SCHEMA_VERSION = "calculation-input-v1"
EXPECTED_ITEM_COUNT = 29
EXPECTED_MOD_COUNT = 148
EXPECTED_PASSIVE_NODES = 19
EXPECTED_PASSIVE_POINTS = 113
EXPECTED_SPECIALIZATIONS = 5


class InputError(ValueError):
    """Fatal input-contract violation (bad/missing/mismatched frozen input)."""


def _read_json_object(path: Path, *, what: str) -> dict[str, Any]:
    if not path.is_file():
        raise InputError(f"{what} not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InputError(f"{what} is not valid JSON ({path}): {exc}") from exc
    if not isinstance(data, dict):
        raise InputError(f"{what} must be a JSON object: {path}")
    return data


def _sha256_of(path: Path) -> str:
    if not path.is_file():
        raise InputError(f"file not found for fingerprint: {path}")
    return file_sha256(path)


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


@dataclass
class InputBundle:
    """All frozen inputs of one calculator run, with fingerprints and checks."""

    root: Path
    build_dir: Path
    revision_dir: Path
    dataset_current_path: Path
    dataset_manifest_path: Path
    database_path: Path
    calc_input_path: Path
    current: dict[str, Any]
    revision: dict[str, Any]
    build: dict[str, Any]
    calculation_input: dict[str, Any]
    dataset_current: dict[str, Any]
    dataset_manifest: dict[str, Any]
    dataset_meta: dict[str, Any]
    dataset_fingerprint: dict[str, Any]
    fingerprints: dict[str, Any] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)

    # -- convenience accessors ---------------------------------------------
    @property
    def mods(self) -> list[dict[str, Any]]:
        return list(self.calculation_input.get("mods") or [])

    @property
    def items(self) -> list[dict[str, Any]]:
        return list(self.calculation_input.get("items") or [])

    @property
    def character(self) -> dict[str, Any]:
        return dict(self.calculation_input.get("character") or {})

    @property
    def passive_tree(self) -> dict[str, Any]:
        return dict(self.calculation_input.get("passive_tree") or {})

    @property
    def passive_nodes(self) -> list[dict[str, Any]]:
        return list(self.passive_tree.get("nodes") or [])

    @property
    def specializations(self) -> list[dict[str, Any]]:
        return list((self.calculation_input.get("skills") or {}).get("specializations") or [])

    @property
    def action_bar(self) -> list[dict[str, Any]]:
        return list((self.calculation_input.get("skills") or {}).get("action_bar") or [])

    def specialization_for_tree(self, tree_id: str) -> Optional[dict[str, Any]]:
        for spec in self.specializations:
            tree = (spec.get("tree") or {}).get("entity_id") or ""
            if tree.endswith(f":{tree_id}") or tree == tree_id:
                return spec
        return None

    def item_by_instance(self, instance_id: str) -> Optional[dict[str, Any]]:
        for item in self.items:
            if item.get("instance_id") == instance_id:
                return item
        return None

    def mod_by_id(self, mod_id: str) -> Optional[dict[str, Any]]:
        for mod in self.mods:
            if mod.get("mod_id") == mod_id:
                return mod
        return None

    # -- report fingerprints ------------------------------------------------
    def build_meta(self) -> dict[str, Any]:
        """Shape consumed by ``le_calc.report`` (see Lead integration contract).

        The pinned revision always comes from ``calculation-input.revision_id``;
        the moving ``current.json`` pointer is reported separately (drift is
        possible when another workflow upgrades the delivered build).
        """
        calc = self.calculation_input
        fp = self.fingerprints
        revision_meta = (self.revision.get("meta") or {}).get("revision") or {}
        return {
            "build": {
                "build_id": calc.get("build_id"),
                "revision_id": calc.get("revision_id"),
                "content_sha256": calc.get("content_sha256"),
                "revision_seq": calc.get("revision_seq"),
                "previous_revision_id": revision_meta.get("previous_revision_id"),
            },
            "dataset": {
                "dataset_id": calc.get("dataset_id"),
                "target_game_version": self.dataset_meta.get("target_game_version"),
                "logical_content_sha256": self.dataset_fingerprint.get("logical_sha256"),
                "file_sha256": fp.get("database_sha256"),
            },
            "calc_input": {
                "path": _relative(self.root, self.calc_input_path),
                "sha256": fp.get("calc_input_sha256"),
                "mod_count": len(self.mods),
                "unresolved_inputs": len(calc.get("unresolved_inputs") or []),
                "schema_version": calc.get("schema_version"),
                "generated_at": calc.get("generated_at"),
            },
            "revision_selection": {
                "pinned_from": "calculation-input.revision_id",
                "revision_id": calc.get("revision_id"),
                "current_pointer_revision_id": self.current.get("revision_id"),
                "current_pointer_drift": bool(fp.get("current_pointer_drift")),
            },
            # top-level flat fingerprints (report.fingerprints reads these)
            "build_spec_sha256": fp.get("build_json_sha256"),
            "content_sha256": calc.get("content_sha256"),
            "calc_input_sha256": fp.get("calc_input_sha256"),
            "database_sha256": fp.get("database_sha256"),
            "dataset_logical_content_sha256": fp.get("dataset_logical_content_sha256"),
            "current_pointer_sha256": fp.get("current_json_sha256"),
            "source_raw_sha256": fp.get("source_raw_sha256"),
            "game_version": calc.get("game_version"),
            "revision_published_at": revision_meta.get("published_at"),
        }

    def to_dict(self) -> dict[str, Any]:
        """Compact, JSON-safe description of the loaded bundle (no raw mods)."""
        return {
            "paths": {
                "root": str(self.root),
                "build_dir": _relative(self.root, self.build_dir),
                "revision_dir": _relative(self.root, self.revision_dir),
                "calculation_input": _relative(self.root, self.calc_input_path),
                "database": _relative(self.root, self.database_path),
                "dataset_manifest": _relative(self.root, self.dataset_manifest_path),
            },
            "fingerprints": dict(self.fingerprints),
            "checks": list(self.checks),
            "counts": {
                "items": len(self.items),
                "mods": len(self.mods),
                "passive_nodes": len(self.passive_nodes),
                "passive_points": self.passive_tree.get("points_total"),
                "specializations": len(self.specializations),
            },
        }


def _check(
    checks: list[dict[str, Any]],
    name: str,
    ok: bool,
    detail: str = "",
    *,
    severity: str = "fatal",
) -> bool:
    checks.append(
        {
            "check": name,
            "status": "pass" if ok else ("info" if severity == "info" else "fail"),
            "detail": detail,
        }
    )
    return ok


def load_bundle(
    root: str | Path | None = None,
    *,
    build_dir: str | Path | None = None,
    calc_input_path: str | Path | None = None,
    dataset_current_path: str | Path | None = None,
    verify_database_file: bool = True,
) -> InputBundle:
    """Load and fingerprint the frozen calculator inputs.

    ``build_dir`` and ``calc_input_path`` are required: the public release does
    not ship any case build revision or frozen calculation-input export.  Relative
    paths resolve against ``root`` (default: the project root).
    """
    base = Path(root).resolve() if root is not None else REPO_ROOT
    if build_dir is None:
        raise InputError(
            "load_bundle: build_dir 未提供；公开版不内置案例构筑 revision，"
            "请显式传入 build_dir（例如 data/builds/<tool>/<share>）"
        )
    if calc_input_path is None:
        raise InputError(
            "load_bundle: calc_input_path 未提供；公开版不内置案例计算输入，"
            "请显式传入 calculation-input JSON 路径"
        )
    build_dir_path = (base / build_dir).resolve()
    calc_path = (base / calc_input_path).resolve()
    dataset_current_file = (
        (base / dataset_current_path).resolve()
        if dataset_current_path is not None
        else (base / DEFAULT_DATASET_CURRENT).resolve()
    )

    checks: list[dict[str, Any]] = []

    # -- calculator export (source of truth for the pinned revision) --------
    # The export is the frozen artifact consumed by the calculator; its
    # ``revision_id`` selects ``revisions/<id>/build.json``.  The moving
    # ``current.json`` pointer is cross-checked but never overrides the pin, so
    # a concurrent workflow upgrading the delivered build cannot silently
    # change a reproducible calculation run.
    calc = _read_json_object(calc_path, what="calculation-input export")
    revision_id = calc.get("revision_id")
    if not revision_id:
        raise InputError(f"calculation-input export has no revision_id: {calc_path}")
    revision_id = str(revision_id)
    revision_dir = build_dir_path / "revisions" / revision_id
    build_json_path = revision_dir / "build.json"
    revision = _read_json_object(build_json_path, what="published revision build.json")

    # -- build current pointer (cross-check only) --------------------------
    current_path = build_dir_path / "current.json"
    current = _read_json_object(current_path, what="build current pointer")

    # -- dataset pointer + manifest ----------------------------------------
    dataset_current = _read_json_object(dataset_current_file, what="dataset current pointer")
    manifest_path = base / str(dataset_current.get("manifest") or "")
    dataset_manifest = _read_json_object(manifest_path, what="dataset manifest")
    database_path = base / str(dataset_current.get("database") or "")
    if not database_path.is_file():
        raise InputError(f"dataset database not found: {database_path}")

    dataset_meta: dict[str, Any] = {}
    dataset_fingerprint: dict[str, Any] = {}
    try:  # catalog access is optional for pure fingerprint checks
        from .catalog import Catalog

        with Catalog(database_path, root=base, current_path=dataset_current_file) as catalog:
            dataset_meta = catalog.dataset_meta()
            dataset_fingerprint = catalog.dataset_fingerprint()
    except Exception:  # pragma: no cover - fingerprint checks stay valid without catalog
        dataset_meta = {}

    # -- fingerprints -------------------------------------------------------
    calc_sha = _sha256_of(calc_path)
    build_sha = _sha256_of(build_json_path)
    current_sha = _sha256_of(current_path)
    if verify_database_file:
        database_sha = cached_file_sha256(database_path)
    else:
        database_sha = (dataset_manifest.get("database") or {}).get("sha256")
    revision_meta = (revision.get("meta") or {}).get("revision") or {}
    source_payload = (revision.get("meta") or {}).get("source_payload") or {}
    pinned_revision_id = str(calc.get("revision_id"))
    current_revision_id = current.get("revision_id")
    current_drift = current_revision_id != pinned_revision_id
    fingerprints: dict[str, Any] = {
        "calc_input_sha256": calc_sha,
        "build_json_sha256": build_sha,
        "current_json_sha256": current_sha,
        "database_sha256": database_sha,
        "revision_id": pinned_revision_id,
        "revision_seq": calc.get("revision_seq"),
        "content_sha256": calc.get("content_sha256"),
        "source_raw_sha256": source_payload.get("raw_sha256"),
        "dataset_id": calc.get("dataset_id") or current.get("dataset_id"),
        "dataset_logical_content_sha256": dataset_current.get("logical_content_sha256"),
        "dataset_file_sha256": dataset_current.get("file_sha256"),
        "game_version": calc.get("game_version"),
        "calculation_input_schema": calc.get("schema_version"),
        "calculation_input_generated_at": calc.get("generated_at"),
        "revision_published_at": revision_meta.get("published_at"),
        "revision_id_source": "calculation-input",
        "current_pointer_revision_id": current_revision_id,
        "current_pointer_drift": current_drift,
        "excluded_non_dps_count": calc.get("excluded_non_dps"),
        "ruleset_note": "ruleset loaded separately by le_calc.ruleset",
    }

    # -- contract checks ----------------------------------------------------
    fatal = False

    _check(
        checks,
        "revision_pin_source",
        True,
        f"pinned revision {pinned_revision_id!r} from calculation-input.revision_id "
        f"(build current.json={current_revision_id!r})",
        severity="info",
    )
    fatal |= not _check(
        checks,
        "calc_input_schema",
        calc.get("schema_version") == CALC_SCHEMA_VERSION,
        f"expected {CALC_SCHEMA_VERSION!r}, got {calc.get('schema_version')!r}",
    )
    fatal |= not _check(
        checks,
        "calc_input_build_id",
        calc.get("build_id") == (revision.get("meta") or {}).get("build_id"),
        f"calc={calc.get('build_id')!r} build_meta={(revision.get('meta') or {}).get('build_id')!r}",
    )
    fatal |= not _check(
        checks,
        "build_meta_revision_id",
        revision_meta.get("revision_id") == pinned_revision_id,
        f"build={revision_meta.get('revision_id')!r} pinned={pinned_revision_id!r}",
    )
    fatal |= not _check(
        checks,
        "build_meta_content_sha256",
        revision_meta.get("content_sha256") == calc.get("content_sha256"),
        f"build={revision_meta.get('content_sha256')!r} calc={calc.get('content_sha256')!r}",
    )
    fatal |= not _check(
        checks,
        "build_meta_revision_seq",
        revision_meta.get("revision_seq") == calc.get("revision_seq"),
        f"build={revision_meta.get('revision_seq')!r} calc={calc.get('revision_seq')!r}",
    )
    fatal |= not _check(
        checks,
        "revision_dir_matches_content_hash",
        revision_dir.name == f"rev-{str(calc.get('content_sha256'))[:16]}",
        f"dir={revision_dir.name!r} expected=rev-{str(calc.get('content_sha256'))[:16]}",
    )
    fatal |= not _check(
        checks,
        "calc_input_dataset_id",
        calc.get("dataset_id") == dataset_current.get("dataset_id"),
        f"calc={calc.get('dataset_id')!r} dataset={dataset_current.get('dataset_id')!r}",
    )
    fatal |= not _check(
        checks,
        "calc_input_game_version",
        calc.get("game_version")
        == (dataset_meta.get("target_game_version") or calc.get("game_version")),
        f"calc={calc.get('game_version')!r} dataset={dataset_meta.get('target_game_version')!r}",
    )

    # moving build pointer: a concurrent workflow may upgrade the delivered
    # build while this pinned calculator export stays reproducible.  Drift is
    # recorded (info) instead of failing the run; the authoritative revision is
    # always calculation-input.revision_id.
    _check(
        checks,
        "build_current_pointer_matches_calc",
        not current_drift,
        f"current.json revision={current_revision_id!r} differs from pinned calc revision "
        f"{pinned_revision_id!r}; the calc export is authoritative (pin prevents drift)",
        severity="info",
    )
    _check(
        checks,
        "build_current_pointer_build_id",
        current.get("build_id") == calc.get("build_id"),
        f"current={current.get('build_id')!r} calc={calc.get('build_id')!r}",
        severity="info",
    )
    _check(
        checks,
        "build_current_pointer_dataset_sha256",
        current.get("dataset_logical_sha256") == dataset_current.get("logical_content_sha256"),
        "build current.dataset_logical_sha256 vs data/current.logical_content_sha256",
        severity="info",
    )

    # dataset hash chain: data/current.json -> manifest.database + dataset_fingerprints
    manifest_db = dataset_manifest.get("database") or {}
    fatal |= not _check(
        checks,
        "dataset_current_id",
        dataset_current.get("dataset_id") == dataset_manifest.get("dataset_id"),
        f"current={dataset_current.get('dataset_id')!r} manifest={dataset_manifest.get('dataset_id')!r}",
    )
    fatal |= not _check(
        checks,
        "dataset_logical_sha256_chain",
        dataset_current.get("logical_content_sha256")
        == manifest_db.get("logical_content_sha256")
        == (dataset_fingerprint.get("logical_sha256") or manifest_db.get("logical_content_sha256")),
        "data/current.json vs manifest.database vs dataset_fingerprints",
    )
    fatal |= not _check(
        checks,
        "dataset_file_sha256_chain",
        dataset_current.get("file_sha256") == manifest_db.get("sha256"),
        f"current={dataset_current.get('file_sha256')!r} manifest={manifest_db.get('sha256')!r}",
    )
    fatal |= not _check(
        checks,
        "dataset_database_file_sha256",
        database_sha == dataset_current.get("file_sha256"),
        f"actual={database_sha!r} expected={dataset_current.get('file_sha256')!r}"
        if verify_database_file
        else "database file hash not verified (verify_database_file=False)",
    )
    fatal |= not _check(
        checks,
        "dataset_database_size",
        manifest_db.get("size_bytes") in (None, database_path.stat().st_size),
        f"actual={database_path.stat().st_size} manifest={manifest_db.get('size_bytes')!r}",
    )

    # calculator export completeness
    mods = list(calc.get("mods") or [])
    items = list(calc.get("items") or [])
    item_mod_count = sum(len(item.get("mods") or []) for item in items)
    fatal |= not _check(
        checks,
        "item_count",
        len(items) == EXPECTED_ITEM_COUNT,
        f"expected {EXPECTED_ITEM_COUNT}, got {len(items)}",
    )
    fatal |= not _check(
        checks,
        "mod_count",
        len(mods) == EXPECTED_MOD_COUNT,
        f"expected {EXPECTED_MOD_COUNT}, got {len(mods)}",
    )
    fatal |= not _check(
        checks,
        "item_mods_concat_equals_mods",
        item_mod_count == len(mods),
        f"items[].mods={item_mod_count} mods={len(mods)}",
    )
    fatal |= not _check(
        checks,
        "mod_ids_unique",
        len({m.get("mod_id") for m in mods}) == len(mods),
        "duplicate mod_id in calculation-input export",
    )
    fatal |= not _check(
        checks,
        "unresolved_inputs_empty",
        not (calc.get("unresolved_inputs") or []),
        f"{len(calc.get('unresolved_inputs') or [])} unresolved input(s) recorded",
    )
    # new in the 2026-11-04 re-review export: out-of-DPS properties are listed
    # separately instead of being emitted as mod rows (they must not be counted
    # twice in the ledger).
    excluded_non_dps_ids = list(calc.get("excluded_non_dps_mod_ids") or [])
    excluded_non_dps_count = calc.get("excluded_non_dps")
    if excluded_non_dps_count is not None or excluded_non_dps_ids:
        fatal |= not _check(
            checks,
            "excluded_non_dps_count",
            int(excluded_non_dps_count or 0) == len(excluded_non_dps_ids),
            f"excluded_non_dps={excluded_non_dps_count!r} ids={len(excluded_non_dps_ids)}",
        )
        emitted_mod_ids = {m.get("mod_id") for m in mods}
        overlap = sorted(set(excluded_non_dps_ids) & emitted_mod_ids)
        fatal |= not _check(
            checks,
            "excluded_non_dps_not_emitted",
            not overlap,
            f"excluded out-of-DPS mod ids also present in mods: {overlap}",
        )
        _check(
            checks,
            "excluded_non_dps_properties",
            True,
            f"out-of-DPS source properties={calc.get('excluded_non_dps_properties')!r}; "
            f"mod ids excluded before export={excluded_non_dps_ids}",
            severity="info",
        )

    passive_tree = calc.get("passive_tree") or {}
    passive_nodes = list(passive_tree.get("nodes") or [])
    fatal |= not _check(
        checks,
        "passive_node_count",
        len(passive_nodes) == EXPECTED_PASSIVE_NODES,
        f"expected {EXPECTED_PASSIVE_NODES}, got {len(passive_nodes)}",
    )
    fatal |= not _check(
        checks,
        "passive_points_total",
        passive_tree.get("points_total") == EXPECTED_PASSIVE_POINTS,
        f"expected {EXPECTED_PASSIVE_POINTS}, got {passive_tree.get('points_total')!r}",
    )
    specializations = (calc.get("skills") or {}).get("specializations") or []
    fatal |= not _check(
        checks,
        "specialization_count",
        len(specializations) == EXPECTED_SPECIALIZATIONS,
        f"expected {EXPECTED_SPECIALIZATIONS}, got {len(specializations)}",
    )
    bh_spec = None
    for spec in specializations:
        tree_entity = ((spec.get("tree") or {}).get("entity_id") or "")
        if tree_entity.endswith(":bh2"):
            bh_spec = spec
    fatal |= not _check(
        checks,
        "bh2_specialization_present",
        bh_spec is not None,
        "no Black Hole specialization in calculator export",
    )
    if bh_spec is not None:
        fatal |= not _check(
            checks,
            "bh2_points_total",
            bh_spec.get("points_total") == 36,
            f"expected 36, got {bh_spec.get('points_total')!r}",
        )

    # non-fatal observations
    _check(
        checks,
        "calc_generated_at_matches_published_at",
        calc.get("generated_at") == revision_meta.get("published_at"),
        f"calc={calc.get('generated_at')!r} revision published_at="
        f"{revision_meta.get('published_at')!r}",
        severity="info",
    )
    local_inputs = (revision.get("meta") or {}).get("local_inputs") or {}
    _check(
        checks,
        "local_inputs_provenance",
        local_inputs.get("entries") == 17 and local_inputs.get("applied") == 17,
        f"entries={local_inputs.get('entries')!r} applied={local_inputs.get('applied')!r}",
        severity="info",
    )
    _check(
        checks,
        "revision_previous_id",
        bool(revision_meta.get("previous_revision_id")),
        f"previous_revision_id={revision_meta.get('previous_revision_id')!r}",
        severity="info",
    )

    if fatal:
        failures = [c for c in checks if c["status"] == "fail"]
        detail = "; ".join(f"{c['check']}: {c['detail']}" for c in failures)
        raise InputError(f"input contract check failed ({len(failures)}): {detail}")

    return InputBundle(
        root=base,
        build_dir=build_dir_path,
        revision_dir=revision_dir,
        dataset_current_path=dataset_current_file,
        dataset_manifest_path=manifest_path if manifest_path.is_file() else Path(),
        database_path=database_path,
        calc_input_path=calc_path,
        current=current,
        revision=revision,
        build=revision,
        calculation_input=calc,
        dataset_current=dataset_current,
        dataset_manifest=dataset_manifest,
        dataset_meta=dataset_meta,
        dataset_fingerprint=dataset_fingerprint,
        fingerprints=fingerprints,
        checks=checks,
    )


def load_input_bundle(
    root: str | Path | None = None,
    *,
    build_dir: str | Path | None = None,
    calc_input_path: str | Path | None = None,
    dataset_current_path: str | Path | None = None,
    verify_database_file: bool = True,
) -> InputBundle:
    """Compatibility alias for :func:`load_bundle` (same explicit-input contract)."""
    return load_bundle(
        root,
        build_dir=build_dir,
        calc_input_path=calc_input_path,
        dataset_current_path=dataset_current_path,
        verify_database_file=verify_database_file,
    )


__all__ = [
    "CALC_SCHEMA_VERSION",
    "DEFAULT_BUILD_DIR",
    "DEFAULT_CALC_INPUT",
    "EXPECTED_ITEM_COUNT",
    "EXPECTED_MOD_COUNT",
    "EXPECTED_PASSIVE_NODES",
    "EXPECTED_PASSIVE_POINTS",
    "EXPECTED_SPECIALIZATIONS",
    "InputBundle",
    "InputError",
    "load_bundle",
    "load_input_bundle",
]
