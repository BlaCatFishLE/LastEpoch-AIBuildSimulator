"""Command line interface for the phase-2 LE Tools build importer.

Contract (see docs/phase-2-bd-import-plan.zh-CN.md §11.3)::

    python -m le_build import --url https://www.lastepochtools.com/planner/o3ZRm9my --dataset phase1-dataset-002
    python -m le_build import --file build-response.json --share-id o3ZRm9my --offline --dataset phase1-dataset-002
    python -m le_build show --build letools:o3ZRm9my
    python -m le_build explain --build letools:o3ZRm9my --path equipment.hands
    python -m le_build validate --build letools:o3ZRm9my --offline
    python -m le_build view --build letools:o3ZRm9my --out build-view.html

Exit codes: 0 success, 1 failure, 2 argument error, 3 published but partial/unresolved.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from . import __version__
from . import inputs as inputs_module
from .builder import BuildSpecBuilder, IncompatibleBuildError, SchemaError
from .catalog import Catalog, CatalogError
from .paths import BuildPaths, load_json, repo_default, write_json
from .report import (
    acceptance_checks,
    build_gap_report,
    build_import_report,
    render_gap_report_markdown,
    render_import_report_markdown,
)
from .resolve import Resolver
from .sources import letools as letools_source
from .store import (
    StoreError,
    build_ref_parts,
    cleanup_temp_revisions,
    load_build,
    load_current,
    publish_revision,
    write_failure_report,
)
from .validate import coverage, explain, validate_spec

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2
EXIT_PARTIAL = 3


def _load_restorer():
    try:
        from . import rolls as rolls_module
    except ImportError:
        return None
    required = ("restore_affix_value", "restore_implicit_values", "restore_unique_values")
    if all(callable(getattr(rolls_module, name, None)) for name in required):
        return rolls_module
    return None


def _root_from(args) -> Path:
    if getattr(args, "root", None):
        return Path(args.root).resolve()
    if getattr(args, "repo_root", None):
        return Path(args.repo_root).resolve()
    return repo_default()


def _catalog_for(args, root: Path) -> Catalog:
    return Catalog.open(root=root, dataset=getattr(args, "dataset", None), db=getattr(args, "db", None), verify=True)


def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _summary_of_spec(spec: dict) -> dict:
    counts = {
        "equipment": len(spec.get("equipment", {}).get("slots", [])),
        "idols": len(spec.get("idols", {}).get("entries", [])),
        "blessings": len(spec.get("blessings", {}).get("entries", [])),
        "item_instances": len(spec.get("equipment", {}).get("slots", []))
        + len(spec.get("idols", {}).get("entries", []))
        + len(spec.get("blessings", {}).get("entries", [])),
        "specializations": len(spec.get("specializations", [])),
        "action_bar": len(spec.get("action_bar", {}).get("slots", [])),
        "passive_nodes": len(spec.get("passive_tree", {}).get("nodes", [])),
        "skill_tree_nodes": sum(len(entry.get("nodes", [])) for entry in spec.get("specializations", [])),
        "readable_items": (spec.get("readable_summary") or {}).get("items"),
        "readable_by_completeness": (spec.get("readable_summary") or {}).get("by_completeness"),
        "issues": len(spec.get("issues", [])),
    }
    character = spec.get("character") or {}
    return {
        "build_id": (spec.get("meta") or {}).get("build_id"),
        "revision": (spec.get("meta") or {}).get("revision"),
        "scope": {
            "scope_version": (spec.get("scope") or {}).get("scope_version"),
            "excluded_features": [
                entry.get("feature") for entry in (spec.get("scope") or {}).get("excluded_features") or []
            ],
        },
        "character": {
            "level": character.get("level"),
            "class": ((character.get("class") or {}).get("ref") or {}).get("name"),
            "mastery": ((character.get("mastery") or {}).get("ref") or {}).get("name"),
        },
        "counts": counts,
        "statuses": spec.get("statuses"),
        "equipment_slots": [
            {
                "slot": slot.get("slot"),
                "name": _instance_name(slot.get("instance") or {}),
                "affixes": (slot.get("instance") or {}).get("affix_total"),
            }
            for slot in spec.get("equipment", {}).get("slots", [])
        ],
        "action_bar": [
            {"slot": entry.get("slot"), "name": (entry.get("ref") or {}).get("name")}
            for entry in spec.get("action_bar", {}).get("slots", [])
        ],
        "specializations": [
            {
                "slot": entry.get("slot_number"),
                "tree": (entry.get("tree") or {}).get("name"),
                "skill": (entry.get("owner_skill") or {}).get("name"),
                "level": entry.get("source_level"),
                "points": entry.get("points_total"),
            }
            for entry in spec.get("specializations", [])
        ],
    }


def _instance_name(instance: dict) -> str | None:
    definition = (instance.get("definition") or {}).get("ref") or {}
    base = (instance.get("base") or {}).get("ref") or {}
    name = definition.get("name") or base.get("name")
    if definition.get("name") and base.get("name") and definition["name"] != base["name"]:
        return f"{definition['name']} ({base['name']})"
    return name


# --------------------------------------------------------------------- import
def _resolve_share_id(args, file_path: Path | None) -> tuple[str, str | None]:
    if args.url:
        parsed = letools_source.parse_share_url(args.url)
        return parsed["share_id"], parsed["normalized_url"]
    if args.share_id:
        return args.share_id, None
    if file_path is not None:
        meta_path = file_path.parent / "capture-meta.json"
        if meta_path.is_file():
            try:
                meta = load_json(meta_path)
            except Exception:  # pragma: no cover - defensive
                meta = {}
            share_url = meta.get("share_url")
            if share_url:
                parsed = letools_source.parse_share_url(share_url)
                return parsed["share_id"], parsed["normalized_url"]
        stem = file_path.stem
        if stem.startswith("build-") and len(stem) > 6:
            return stem[6:], None
    raise ValueError(
        "cannot determine the share id for an offline file: pass --share-id or place capture-meta.json next to the file"
    )


def _failure_report(status: str, stages: dict, error: str, extra: dict | None = None) -> dict:
    report = {
        "report_version": "import-failure-report-v1",
        "status": status,
        "stages": stages,
        "error": error,
    }
    if extra:
        report.update(extra)
    return report


def cmd_import(args) -> int:
    root = _root_from(args)
    paths = BuildPaths(root)
    requested_file = Path(args.file).resolve() if getattr(args, "file", None) else None
    if bool(args.url) == bool(requested_file):
        print("error: pass exactly one of --url or --file", file=sys.stderr)
        return EXIT_ARGS
    if requested_file is not None and not args.offline:
        print("error: --file imports an archived payload and requires --offline", file=sys.stderr)
        return EXIT_ARGS
    if args.url and args.offline:
        print("error: --url requires network access; drop --offline for live imports", file=sys.stderr)
        return EXIT_ARGS

    try:
        share_id, normalized_url = _resolve_share_id(args, requested_file)
    except (ValueError, letools_source.UrlError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ARGS

    catalog = None
    fetch_meta: dict = {}
    attempt_id = None
    canonical_url = normalized_url or f"https://www.lastepochtools.com/planner/{share_id}"
    stages: dict = {"fetch": "pending", "schema": "pending", "catalog": "pending", "import": "pending"}
    try:
        catalog = _catalog_for(args, root)
        stages["catalog"] = f"ok:{catalog.dataset_id}"
        cleanup_temp_revisions(paths, share_id)
        target_version = (catalog.meta() or {}).get("target_game_version")

        if args.url:
            fetch = letools_source.fetch_build(
                share_id,
                timeout=args.timeout,
                retries=args.retries,
                max_bytes=args.max_bytes,
            )
        else:
            fetch = letools_source.load_archived_file(requested_file)
        stages["fetch"] = "ok"
        archived = letools_source.archive_fetch(paths, share_id, fetch)
        attempt_id = archived["attempt_id"]
        fetch_meta = {
            "attempt_id": archived["attempt_id"],
            "raw_path": str(archived["raw_path"].relative_to(root)),
            "raw_sha256": archived["raw_sha256"],
            "api_url": fetch.get("api_url"),
            "fetched_at": fetch.get("fetched_at"),
            "http_status": fetch.get("http_status"),
            "fetch_mode": fetch.get("mode"),
            "normalized_url": normalized_url,
        }
        draft = letools_source.build_draft(
            fetch, share_id=share_id, requested_url=canonical_url
        )
        version_compatibility = letools_source.classify_versions(draft["versions"], target_version or "")
        if version_compatibility["status"] != "matched":
            stages["schema"] = "incompatible"
            report = _failure_report(
                "incompatible",
                stages,
                "source version is not compatible with the selected catalog dataset; raw payload archived, no revision published",
                {
                    "share_id": share_id,
                    "version_compatibility": version_compatibility,
                    "import": fetch_meta,
                },
            )
            path = write_failure_report(paths, share_id, attempt_id, report)
            print(f"error: incompatible source version; report: {path}", file=sys.stderr)
            _print_json(report)
            return EXIT_FAIL

        restorer = _load_restorer()
        local_inputs_doc = None
        local_inputs_meta = None
        input_index = None
        inputs_path = paths.inputs_file(share_id)
        if not getattr(args, "ignore_local_inputs", False) and inputs_path.is_file():
            try:
                local_inputs_doc = inputs_module.load_document(
                    inputs_path, build_id=f"letools:{share_id}"
                )
            except inputs_module.InputError as exc:
                stages["import"] = "local_inputs_invalid"
                report = _failure_report(
                    "local_inputs_invalid", stages, str(exc),
                    {"share_id": share_id, "inputs_path": str(inputs_path)},
                )
                path = write_failure_report(paths, share_id, attempt_id, report)
                print(f"error: {exc}; report: {path}", file=sys.stderr)
                return EXIT_FAIL
            current_spec = None
            try:
                current_spec = load_build(paths, f"letools:{share_id}")["spec"]
            except StoreError:
                current_spec = None
            if current_spec is not None and local_inputs_doc and local_inputs_doc.get("entries"):
                target_errors = inputs_module.validate_against_spec(local_inputs_doc, current_spec)
                if target_errors:
                    stages["import"] = "local_inputs_invalid"
                    report = _failure_report(
                        "local_inputs_invalid", stages, "; ".join(target_errors),
                        {"share_id": share_id, "inputs_path": str(inputs_path)},
                    )
                    path = write_failure_report(paths, share_id, attempt_id, report)
                    print(f"error: local inputs do not match the build: {'; '.join(target_errors)}", file=sys.stderr)
                    return EXIT_FAIL
            local_inputs_meta = {
                "version": local_inputs_doc.get("version"),
                "path": str(inputs_path.relative_to(root)),
                "sha256": inputs_module.document_sha256(local_inputs_doc),
                "entries": len(local_inputs_doc.get("entries") or []),
            }
            input_index = inputs_module.InputIndex(local_inputs_doc)
            stages["import"] = "local_inputs_loaded"
        builder = BuildSpecBuilder(
            catalog, Resolver(catalog), restorer=restorer, input_index=input_index
        )
        try:
            spec = builder.build(
                draft=draft,
                fetch_meta=fetch_meta,
                catalog_compatibility={
                    "status": version_compatibility["status"],
                    "raw": version_compatibility["raw"],
                    "normalized": version_compatibility["normalized"],
                    "problems": version_compatibility["problems"],
                    "warnings": version_compatibility["warnings"],
                    "target_game_version": version_compatibility["target_game_version"],
                },
                previous_revision=None,
            )
        except SchemaError as exc:
            stages["schema"] = "invalid"
            report = _failure_report(
                "schema_invalid",
                stages,
                str(exc),
                {"share_id": share_id, "details": exc.details, "import": fetch_meta},
            )
            path = write_failure_report(paths, share_id, attempt_id, report)
            print(f"error: unsupported source payload shape; report: {path}", file=sys.stderr)
            return EXIT_FAIL
        if input_index is not None:
            unapplied = input_index.unapplied()
            if unapplied:
                stages["import"] = "local_inputs_unapplied"
                report = _failure_report(
                    "local_inputs_unapplied",
                    stages,
                    "local input entries could not be applied to this payload",
                    {
                        "share_id": share_id,
                        "inputs_path": local_inputs_meta.get("path") if local_inputs_meta else None,
                        "unapplied": unapplied,
                    },
                )
                path = write_failure_report(paths, share_id, attempt_id, report)
                print(f"error: local input entries could not be applied: {len(unapplied)}; report: {path}", file=sys.stderr)
                return EXIT_FAIL
            local_inputs_meta["applied"] = len(input_index.entries)
            local_inputs_meta["inputs_manifest"] = input_index.manifest()
            spec["meta"]["local_inputs"] = local_inputs_meta
        stages["schema"] = "supported"
        spec["meta"]["import_attempt"] = {
            "attempt_id": attempt_id,
            "mode": fetch.get("mode"),
            "http_status": fetch.get("http_status"),
            "fetched_at": fetch.get("fetched_at"),
            "requested_url": args.url or str(requested_file),
        }

        validation = validate_spec(spec, catalog)
        coverage_result = coverage(spec)
        acceptance = acceptance_checks(spec)
        gap_report = build_gap_report(spec, catalog)
        spec["coverage"] = coverage_result
        spec["value_resolution_notes"] = {
            "module": "le_build.rolls" if restorer else None,
            "codec_rule_version": getattr(restorer, "CODE_RULE_VERSION", None) if restorer else None,
        }
        # Pre-compute revision identity so the reports can name the revision they describe.
        # meta.revision is excluded from the content hash, so this does not change it.
        from . import store as store_module

        predicted_revision = store_module.revision_id_for(spec)
        existing_current = store_module.load_current(paths, share_id)
        existing_seq = None
        existing_dir = paths.revision_dir(share_id, predicted_revision)
        if existing_dir.is_dir():
            try:
                existing_spec = json.loads((existing_dir / "build.json").read_text(encoding="utf-8"))
                existing_seq = ((existing_spec.get("meta") or {}).get("revision") or {}).get("revision_seq")
            except (OSError, json.JSONDecodeError):  # pragma: no cover - defensive
                existing_seq = None
        predicted_seq = existing_seq or (
            (int(existing_current.get("revision_seq")) + 1) if existing_current and existing_current.get("revision_seq") else 1
        )
        spec.setdefault("meta", {})["revision"] = {
            "revision_id": predicted_revision,
            "content_sha256": store_module.content_hash(spec),
            "revision_seq": predicted_seq,
            "published_at": None,
        }
        import_report = build_import_report(
            spec,
            import_info={"mode": fetch.get("mode"), "http_status": fetch.get("http_status")},
            validation=validation,
            coverage=coverage_result,
            acceptance=acceptance,
        )
        import_report["value_resolution"] = {
            "status": spec["statuses"].get("value_resolution"),
            "coverage": coverage_result.get("values"),
            "notes": spec.get("value_resolution_notes"),
        }
        import_markdown = render_import_report_markdown(import_report, spec, gap_report)
        gap_markdown = render_gap_report_markdown(gap_report)
        artifacts = {
            "import-report.json": import_report,
            "import-report.zh-CN.md": import_markdown,
            "calculation-gap-report.json": gap_report,
            "calculation-gap-report.zh-CN.md": gap_markdown,
        }
        if args.dry_run:
            stages["import"] = "dry_run"
            _print_json(
                {
                    "dry_run": True,
                    "statuses": spec.get("statuses"),
                    "validation": validation["status"],
                    "acceptance_failed": [check for check in acceptance if check["status"] != "pass"],
                    "counts": import_report.get("counts"),
                    "revision_preview": {
                        "content_sha256": __import__("le_build.store", fromlist=["store"]).content_hash(spec)
                    },
                }
            )
            return EXIT_OK

        publish = publish_revision(paths, share_id, spec, artifacts=artifacts)
        stages["import"] = publish["state"]
        # Human-friendly mirror outside the immutable revision (regenerable copies).
        # Re-render with the actual published revision metadata (published_at/seq) so the
        # mirror describes the stored revision exactly.
        published_revision = dict(spec.get("meta", {}).get("revision") or {})
        published_revision.update(
            {
                "revision_id": publish["revision_id"],
                "revision_seq": publish.get("revision_seq"),
                "content_sha256": publish.get("content_sha256"),
                "published_at": publish.get("published_at"),
            }
        )
        import_report["revision"] = published_revision
        import_markdown = render_import_report_markdown(import_report, spec, gap_report)
        mirror = paths.reports_dir() / share_id
        write_json(mirror / "import-report.json", import_report)
        write_json(mirror / "calculation-gap-report.json", gap_report)
        (mirror / "import-report.zh-CN.md").write_text(import_markdown, encoding="utf-8", newline="\n")
        (mirror / "calculation-gap-report.zh-CN.md").write_text(gap_markdown, encoding="utf-8", newline="\n")
        summary = {
            "build_id": spec["meta"]["build_id"],
            "revision_id": publish["revision_id"],
            "revision_seq": publish.get("revision_seq"),
            "state": publish["state"],
            "statuses": spec["statuses"],
            "counts": import_report.get("counts"),
            "validation": validation["status"],
            "acceptance_failed": [
                check["check"] for check in acceptance if check["status"] != "pass"
            ],
            "revision_dir": str(paths.revision_dir(share_id, publish["revision_id"]).relative_to(root)),
            "report_mirror": str(mirror.relative_to(root)),
            "commands": import_report.get("commands"),
        }
        _print_json(summary)
        if validation["status"] == "fail" or spec["statuses"].get("schema") == "invalid":
            return EXIT_FAIL
        if (
            validation["status"] == "warn"
            or spec["statuses"].get("identity_resolution") != "resolved"
            or [check for check in acceptance if check["status"] != "pass"]
        ):
            return EXIT_PARTIAL
        return EXIT_OK
    except CatalogError as exc:
        print(f"error: catalog: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except letools_source.FetchError as exc:
        report = _failure_report(
            "fetch_failed",
            {**stages, "fetch": exc.code},
            str(exc),
            {"share_id": share_id, "fetch": exc.fetch},
        )
        try:
            path = write_failure_report(paths, share_id, attempt_id or "fetch-" + share_id, report)
            print(f"error: fetch failed ({exc.code}); report: {path}", file=sys.stderr)
        except OSError:  # pragma: no cover - defensive
            print(f"error: fetch failed ({exc.code}): {exc}", file=sys.stderr)
        return EXIT_FAIL
    except IncompatibleBuildError as exc:
        print(f"error: incompatible build: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except (StoreError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except Exception:  # pragma: no cover - defensive: never leave a half state silently
        traceback.print_exc()
        return EXIT_FAIL
    finally:
        if catalog is not None:
            catalog.close()


# ----------------------------------------------------------- local input layer
def _build_context(args):
    root = _root_from(args)
    paths = BuildPaths(root)
    source, share_id = build_ref_parts(args.build)
    paths = BuildPaths(root, source=source)
    try:
        loaded = load_build(paths, args.build)
    except StoreError as exc:
        raise inputs_module.InputError(str(exc)) from exc
    return root, paths, share_id, loaded


def _inputs_file_for(paths: BuildPaths, share_id: str) -> Path:
    return paths.inputs_file(share_id)


def _load_or_empty_inputs(paths: BuildPaths, share_id: str, build_id: str) -> dict:
    path = _inputs_file_for(paths, share_id)
    document = inputs_module.load_document(path, build_id=build_id) if path.is_file() else None
    return document or inputs_module.empty_document(build_id)


def _input_entry_from_args(args) -> dict:
    entry = {
        "kind": args.kind,
        "instance_path": args.instance,
    }
    if args.roll is not None:
        entry["roll"] = args.roll
    if args.roll_id is not None:
        entry["roll_id"] = args.roll_id
    if args.index is not None:
        entry["index"] = args.index
    if args.target is not None:
        entry["target"] = args.target
    if args.affix_value_index is not None:
        entry["affix_value_index"] = args.affix_value_index
    if args.mod_index is not None:
        entry["mod_index"] = args.mod_index
    if args.value is not None:
        entry["value_numeric"] = args.value
    if args.note:
        entry["note"] = args.note
    return entry


def _explicit_display_meta(spec: dict, entry: dict) -> dict | None:
    """Resolve display metadata (unit/percent/range) for an explicit_value target."""
    index = inputs_module.build_target_index(spec)
    target = entry.get("target")
    path = entry.get("instance_path")
    if target == "affix":
        info = index["explicit_value"].get(("affix", path))
        if not info:
            return None
        value_index = int(entry.get("affix_value_index", 0))
        values = info.get("values") or []
        return values[value_index] if value_index < len(values) else None
    if target == "unique_mod":
        if entry.get("mod_index") is not None:
            return index["explicit_value"].get(("unique_mod", path, int(entry["mod_index"])))
        roll_id = entry.get("roll_id")
        for (kind, item_path, mod_index), info in index["explicit_value"].items():
            if kind == "unique_mod" and item_path == path and info.get("roll_id") == roll_id:
                return info
        return None
    if target == "implicit":
        return index["explicit_value"].get(("implicit", path, int(entry.get("index"))))
    return None


def _apply_display_value(entry: dict, display_value: float, unit: str, spec: dict) -> dict:
    """Convert a typed displayed value (e.g. 35 or 2) to normalized ``value_numeric``."""
    if entry.get("kind") != "explicit_value":
        raise inputs_module.InputError("--display-value is only valid with --kind explicit_value")
    if entry.get("value_numeric") is not None:
        raise inputs_module.InputError("use either --value (normalized) or --display-value, not both")
    meta = _explicit_display_meta(spec, entry)
    if meta is None:
        raise inputs_module.InputError(
            "cannot resolve the explicit_value target; check --target/--instance/--mod-index/--affix-value-index"
        )
    percent = unit == "percent" or (unit == "auto" and bool(meta.get("percent")))
    normalized = display_value / 100.0 if percent else display_value
    entry["value_numeric"] = normalized
    entry["display_value"] = display_value
    entry["display_unit"] = "percent" if percent else "flat"
    return entry


def _print_input_targets(build_id: str, spec: dict, document: dict) -> None:
    targets = inputs_module.describe_targets(spec)
    existing = {inputs_module.target_key(entry) for entry in document.get("entries") or []}
    pending = [target for target in targets if inputs_module.target_key(target) not in existing]
    _print_json(
        {
            "build_id": build_id,
            "unresolved_targets_total": len(targets),
            "pending_targets": len(pending),
            "entries": len(document.get("entries") or []),
            "targets": targets,
        }
    )


def cmd_input(args) -> int:
    root = _root_from(args)
    try:
        root, paths, share_id, loaded = _build_context(args)
        spec = loaded["spec"]
        build_id = spec.get("meta", {}).get("build_id") or f"letools:{share_id}"
        inputs_path = _inputs_file_for(paths, share_id)
        action = args.input_command
        if action == "set":
            document = _load_or_empty_inputs(paths, share_id, build_id)
            entry = _input_entry_from_args(args)
            if getattr(args, "display_value", None) is not None:
                entry = _apply_display_value(entry, args.display_value, args.unit, spec)
            document = inputs_module.add_entry(document, entry)
            new_entry = document["entries"][-1]
            errors = inputs_module.validate_against_spec(
                {"version": inputs_module.INPUTS_VERSION, "build_id": build_id, "entries": [new_entry]}, spec
            )
            if errors:
                raise inputs_module.InputError("; ".join(errors))
            digest = inputs_module.save_document(inputs_path, document)
            summary = {
                "input_id": new_entry.get("input_id"),
                "kind": new_entry.get("kind"),
                "instance_path": new_entry.get("instance_path"),
                "inputs_file": str(inputs_path.relative_to(root)),
                "inputs_sha256": digest,
                "entries": len(document["entries"]),
                "pending_targets": len(
                    [
                        target
                        for target in inputs_module.describe_targets(spec)
                        if inputs_module.target_key(target)
                        not in {inputs_module.target_key(item) for item in document["entries"]}
                    ]
                ),
            }
            _print_json(summary)
            if args.sync:
                sync_args = argparse.Namespace(
                    build=args.build,
                    dry_run=False,
                    root=getattr(args, "root", None),
                    dataset=getattr(args, "dataset", None),
                    db=getattr(args, "db", None),
                )
                return cmd_sync(sync_args)
            return EXIT_OK
        if action == "list":
            document = _load_or_empty_inputs(paths, share_id, build_id)
            if args.json:
                _print_input_targets(build_id, spec, document)
            else:
                targets = inputs_module.describe_targets(spec)
                existing = {inputs_module.target_key(entry) for entry in document["entries"]}
                pending = [target for target in targets if inputs_module.target_key(target) not in existing]
                print(f"build: {build_id}")
                print(f"inputs file: {inputs_path} ({len(document['entries'])} entries)")
                for entry in document["entries"]:
                    print(
                        f"  [{entry.get('input_id')}] {entry.get('kind')} "
                        f"{entry.get('instance_path')} "
                        f"{'roll=' + str(entry.get('roll')) if entry.get('roll') is not None else ''}"
                        f"{' rollId=' + str(entry.get('roll_id')) if entry.get('roll_id') is not None else ''}"
                        f"{' index=' + str(entry.get('index')) if entry.get('index') is not None else ''}"
                        f"{' value=' + str(entry.get('value_numeric')) if entry.get('value_numeric') is not None else ''}"
                    )
                total_variables = sum(len(target.get("variables") or []) for target in targets)
                pending_variables = sum(len(target.get("variables") or []) for target in pending)
                print(
                    f"pending: {len(pending)}/{len(targets)} target group(s), "
                    f"{pending_variables}/{total_variables} variable row(s)"
                )
                for target in pending:
                    roll_note = (
                        f" [rollId={target.get('roll_id')}]"
                        if target.get("roll_id") is not None
                        else ""
                    )
                    print(
                        f"  - {target.get('kind')} {target.get('instance_path')} "
                        f"{target.get('label')}{roll_note}"
                    )
                    for variable in target.get("variables") or []:
                        locator = (
                            f"mod_index={variable.get('mod_index')}"
                            if variable.get("mod_index") is not None
                            else f"value_index={variable.get('affix_value_index', variable.get('index'))}"
                        )
                        print(
                            f"      * {locator} {variable.get('unit')} "
                            f"{variable.get('range_text') or ''} {variable.get('label') or ''}"
                        )
            return EXIT_OK
        if action == "remove":
            document = _load_or_empty_inputs(paths, share_id, build_id)
            document, removed = inputs_module.remove_entry(document, args.input_id)
            digest = inputs_module.save_document(inputs_path, document)
            _print_json(
                {
                    "removed": removed,
                    "input_id": args.input_id,
                    "entries": len(document["entries"]),
                    "inputs_sha256": digest,
                }
            )
            return EXIT_OK if removed else EXIT_FAIL
        if action == "import":
            incoming = inputs_module.load_document(args.file)
            if not incoming:
                raise inputs_module.InputError(f"no entries found in {args.file}")
            if incoming.get("build_id") and incoming.get("build_id") != build_id:
                raise inputs_module.InputError(
                    f"inputs file build_id {incoming.get('build_id')!r} does not match {build_id!r}"
                )
            incoming["build_id"] = build_id
            if args.replace:
                document = incoming
            else:
                document = _load_or_empty_inputs(paths, share_id, build_id)
                document = inputs_module.merge_documents(document, incoming)
            errors = inputs_module.validate_against_spec(document, spec)
            if errors:
                raise inputs_module.InputError("; ".join(errors))
            digest = inputs_module.save_document(inputs_path, document)
            _print_json(
                {
                    "inputs_file": str(inputs_path.relative_to(root)),
                    "inputs_sha256": digest,
                    "entries": len(document["entries"]),
                    "replaced": bool(args.replace),
                }
            )
            return EXIT_OK
        if action == "export":
            document = _load_or_empty_inputs(paths, share_id, build_id)
            if args.out:
                out = Path(args.out)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(
                    json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                print(json.dumps({"written": str(out), "entries": len(document["entries"])}, ensure_ascii=False))
            else:
                _print_json(document)
            return EXIT_OK
        print(f"error: unknown input action {action!r}", file=sys.stderr)
        return EXIT_ARGS
    except inputs_module.InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAIL


def cmd_sync(args) -> int:
    root = _root_from(args)
    paths = BuildPaths(root)
    try:
        loaded = load_build(paths, args.build)
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAIL
    spec = loaded["spec"]
    meta = spec.get("meta") or {}
    raw_relative = (meta.get("source_payload") or {}).get("raw_path")
    if not raw_relative:
        print("error: current revision has no archived raw payload path; cannot sync offline", file=sys.stderr)
        return EXIT_FAIL
    raw_path = paths.root_path(raw_relative)
    if not raw_path.is_file():
        print(f"error: archived raw payload not found: {raw_path}", file=sys.stderr)
        return EXIT_FAIL
    share_id = meta.get("share_id")
    if not share_id:
        print("error: current revision has no share_id", file=sys.stderr)
        return EXIT_FAIL
    import_args = argparse.Namespace(
        url=None,
        file=str(raw_path),
        share_id=share_id,
        offline=True,
        dry_run=bool(getattr(args, "dry_run", False)),
        ignore_local_inputs=bool(getattr(args, "ignore_local_inputs", False)),
        dataset=getattr(args, "dataset", None),
        db=getattr(args, "db", None),
        timeout=30.0,
        retries=0,
        max_bytes=2 * 1024 * 1024,
        root=getattr(args, "root", None),
    )
    return cmd_import(import_args)


def cmd_export(args) -> int:
    root = _root_from(args)
    paths = BuildPaths(root)
    try:
        loaded = load_build(paths, args.build, revision_id=getattr(args, "revision", None))
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAIL
    try:
        from .calculation import build_calculation_input
    except ImportError:
        print("error: calculation export module (le_build.calculation) is not available", file=sys.stderr)
        return EXIT_FAIL
    try:
        document = build_calculation_input(
            loaded["spec"], generated_at=getattr(args, "generated_at", None)
        )
    except TypeError:
        document = build_calculation_input(loaded["spec"])
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        print(json.dumps({"written": str(out), "size": out.stat().st_size}, ensure_ascii=False))
    else:
        _print_json(document)
    return EXIT_OK


# ---------------------------------------------------------------- read commands
def cmd_show(args) -> int:
    root = _root_from(args)
    paths = BuildPaths(root)
    try:
        loaded = load_build(paths, args.build, revision_id=args.revision)
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAIL
    spec = loaded["spec"]
    if args.json:
        _print_json({"storage": {key: value for key, value in loaded.items() if key != "spec"}, "spec": spec})
        return EXIT_OK
    summary = _summary_of_spec(spec)
    summary["storage"] = {
        "revision_id": loaded["revision_id"],
        "revision_dir": str(loaded["revision_dir"]),
        "is_current": loaded["is_current"],
    }
    _print_json(summary)
    return EXIT_OK


def cmd_explain(args) -> int:
    root = _root_from(args)
    paths = BuildPaths(root)
    try:
        loaded = load_build(paths, args.build, revision_id=getattr(args, "revision", None))
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAIL
    result = explain(loaded["spec"], args.path)
    if not result.get("found"):
        print(f"error: {result.get('reason')}", file=sys.stderr)
        return EXIT_FAIL
    if args.json:
        _print_json(result)
    else:
        _print_json({key: value for key, value in result.items() if key != "value"})
        print("--- value ---")
        _print_json(result.get("value"))
    return EXIT_OK


def cmd_validate(args) -> int:
    root = _root_from(args)
    paths = BuildPaths(root)
    try:
        loaded = load_build(paths, args.build, revision_id=getattr(args, "revision", None))
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAIL
    spec = loaded["spec"]
    catalog = None
    try:
        if args.offline:
            catalog = _catalog_for(args, root)
        validation = validate_spec(spec, catalog)
        coverage_result = coverage(spec)
        acceptance = acceptance_checks(spec)
        result = {
            "build_id": (spec.get("meta") or {}).get("build_id"),
            "revision_id": loaded["revision_id"],
            "is_current": loaded["is_current"],
            "catalog_bound": (spec.get("catalog") or {}).get("dataset_id"),
            "catalog_verified": (spec.get("catalog") or {}).get("verification", {}).get("verified"),
            "validation": validation,
            "coverage": coverage_result,
            "acceptance_failed": [check for check in acceptance if check["status"] != "pass"],
            "statuses": spec.get("statuses"),
        }
        if args.json:
            _print_json(result)
        else:
            print(f"build: {result['build_id']}  revision: {result['revision_id']}")
            print(f"validation: {validation['status']}  pass={validation['pass']} warn={validation['warn']} fail={validation['fail']}")
            for check in validation["checks"]:
                if check["status"] != "pass":
                    print(f"  [{check['status']}] {check['check']}: {check['message']}")
            if result["acceptance_failed"]:
                print("acceptance failures:")
                for check in result["acceptance_failed"]:
                    print(f"  [fail] {check['label']}: observed={check['observed']}")
            print(f"coverage: items={coverage_result['item_instances']} affixes={coverage_result['affix_instances']}")
        if validation["status"] == "fail":
            return EXIT_FAIL
        if validation["status"] == "warn" or result["acceptance_failed"]:
            return EXIT_PARTIAL
        return EXIT_OK
    except CatalogError as exc:
        print(f"error: catalog: {exc}", file=sys.stderr)
        return EXIT_FAIL
    finally:
        if catalog is not None:
            catalog.close()


def cmd_view(args) -> int:
    root = _root_from(args)
    paths = BuildPaths(root)
    try:
        loaded = load_build(paths, args.build, revision_id=getattr(args, "revision", None))
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAIL
    try:
        from .viewer import render_build_html
    except ImportError:
        print("error: HTML viewer module (le_build.viewer) is not available", file=sys.stderr)
        return EXIT_FAIL
    output = Path(args.out) if args.out else (
        paths.reports_dir() / (loaded["spec"].get("meta", {}).get("share_id") or "build") / "build-view.html"
    )
    html = render_build_html(loaded["spec"], revision_id=loaded["revision_id"], build_dir=loaded["revision_dir"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8", newline="\n")
    print(json.dumps({"written": str(output), "build_id": loaded["spec"]["meta"]["build_id"]}, ensure_ascii=False))
    return EXIT_OK


# ------------------------------------------------------------------------ main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="le_build",
        description="Last Epoch phase-2 build importer (LE Tools share link -> offline BuildSpec v1)",
    )
    parser.add_argument("--version", action="version", version=f"le-build {__version__}")
    parser.add_argument("--root", help="project root (defaults to the repository containing data/current.json)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="fetch/read a build payload and publish an immutable BuildSpec revision")
    source_group = import_parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--url", help="LE Tools planner share URL")
    source_group.add_argument("--file", help="archived raw build_data JSON (offline replay)")
    import_parser.add_argument("--share-id", help="share id for --file imports when it cannot be inferred")
    import_parser.add_argument("--offline", action="store_true", help="offline: only archived payloads, no network")
    import_parser.add_argument("--dataset", help="catalog dataset id (default: data/current.json)")
    import_parser.add_argument("--db", help="explicit catalog SQLite path (read-only)")
    import_parser.add_argument("--dry-run", action="store_true", help="build and validate without publishing")
    import_parser.add_argument(
        "--ignore-local-inputs",
        action="store_true",
        help="ignore data/builds/<source>/<share>/inputs/local-inputs.json for this import",
    )
    import_parser.add_argument("--timeout", type=float, default=letools_source.DEFAULT_TIMEOUT)
    import_parser.add_argument("--retries", type=int, default=letools_source.DEFAULT_RETRIES)
    import_parser.add_argument("--max-bytes", type=int, default=letools_source.DEFAULT_MAX_BYTES)
    import_parser.set_defaults(func=cmd_import)

    show_parser = subparsers.add_parser("show", help="show the stored build summary")
    show_parser.add_argument("--build", required=True, help="build id, e.g. letools:o3ZRm9my")
    show_parser.add_argument("--revision", help="revision id (default: current)")
    show_parser.add_argument("--json", action="store_true")
    show_parser.set_defaults(func=cmd_show)

    explain_parser = subparsers.add_parser("explain", help="explain one path with provenance and issues")
    explain_parser.add_argument("--build", required=True)
    explain_parser.add_argument("--path", required=True, help="e.g. equipment.hands / specializations.0.nodes.25")
    explain_parser.add_argument("--revision")
    explain_parser.add_argument("--json", action="store_true")
    explain_parser.set_defaults(func=cmd_explain)

    validate_parser = subparsers.add_parser("validate", help="offline re-validate a stored build")
    validate_parser.add_argument("--build", required=True)
    validate_parser.add_argument("--revision")
    validate_parser.add_argument("--offline", action="store_true", help="also verify catalog binding against the local dataset")
    validate_parser.add_argument("--dataset")
    validate_parser.add_argument("--db")
    validate_parser.add_argument("--json", action="store_true")
    validate_parser.set_defaults(func=cmd_validate)

    input_parser = subparsers.add_parser(
        "input", help="manage local value inputs for missing roll slots (stored outside revisions)"
    )
    input_sub = input_parser.add_subparsers(dest="input_command", required=True)

    input_set = input_sub.add_parser("set", help="add/change one local input entry")
    input_set.add_argument("--build", required=True)
    input_set.add_argument("--kind", required=True, choices=list(inputs_module.INPUT_KINDS))
    input_set.add_argument("--instance", required=True, help="BuildSpec source_path target")
    input_set.add_argument("--roll", type=int, help="raw roll quality 0..255 (r=0 is valid)")
    input_set.add_argument("--roll-id", type=int, dest="roll_id", help="unique mod rollId")
    input_set.add_argument("--index", type=int, help="implicit index")
    input_set.add_argument("--target", choices=["affix", "unique_mod", "implicit"], help="explicit_value target")
    input_set.add_argument("--affix-value-index", type=int, dest="affix_value_index")
    input_set.add_argument("--mod-index", type=int, dest="mod_index")
    input_set.add_argument("--value", type=float, help="explicit normalized value (explicit_value kind)")
    input_set.add_argument(
        "--display-value",
        type=float,
        dest="display_value",
        help="explicit value as displayed in game (e.g. 35 for 35%%, 2 for +2 skills); converted per target unit",
    )
    input_set.add_argument(
        "--unit",
        choices=["auto", "percent", "flat"],
        default="auto",
        help="unit for --display-value (default: infer from the target stat/display text)",
    )
    input_set.add_argument("--note", help="free-form note kept in provenance")
    input_set.add_argument("--sync", action="store_true", help="publish a new revision immediately after saving")
    input_set.add_argument("--dataset", help="catalog dataset id override for --sync")
    input_set.add_argument("--db", help="explicit catalog SQLite path for --sync (temp roots)")
    input_set.set_defaults(func=cmd_input)

    input_list = input_sub.add_parser("list", help="list saved inputs and pending unresolved targets")
    input_list.add_argument("--build", required=True)
    input_list.add_argument("--json", action="store_true")
    input_list.set_defaults(func=cmd_input)

    input_remove = input_sub.add_parser("remove", help="remove one input entry by id")
    input_remove.add_argument("--build", required=True)
    input_remove.add_argument("--input-id", required=True)
    input_remove.set_defaults(func=cmd_input)

    input_import = input_sub.add_parser("import", help="merge/import a build-local-inputs-v1 JSON file")
    input_import.add_argument("--build", required=True)
    input_import.add_argument("--file", required=True)
    input_import.add_argument("--replace", action="store_true", help="replace the whole local inputs file")
    input_import.set_defaults(func=cmd_input)

    input_export = input_sub.add_parser("export", help="write the current local inputs file")
    input_export.add_argument("--build", required=True)
    input_export.add_argument("--out")
    input_export.set_defaults(func=cmd_input)

    sync_parser = subparsers.add_parser(
        "sync", help="re-import the archived payload with local inputs and publish a new revision"
    )
    sync_parser.add_argument("--build", required=True)
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--ignore-local-inputs", action="store_true")
    sync_parser.add_argument("--dataset", help="catalog dataset id override (default: data/current.json)")
    sync_parser.add_argument("--db", help="explicit catalog SQLite path (useful with --root temp dirs)")
    sync_parser.set_defaults(func=cmd_sync)

    export_parser = subparsers.add_parser(
        "export", help="export calculation-input-v1 JSON for the calculator work"
    )
    export_parser.add_argument("--build", required=True)
    export_parser.add_argument("--revision")
    export_parser.add_argument("--out", help="output JSON path (stdout when omitted)")
    export_parser.add_argument("--generated-at", dest="generated_at", help="fixed timestamp for deterministic tests")
    export_parser.set_defaults(func=cmd_export)

    view_parser = subparsers.add_parser("view", help="render a standalone offline HTML visualisation")
    view_parser.add_argument("--build", required=True)
    view_parser.add_argument("--revision")
    view_parser.add_argument("--out", help="output HTML path")
    view_parser.set_defaults(func=cmd_view)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("interrupted", file=sys.stderr)
        return EXIT_FAIL
