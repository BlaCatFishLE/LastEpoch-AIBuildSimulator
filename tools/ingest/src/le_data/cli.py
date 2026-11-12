"""Command line interface: ``le-data``.

Exit codes (documented in README):
  0 = success / validation passed
  1 = execution or validation failure
  2 = usage / argument error
  3 = partial completion (some sources or stages failed but usable output exists)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .config import load_coverage_scope, load_sources
from .fetch import FetchError
from .normalize.base import Crosswalk, Normalizer
from .paths import (
    CONFIG_DIR,
    DATA_DIR,
    NORMALIZED_DIR,
    PROJECT_ROOT,
    RAW_DIR,
    REPORTS_DIR,
    STAGING_DIR,
    ensure_dirs,
)
from .records import SourceIssue, SourceRecord, read_jsonl, write_jsonl
from .runs import RunStore
from .store import (
    SCHEMA_VERSION,
    NORMALIZER_VERSION,
    apply_migrations,
    connect,
    checkpoint,
    insert_row,
    insert_rows,
    logical_fingerprint,
    now_utc,
    table_counts,
    upsert_dataset_meta,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_PARTIAL = 3

STATUS = {
    "ok": EXIT_OK,
    "partial": EXIT_PARTIAL,
    "failed": EXIT_FAIL,
}


def log(message: str) -> None:
    print(message, file=sys.stderr)


def _version_slug(version: str | None) -> str:
    if not version:
        return "unknown"
    return "".join(ch for ch in version if ch.isalnum())[:16]


def _snapshot_id(source: str, claimed_version: str | None, run_id: str | None = None) -> str:
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    base = f"{source}-v{_version_slug(claimed_version)}-{date}"
    if run_id:
        # A run sequence suffix keeps already-published snapshot directories
        # immutable when the same source/version/date is discovered again.
        suffix = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8]
        base = f"{base}-{suffix}"
    return base


_SNAPSHOT_VOLATILE_KEYS = {
    "discovered_at",
    "fetched_at",
    "observed_at",
    "generated_at",
    "timestamp",
    "fetch_started_at",
    "fetch_finished_at",
    "page_results",
    "content_fingerprint",
}


def _inventory_fingerprint(inventory: dict) -> str:
    """Content fingerprint of a discovery inventory, ignoring wall-clock fields."""

    def scrub(value):
        if isinstance(value, dict):
            return {
                key: scrub(item)
                for key, item in value.items()
                if key not in _SNAPSHOT_VOLATILE_KEYS
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    payload = json.dumps(scrub(inventory), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _next_snapshot_id(base: str) -> str:
    return base + "-r2"


def _adapter(source: str, cfg, raw_root: Path):
    from .adapters import get_adapter

    return get_adapter(source, cfg.get(source, {}), raw_root, logger=log)


def _sources_from_args(args) -> list[str]:
    cfg = load_sources()
    if args.source and args.source != "all":
        return [args.source]
    return cfg.enabled_sources()


# -- doctor -----------------------------------------------------------------
def cmd_doctor(args) -> int:
    checks: list[dict] = []
    ok = True

    def check(name: str, passed: bool, detail, required: bool = True) -> None:
        nonlocal ok
        checks.append({"check": name, "ok": passed, "required": required, "detail": detail})
        if required and not passed:
            ok = False

    check("python_version", sys.version_info >= (3, 11), sys.version.split()[0])
    check("sqlite_version", sqlite3.sqlite_version_info >= (3, 35), sqlite3.sqlite_version)
    check("project_root", (PROJECT_ROOT / "docs" / "phase-1-agent-workguide.zh-CN.md").exists(), str(PROJECT_ROOT))
    try:
        cfg = load_sources()
        check("sources_config", True, {"path": str(cfg.path), "sources": list(cfg.sources)})
    except Exception as exc:  # noqa: BLE001
        cfg = None
        check("sources_config", False, str(exc))
    try:
        scope = load_coverage_scope()
        check("coverage_scope", True, {"domains": [d["id"] for d in scope.get("required_domains", [])]})
    except Exception as exc:  # noqa: BLE001
        check("coverage_scope", False, str(exc))
    writable = True
    detail: dict = {}
    for name, path in ("data", DATA_DIR), ("reports", REPORTS_DIR), ("staging", STAGING_DIR):
        try:
            ensure_dirs(path)
            probe = path / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            detail[name] = str(path)
        except Exception as exc:  # noqa: BLE001
            writable = False
            detail[name] = f"not writable: {exc}"
    check("project_dirs", writable, detail)
    if cfg is not None:
        for source in cfg.enabled_sources():
            try:
                adapter = _adapter(source, cfg.sources, RAW_DIR)
                check(f"adapter_{source}", True, type(adapter).__name__)
            except Exception as exc:  # noqa: BLE001
                check(f"adapter_{source}", False, f"{type(exc).__name__}: {exc}")
        game_cfg = cfg.sources.get("game", {})
        if game_cfg.get("install_dir"):
            exists = Path(game_cfg["install_dir"]).exists()
            check("game_install", exists, game_cfg["install_dir"], required=False)
    if args.network:
        import urllib.request

        for source in cfg.enabled_sources() if cfg else []:
            url = cfg.sources[source].get("version_probe", {}).get("url")
            if not url:
                continue
            try:
                with urllib.request.urlopen(url, timeout=15) as response:
                    check(f"network_{source}", response.status == 200, f"{url} -> {response.status}", required=False)
            except Exception as exc:  # noqa: BLE001
                check(f"network_{source}", False, f"{url} -> {exc}", required=False)
    print(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False, indent=2))
    return EXIT_OK if ok else EXIT_FAIL


# -- discover / fetch / parse ----------------------------------------------
def cmd_discover(args) -> int:
    cfg = load_sources()
    sources = _sources_from_args(args)
    store = RunStore(args.run)
    statuses: dict[str, str] = {}
    for source in sources:
        source_cfg = cfg.get(source)
        base_id = args.snapshot or _snapshot_id(
            source, source_cfg.get("claimed_version"), args.run
        )
        snapshot_id = base_id
        snapshot_dir = RAW_DIR / source / snapshot_id
        old_inventory = None
        old_fingerprint = None
        if snapshot_dir.joinpath("inventory.json").exists():
            try:
                old_inventory = json.loads(
                    snapshot_dir.joinpath("inventory.json").read_text(encoding="utf-8")
                )
                old_fingerprint = old_inventory.get("content_fingerprint")
            except (OSError, json.JSONDecodeError):
                old_inventory = None
        try:
            adapter = _adapter(source, cfg.sources, RAW_DIR)
            inventory = adapter.discover(snapshot_id, offline=args.offline)
            fingerprint = _inventory_fingerprint(inventory)
            # A published raw snapshot directory must stay immutable.  If the
            # same source/version/date yields different discovery content, put
            # the new content in a new suffixed snapshot instead of overwriting.
            attempts = 0
            while (
                old_fingerprint is not None
                and fingerprint != old_fingerprint
                and args.snapshot is None
                and attempts < 3
            ):
                snapshot_id = f"{base_id}-r{attempts + 2}"
                snapshot_dir = RAW_DIR / source / snapshot_id
                snapshot_dir.mkdir(parents=True, exist_ok=True)
                log(f"[discover] {source}: discovery changed; preserving the old snapshot and "
                    f"using a new one: {snapshot_id}")
                adapter = _adapter(source, cfg.sources, RAW_DIR)
                inventory = adapter.discover(snapshot_id, offline=args.offline)
                fingerprint = _inventory_fingerprint(inventory)
                attempts += 1
            inventory.setdefault("source", source)
            inventory.setdefault("source_snapshot_id", snapshot_id)
            inventory.setdefault("discovered_at", now_utc())
            inventory.setdefault("claimed_version", source_cfg.get("claimed_version"))
            inventory["content_fingerprint"] = fingerprint
            inventory_path = snapshot_dir / "inventory.json"
            inventory_path.write_text(
                json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            issues = [issue.to_dict() for issue in getattr(adapter, "issues", [])]
            if issues:
                (snapshot_dir / "discover_issues.json").write_text(
                    json.dumps(issues, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            status = inventory.get("status", "ok")
            statuses[source] = status
            store.set_snapshot(source, snapshot_id, status=status)
            store.mark_stage("discover", source, status)
            log(f"[discover] {source}: snapshot={snapshot_id} status={status} "
                f"resources={len(inventory.get('resources', []))} "
                f"fingerprint={fingerprint}")
        except Exception as exc:  # noqa: BLE001
            statuses[source] = "failed"
            store.mark_stage("discover", source, "failed", str(exc))
            log(f"[discover] {source} FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc(file=sys.stderr)
    return EXIT_OK if all(status == "ok" for status in statuses.values()) else EXIT_PARTIAL


def cmd_fetch(args) -> int:
    cfg = load_sources()
    store = RunStore(args.run)
    state = store.load()
    if state is None:
        log(f"run {args.run} does not exist; run discover first")
        return EXIT_FAIL
    sources = _sources_from_args(args) if args.source else list(state.snapshots)
    statuses: dict[str, str] = {}
    for source in sources:
        snapshot_id = state.snapshots.get(source)
        if not snapshot_id:
            statuses[source] = "failed"
            log(f"[fetch] {source}: no snapshot recorded; run discover first")
            continue
        snapshot_dir = RAW_DIR / source / snapshot_id
        inventory_path = snapshot_dir / "inventory.json"
        inventory = (
            json.loads(inventory_path.read_text(encoding="utf-8"))
            if inventory_path.exists()
            else None
        )
        try:
            adapter = _adapter(source, cfg.sources, RAW_DIR)
            results = adapter.fetch(snapshot_id, offline=args.offline, inventory=inventory)
            manifest = {
                "source": source,
                "source_snapshot_id": snapshot_id,
                "fetched_at": now_utc(),
                "offline": bool(args.offline),
                "results": results,
            }
            (snapshot_dir / "fetch_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            failed = [item for item in results if item.get("fetch_status") == "failed"]
            status = "ok" if not failed else ("partial" if len(failed) < len(results) else "failed")
            statuses[source] = status
            store.mark_stage("fetch", source, status)
            log(f"[fetch] {source}: {len(results)} resources, {len(failed)} failed -> {status}")
        except Exception as exc:  # noqa: BLE001
            statuses[source] = "failed"
            store.mark_stage("fetch", source, "failed", str(exc))
            log(f"[fetch] {source} FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc(file=sys.stderr)
    return EXIT_OK if all(status == "ok" for status in statuses.values()) else EXIT_PARTIAL


def cmd_parse(args) -> int:
    cfg = load_sources()
    store = RunStore(args.run)
    state = store.load()
    if state is None:
        log(f"run {args.run} does not exist; run discover first")
        return EXIT_FAIL
    sources = _sources_from_args(args) if args.source else list(state.snapshots)
    statuses: dict[str, str] = {}
    for source in sources:
        snapshot_id = state.snapshots.get(source)
        if not snapshot_id:
            statuses[source] = "failed"
            log(f"[parse] {source}: no snapshot recorded")
            continue
        try:
            adapter = _adapter(source, cfg.sources, RAW_DIR)
            records = []
            for record in adapter.parse(snapshot_id):
                records.append(record.to_dict())
            path = store.source_records_path(source)
            count = write_jsonl(path, records)
            issues = [issue.to_dict() for issue in getattr(adapter, "issues", [])]
            by_kind: dict[str, int] = {}
            by_status: dict[str, int] = {}
            for record in records:
                by_kind[record["entity_kind"]] = by_kind.get(record["entity_kind"], 0) + 1
                by_status[record["parse_status"]] = by_status.get(record["parse_status"], 0) + 1
            summary = {
                "source": source,
                "source_snapshot_id": snapshot_id,
                "records": count,
                "by_kind": by_kind,
                "by_parse_status": by_status,
                "issues": issues,
                "output": str(path),
            }
            store.summary_path(source).write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            status = "ok" if count > 0 else "failed"
            statuses[source] = status
            store.mark_stage("parse", source, status)
            log(f"[parse] {source}: {count} records {by_kind} statuses={by_status}")
        except Exception as exc:  # noqa: BLE001
            statuses[source] = "failed"
            store.mark_stage("parse", source, "failed", str(exc))
            log(f"[parse] {source} FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc(file=sys.stderr)
    return EXIT_OK if all(status == "ok" for status in statuses.values()) else EXIT_PARTIAL


# -- normalize --------------------------------------------------------------
def _insert_provenance(conn, store: RunStore, state, source: str, snapshot_id: str) -> None:
    snapshot_dir = RAW_DIR / source / snapshot_id
    inventory_path = snapshot_dir / "inventory.json"
    inventory = (
        json.loads(inventory_path.read_text(encoding="utf-8")) if inventory_path.exists() else {}
    )
    fetch_manifest_path = snapshot_dir / "fetch_manifest.json"
    fetch_manifest = (
        json.loads(fetch_manifest_path.read_text(encoding="utf-8"))
        if fetch_manifest_path.exists()
        else None
    )
    run_status = state.source_status.get(source, "unknown")
    snapshot_status = {"ok": "ok", "partial": "partial", "failed": "failed"}.get(
        run_status, "partial"
    )
    version_evidence = inventory.get("version_evidence") or {}
    insert_row(
        conn,
        "source_snapshots",
        {
            "source_snapshot_id": snapshot_id,
            "source": source,
            "claimed_version": inventory.get("claimed_version"),
            "version_evidence": json.dumps(version_evidence, ensure_ascii=False)
            if version_evidence
            else None,
            "fetch_mode": "cache" if fetch_manifest and fetch_manifest.get("offline") else "auto",
            "fetch_started_at": (fetch_manifest or {}).get("fetched_at"),
            "fetch_finished_at": (fetch_manifest or {}).get("fetched_at"),
            "status": snapshot_status,
            "entry_url": inventory.get("entry_url"),
            "notes": "; ".join(inventory.get("notes", [])) if isinstance(inventory.get("notes"), list) else None,
        },
        or_replace=True,
    )
    if fetch_manifest:
        for result in fetch_manifest.get("results", []):
            resource_id = f"{snapshot_id}:{result.get('resource_name')}"
            insert_row(
                conn,
                "raw_resources",
                {
                    "resource_id": resource_id,
                    "source_snapshot_id": snapshot_id,
                    "source": source,
                    "resource_name": result.get("resource_name"),
                    "url": result.get("url"),
                    "local_path": result.get("path"),
                    "sha256": result.get("sha256"),
                    "size_bytes": result.get("size"),
                    "content_type": result.get("content_type"),
                    "http_status": result.get("http_status"),
                    "etag": result.get("etag"),
                    "last_modified": result.get("last_modified"),
                    "fetched_at": result.get("fetched_at"),
                    "fetch_status": result.get("fetch_status"),
                    "role": result.get("role"),
                    "notes": result.get("error"),
                },
                or_ignore=True,
            )


def cmd_normalize(args) -> int:
    cfg = load_sources()
    scope = load_coverage_scope()
    store = RunStore(args.run)
    state = store.load()
    if state is None:
        log(f"run {args.run} does not exist")
        return EXIT_FAIL
    dataset_id = args.dataset
    staging_db = store.staging_db_path()
    if staging_db.exists() and not args.keep:
        staging_db.unlink()
    conn = connect(staging_db)
    try:
        apply_migrations(conn)
        crosswalk = Crosswalk.load()
        per_source: dict[str, dict] = {}
        for source, snapshot_id in state.snapshots.items():
            records_path = store.source_records_path(source)
            if not records_path.exists():
                log(f"[normalize] {source}: no parsed records at {records_path}; skipping")
                continue
            _insert_provenance(conn, store, state, source, snapshot_id)
            records = []
            for row in read_jsonl(records_path):
                record = SourceRecord.from_dict(row)
                records.append(record)
                insert_row(
                    conn,
                    "source_records",
                    {
                        "record_id": record.record_id,
                        "source_snapshot_id": record.source_snapshot_id,
                        "source": record.source,
                        "resource_name": record.resource_name,
                        "entity_kind": record.entity_kind,
                        "source_scope": record.source_scope,
                        "source_id": record.source_id,
                        "locator": record.locator,
                        "raw_payload": json.dumps(record.raw, ensure_ascii=False),
                        "parse_status": record.parse_status,
                        "notes": "; ".join(record.notes) if record.notes else None,
                    },
                    or_ignore=True,
                )
            normalizer = Normalizer(
                conn, source, snapshot_id, crosswalk=crosswalk, logger=log
            )
            summary = normalizer.normalize_records(records)
            per_source[source] = summary
            log(f"[normalize] {source}: {summary['entity_counts']}")
        # Semantic post-passes: link effect values to stat definitions before
        # reshaping domains (idols/blessings derive from item bases).
        stat_link_result: dict = {}
        try:
            from .normalize.stat_link import link_stats

            stat_link_result = link_stats(conn) or {}
            log(f"[normalize] stat link: {stat_link_result}")
        except ModuleNotFoundError:
            log("[normalize] stat_link module not available; effect->stat linking skipped")
        source_versions = {
            row["source"]: row["claimed_version"]
            for row in conn.execute("SELECT source, claimed_version FROM source_snapshots")
        }
        target_version = scope.get("target", {}).get("game_version")
        versions_consistent = bool(source_versions) and all(
            version == target_version for version in source_versions.values()
        )
        compatibility = {
            "target_game_version": target_version,
            "target_season": scope.get("target", {}).get("season"),
            "sources": source_versions,
            "decision": "compatible" if versions_consistent else "unverified",
            "evidence": "claimed page versions equal the target; module-level version evidence is "
            "stored per source snapshot in source_snapshots.version_evidence",
        }
        upsert_dataset_meta(
            conn,
            dataset_id,
            status="staging",
            target_game_version=target_version,
            target_season=scope.get("target", {}).get("season"),
            mapping_revision=crosswalk.revision,
            compatibility=json.dumps(compatibility, ensure_ascii=False),
            notes=f"run={args.run}; sources={list(state.snapshots)}",
        )
        from .normalize.domain_reshape import reshape_domains

        reshaped = reshape_domains(conn)
        if any(reshaped.values()):
            log(f"[normalize] domain reshape: {reshaped}")
        for source, snapshot_id in state.snapshots.items():
            insert_row(
                conn,
                "dataset_source_snapshots",
                {"dataset_id": dataset_id, "source_snapshot_id": snapshot_id, "source": source},
                or_ignore=True,
            )
        conn.commit()
        fingerprint = logical_fingerprint(conn)
        insert_row(
            conn,
            "dataset_fingerprints",
            {
                "dataset_id": dataset_id,
                "logical_sha256": fingerprint["logical_sha256"],
                "table_counts": json.dumps(table_counts(conn), ensure_ascii=False),
            },
            or_replace=True,
        )
        conn.commit()
        checkpoint(conn)
    finally:
        conn.close()

    target_dir = NORMALIZED_DIR / dataset_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_db = target_dir / "data.sqlite"
    if target_db.exists():
        target_db.unlink()
    shutil.copyfile(staging_db, target_db)
    (target_dir / "dataset.json").write_text(
        json.dumps(
            {
                "dataset_id": dataset_id,
                "run_id": args.run,
                "snapshots": state.snapshots,
                "created_at": now_utc(),
                "status": "staged",
                "normalizer_version": NORMALIZER_VERSION,
                "schema_version": SCHEMA_VERSION,
                "logical_sha256": fingerprint["logical_sha256"],
                "per_source": per_source,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"[normalize] dataset {dataset_id} -> {target_db} "
        f"logical_sha256={fingerprint['logical_sha256']}")
    return EXIT_OK


# -- validate / report / publish -------------------------------------------
def resolve_dataset_db(args, *, allow_current: bool = True) -> Path:
    if getattr(args, "db", None):
        path = Path(args.db)
        if not path.exists():
            raise FileNotFoundError(f"database not found: {path}")
        return path
    if getattr(args, "dataset", None):
        path = NORMALIZED_DIR / args.dataset / "data.sqlite"
        if not path.exists():
            raise FileNotFoundError(f"dataset {args.dataset} has no database at {path}")
        return path
    if allow_current:
        current = DATA_DIR / "current.json"
        if current.exists():
            data = json.loads(current.read_text(encoding="utf-8"))
            path = PROJECT_ROOT / data["database"]
            if path.exists():
                return path
    raise FileNotFoundError("no database selected; pass --dataset or --db")


def _validate_db(db_path: Path, dataset_id: str, report_key: str) -> tuple[dict, int]:
    from .validation.checks import Validator, write_validation_reports

    scope = load_coverage_scope()
    conn = connect(db_path, readonly=True)
    try:
        result = Validator(conn, scope).run_all()
    finally:
        conn.close()
    report_dir = REPORTS_DIR / report_key
    paths = write_validation_reports(result, report_dir)
    result["report_paths"] = {name: str(path) for name, path in paths.items()}
    result["dataset_id"] = dataset_id
    result["database"] = str(db_path)
    exit_code = EXIT_OK if result["ok"] else EXIT_PARTIAL
    if not result["ok"]:
        exit_code = EXIT_FAIL
    # Persist the validation verdict into the dataset itself.
    conn = connect(db_path)
    try:
        new_status = "valid" if result["ok"] else "partial"
        conn.execute("UPDATE dataset_meta SET status = ? WHERE dataset_id = ?", (new_status, dataset_id))
        conn.commit()
        checkpoint(conn)
    finally:
        conn.close()
    dataset_json = db_path.parent / "dataset.json"
    if dataset_json.exists():
        data = json.loads(dataset_json.read_text(encoding="utf-8"))
        data["status"] = "valid" if result["ok"] else "partial"
        data["validation"] = {
            "ok": result["ok"],
            "counts": result["counts"],
            "report": str((REPORTS_DIR / report_key / "validation.json")),
        }
        dataset_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return result, exit_code


def cmd_validate(args) -> int:
    try:
        db_path = resolve_dataset_db(args)
    except FileNotFoundError as exc:
        log(str(exc))
        return EXIT_FAIL
    conn = connect(db_path, readonly=True)
    try:
        row = conn.execute("SELECT dataset_id FROM dataset_meta").fetchone()
    finally:
        conn.close()
    dataset_id = row["dataset_id"] if row else args.dataset or db_path.parent.name
    result, exit_code = _validate_db(db_path, dataset_id, args.report_key or dataset_id)
    print(json.dumps({"ok": result["ok"], "status": result["status"],
                      "counts": result["counts"], "reports": result["report_paths"]},
                     ensure_ascii=False, indent=2))
    return exit_code


def cmd_report(args) -> int:
    from .report.coverage import build_coverage, write_coverage_reports
    from .report.quality import build_quality, write_quality_reports

    try:
        db_path = resolve_dataset_db(args)
    except FileNotFoundError as exc:
        log(str(exc))
        return EXIT_FAIL
    scope = load_coverage_scope()
    conn = connect(db_path, readonly=True)
    try:
        coverage = build_coverage(conn, scope)
        quality = build_quality(conn)
    finally:
        conn.close()
    report_dir = REPORTS_DIR / (args.report_key or coverage["dataset_id"] or "report")
    paths = write_coverage_reports(coverage, report_dir)
    paths.update(write_quality_reports(quality, report_dir))
    conn = connect(db_path, readonly=True)
    try:
        issues = [
            dict(row)
            for row in conn.execute(
                "SELECT issue_id, issue_type, severity, source, entity_id, field_path, message, "
                "locator, status FROM data_issues ORDER BY severity, issue_type"
            )
        ]
    finally:
        conn.close()
    issues_path = report_dir / "issues.jsonl"
    with open(issues_path, "w", encoding="utf-8") as handle:
        for issue in issues:
            handle.write(json.dumps(issue, ensure_ascii=False) + "\n")
    paths["issues"] = issues_path
    print(json.dumps({name: str(path) for name, path in paths.items()}, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_manifest(args) -> int:
    from .report.manifest import build_manifest

    try:
        db_path = resolve_dataset_db(args)
    except FileNotFoundError as exc:
        log(str(exc))
        return EXIT_FAIL
    conn = connect(db_path)
    try:
        meta = conn.execute("SELECT dataset_id FROM dataset_meta").fetchone()
        dataset_id = meta["dataset_id"] if meta else db_path.parent.name
        manifest = build_manifest(conn, db_path, dataset_id=dataset_id,
                                  generation_command=" ".join(sys.argv),
                                  limitations=args.limitation or [])
    finally:
        conn.close()
    out = Path(args.out) if args.out else db_path.parent / "manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[manifest] wrote {out}")
    return EXIT_OK


def cmd_publish(args) -> int:
    from .report.manifest import publish_dataset

    try:
        db_path = resolve_dataset_db(args)
    except FileNotFoundError as exc:
        log(str(exc))
        return EXIT_FAIL
    conn = connect(db_path, readonly=True)
    try:
        meta = conn.execute("SELECT dataset_id, status FROM dataset_meta").fetchone()
    finally:
        conn.close()
    dataset_id = meta["dataset_id"] if meta else db_path.parent.name
    if not args.skip_validate:
        result, _ = _validate_db(db_path, dataset_id, args.report_key or dataset_id)
        if not result["ok"] and not args.allow_partial:
            log(f"[publish] validation failed ({result['counts']}); use --allow-partial for a "
                f"partial release that will not replace current.json")
            return EXIT_FAIL
    current_exists = (DATA_DIR / "current.json").exists()
    try:
        output = publish_dataset(
            dataset_id,
            dataset_db=db_path,
            generation_command=" ".join(sys.argv),
            limitations=args.limitation or [],
            allow_partial=bool(args.allow_partial),
            force_current=bool(args.force_current),
        )
        result = output.get("result", "published")
        if result == "partial_released":
            log("[publish] partial dataset released but current.json was not replaced "
                "(pass --force-current to override)")
            return EXIT_PARTIAL
        if result == "idempotent":
            log("[publish] identical dataset content already released; current.json unchanged")
    except Exception as exc:  # noqa: BLE001
        log(f"[publish] failed: {type(exc).__name__}: {exc}")
        return EXIT_FAIL
    print(json.dumps(output.get("current"), ensure_ascii=False, indent=2))
    return EXIT_OK


# -- rebuild / diff --------------------------------------------------------
def cmd_rebuild(args) -> int:
    from .report.diff import diff_databases

    store = RunStore(args.from_run)
    state = store.load()
    if state is None:
        log(f"source run {args.from_run} does not exist")
        return EXIT_FAIL
    # Reuse the same parse/normalize machinery offline.
    parse_args = argparse.Namespace(
        run=args.from_run, source="all", offline=True, verbose=args.verbose
    )
    code = cmd_parse(parse_args)
    if code == EXIT_FAIL:
        return EXIT_FAIL
    normalize_args = argparse.Namespace(
        run=args.from_run, dataset=args.dataset, keep=False, verbose=args.verbose
    )
    code = cmd_normalize(normalize_args)
    if code != EXIT_OK:
        return code
    new_db = NORMALIZED_DIR / args.dataset / "data.sqlite"
    if args.compare:
        old_db = NORMALIZED_DIR / args.compare / "data.sqlite"
        if not old_db.exists():
            log(f"comparison dataset {args.compare} has no database")
            return EXIT_FAIL
        result = diff_databases(old_db, new_db, full_enumeration=True)
        report_dir = REPORTS_DIR / f"{args.dataset}-rebuild"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "rebuild_diff.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(
            {
                "identical": result["identical"],
                "old": result["old"]["logical_sha256"],
                "new": result["new"]["logical_sha256"],
                "changed_tables": {k: v["modified"] + v["added"] + v["removed"]
                                   for k, v in result["changed_tables"].items()},
                "diff_report": str(report_dir / "rebuild_diff.json"),
            },
            ensure_ascii=False,
            indent=2,
        ))
    return EXIT_OK


def cmd_diff(args) -> int:
    from .report.diff import diff_databases

    old_db = Path(args.old)
    new_db = Path(args.new)
    for path in (old_db, new_db):
        if not path.exists():
            log(f"database not found: {path}")
            return EXIT_FAIL
    result = diff_databases(old_db, new_db, full_enumeration=args.full_enumeration)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"[diff] wrote {out}")
    else:
        print(json.dumps(
            {
                "identical": result["identical"],
                "old": result["old"]["logical_sha256"],
                "new": result["new"]["logical_sha256"],
                "changed_tables": {
                    table: {"added": info["added"], "removed": info["removed"], "modified": info["modified"]}
                    for table, info in result["changed_tables"].items()
                },
                "entities": {key: result["entities"][key] for key in
                             ("added_count", "removed_count", "renamed_count", "added_by_kind")},
            },
            ensure_ascii=False,
            indent=2,
        ))
    return EXIT_OK


# -- query / trace ---------------------------------------------------------
def cmd_query(args) -> int:
    try:
        db_path = resolve_dataset_db(args)
    except FileNotFoundError as exc:
        log(str(exc))
        return EXIT_FAIL
    conn = connect(db_path, readonly=True)
    try:
        clauses: list[str] = []
        params: list = []
        if args.kind:
            clauses.append("e.kind = ?")
            params.append(args.kind)
        if args.name:
            clauses.append(
                "(n.name LIKE ? OR e.canonical_name LIKE ?)"
            )
            params.extend([f"%{args.name}%", f"%{args.name}%"])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT DISTINCT e.entity_id, e.kind, e.canonical_name, e.record_status "
            "FROM entities e LEFT JOIN entity_names n ON n.entity_id = e.entity_id "
            f"{where} ORDER BY e.kind, e.canonical_name LIMIT ?"
        )
        params.append(args.limit)
        rows = [dict(row) for row in conn.execute(sql, params)]
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for row in rows:
                print(f"{row['entity_id']}\t{row['kind']}\t{row['canonical_name']}\t{row['record_status']}")
            if not rows:
                print("(no matches)", file=sys.stderr)
    finally:
        conn.close()
    return EXIT_OK


def cmd_trace(args) -> int:
    try:
        db_path = resolve_dataset_db(args)
    except FileNotFoundError as exc:
        log(str(exc))
        return EXIT_FAIL
    conn = connect(db_path, readonly=True)
    try:
        entity_id = args.entity_id
        if not entity_id and args.name:
            row = conn.execute(
                "SELECT e.entity_id FROM entities e LEFT JOIN entity_names n "
                "ON n.entity_id = e.entity_id WHERE n.name = ? OR e.canonical_name = ? LIMIT 1",
                (args.name, args.name),
            ).fetchone()
            entity_id = row["entity_id"] if row else None
        if not entity_id:
            log("entity not found; pass --entity-id or --name")
            return EXIT_FAIL
        entity = conn.execute("SELECT * FROM entities WHERE entity_id = ?", (entity_id,)).fetchone()
        if entity is None:
            log(f"entity not found: {entity_id}")
            return EXIT_FAIL
        output: dict = {
            "entity": dict(entity),
            "names": [dict(row) for row in conn.execute(
                "SELECT language, name, is_alias, source, name_key FROM entity_names WHERE entity_id = ?",
                (entity_id,))],
            "source_ids": [dict(row) for row in conn.execute(
                "SELECT source, source_snapshot_id, kind, source_scope, source_id, mapping_method "
                "FROM source_id_map WHERE entity_id = ?", (entity_id,))],
            "relationships": [dict(row) for row in conn.execute(
                "SELECT relationship_type, to_entity_id, raw_target_ref, resolution_status, "
                "qualifiers, evidence FROM entity_relationships WHERE from_entity_id = ?",
                (entity_id,))],
            "incoming_relationships": [dict(row) for row in conn.execute(
                "SELECT from_entity_id, relationship_type, resolution_status FROM "
                "entity_relationships WHERE to_entity_id = ?", (entity_id,))],
            "issues": [dict(row) for row in conn.execute(
                "SELECT issue_id, issue_type, severity, message FROM data_issues WHERE entity_id = ?",
                (entity_id,))],
            "evidence_count": conn.execute(
                "SELECT COUNT(*) FROM field_evidence WHERE entity_id = ?", (entity_id,)
            ).fetchone()[0],
        }
        for table in ("skills", "trees", "nodes", "buffs", "ailments", "items", "affixes"):
            row = conn.execute(f"SELECT * FROM {table} WHERE entity_id = ?", (entity_id,)).fetchone()
            if row is not None:
                output[table] = dict(row)
        if args.evidence:
            output["evidence"] = [dict(row) for row in conn.execute(
                "SELECT field_path, raw_value, normalized_value, unit, evidence_status, "
                "extraction_method, locator FROM field_evidence WHERE entity_id = ? "
                "ORDER BY field_path LIMIT ?", (entity_id, args.limit))]
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return EXIT_OK


# -- argument parsing ------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="le-data",
        description="Last Epoch phase-1 data extraction and normalization pipeline",
    )
    parser.add_argument("--verbose", action="store_true", help="verbose logging")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check interpreter, dependencies, project dirs")
    doctor.add_argument("--network", action="store_true", help="also probe source URLs")
    doctor.set_defaults(func=cmd_doctor)

    discover = sub.add_parser("discover", help="discover sources, versions and resources")
    discover.add_argument("--source", default="all")
    discover.add_argument("--run", required=True)
    discover.add_argument("--snapshot", default=None)
    discover.add_argument("--offline", action="store_true")
    discover.set_defaults(func=cmd_discover)

    fetch = sub.add_parser("fetch", help="fetch discovered resources into the raw cache")
    fetch.add_argument("--source", default=None)
    fetch.add_argument("--run", required=True)
    fetch.add_argument("--offline", action="store_true")
    fetch.set_defaults(func=cmd_fetch)

    parse = sub.add_parser("parse", help="parse cached raw resources into source records")
    parse.add_argument("--source", default=None)
    parse.add_argument("--run", required=True)
    parse.add_argument("--offline", action="store_true", default=True)
    parse.set_defaults(func=cmd_parse)

    normalize = sub.add_parser("normalize", help="build a normalized staging database")
    normalize.add_argument("--run", required=True)
    normalize.add_argument("--dataset", required=True)
    normalize.add_argument("--offline", action="store_true", default=True)
    normalize.add_argument("--keep", action="store_true", help="keep existing staging db")
    normalize.set_defaults(func=cmd_normalize)

    validate = sub.add_parser("validate", help="validate a normalized dataset")
    validate.add_argument("--dataset")
    validate.add_argument("--db")
    validate.add_argument("--report-key", default=None)
    validate.set_defaults(func=cmd_validate)

    report = sub.add_parser("report", help="write coverage, quality and issue reports")
    report.add_argument("--dataset")
    report.add_argument("--db")
    report.add_argument("--report-key", default=None)
    report.set_defaults(func=cmd_report)

    manifest = sub.add_parser("manifest", help="write dataset manifest")
    manifest.add_argument("--dataset")
    manifest.add_argument("--db")
    manifest.add_argument("--out", default=None)
    manifest.add_argument("--limitation", action="append")
    manifest.set_defaults(func=cmd_manifest)

    publish = sub.add_parser("publish", help="validate and publish a dataset release")
    publish.add_argument("--dataset")
    publish.add_argument("--db")
    publish.add_argument("--report-key", default=None)
    publish.add_argument("--allow-partial", action="store_true")
    publish.add_argument("--force-current", action="store_true")
    publish.add_argument("--skip-validate", action="store_true")
    publish.add_argument("--limitation", action="append")
    publish.set_defaults(func=cmd_publish)

    rebuild = sub.add_parser("rebuild", help="rebuild a dataset from fixed raw snapshots")
    rebuild.add_argument("--from-run", required=True)
    rebuild.add_argument("--dataset", required=True)
    rebuild.add_argument("--compare", default=None)
    rebuild.set_defaults(func=cmd_rebuild)

    diff = sub.add_parser("diff", help="diff two normalized databases")
    diff.add_argument("--old", required=True)
    diff.add_argument("--new", required=True)
    diff.add_argument("--out", default=None)
    diff.add_argument("--full-enumeration", action="store_true")
    diff.set_defaults(func=cmd_diff)

    query = sub.add_parser("query", help="search entities")
    query.add_argument("--dataset")
    query.add_argument("--db")
    query.add_argument("--kind", default=None)
    query.add_argument("--name", default=None)
    query.add_argument("--limit", type=int, default=50)
    query.add_argument("--json", action="store_true")
    query.set_defaults(func=cmd_query)

    trace = sub.add_parser("trace", help="trace one entity's sources, evidence and issues")
    trace.add_argument("--dataset")
    trace.add_argument("--db")
    trace.add_argument("--entity-id", default=None)
    trace.add_argument("--name", default=None)
    trace.add_argument("--evidence", action="store_true")
    trace.add_argument("--limit", type=int, default=100)
    trace.set_defaults(func=cmd_trace)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        log("interrupted")
        return EXIT_FAIL
    except SystemExit:  # pragma: no cover
        raise
    except Exception as exc:  # noqa: BLE001
        log(f"unhandled error: {type(exc).__name__}: {exc}")
        traceback.print_exc(file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
