"""Offline validation, coverage accounting and explain support for BuildSpec v1."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .catalog import Catalog

REQUIRED_SECTIONS = (
    "meta",
    "catalog",
    "scope",
    "character",
    "passive_tree",
    "specializations",
    "action_bar",
    "equipment",
    "idols",
    "blessings",
    "context",
    "progression",
    "readable_summary",
    "extensions",
    "issues",
    "statuses",
)

VALID_STATUS_VALUES = {
    "fetch": {"ok", "failed"},
    "schema": {"supported", "partial", "invalid"},
    "catalog_compatibility": {"matched", "unknown", "mismatched"},
    "identity_resolution": {"resolved", "partial", "failed"},
    "value_resolution": {"resolved", "partial", "not_run"},
    "calculation_readiness": {"not_assessed", "partial", "ready_for_scoped_engine"},
}
VALID_TEXT_READINESS = {"complete", "partial", "unavailable"}


# ---------------------------------------------------------------------------
# Minimal JSON-Schema subset validator (stdlib only) for schemas/build-spec-v1.json
# ---------------------------------------------------------------------------
_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def _resolve_ref(root: dict, ref: str) -> dict:
    if not ref.startswith("#/"):
        raise ValueError(f"only local $ref supported, got {ref!r}")
    node = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[part]
    return node


def _validate_schema_node(value, schema: dict, root: dict, path: str, errors: list[str]) -> None:
    if "$ref" in schema:
        _validate_schema_node(value, _resolve_ref(root, schema["$ref"]), root, path, errors)
        return
    if "anyOf" in schema:
        branch_errors: list[list[str]] = []
        for subschema in schema["anyOf"]:
            local: list[str] = []
            _validate_schema_node(value, subschema, root, path, local)
            if not local:
                return
            branch_errors.append(local)
        errors.append(f"{path}: value does not match any allowed schema branch: {branch_errors[0][:3]}")
        return
    if "oneOf" in schema:
        matching = 0
        for subschema in schema["oneOf"]:
            local: list[str] = []
            _validate_schema_node(value, subschema, root, path, local)
            if not local:
                matching += 1
        if matching != 1:
            errors.append(f"{path}: expected exactly one matching schema branch, got {matching}")
        return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']!r}")
    expected_types = schema.get("type")
    if expected_types:
        types = expected_types if isinstance(expected_types, list) else [expected_types]
        if not any(_TYPE_CHECKS.get(type_name, lambda _: True)(value) for type_name in types):
            errors.append(f"{path}: expected type {types!r}, got {type(value).__name__}")
            return
    if isinstance(value, dict):
        for required in schema.get("required", []):
            if required not in value:
                errors.append(f"{path}: missing required property {required!r}")
        properties = schema.get("properties") or {}
        for key, subschema in properties.items():
            if key in value:
                _validate_schema_node(value[key], subschema, root, f"{path}.{key}", errors)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _validate_schema_node(item, schema["items"], root, f"{path}[{index}]", errors)
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        errors.append(f"{path}: {value!r} does not match pattern {schema['pattern']!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} is below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} is above maximum {schema['maximum']}")


def schema_violations(spec: dict, schema_path: Path | str | None = None, *, legacy: bool = False) -> list[str]:
    """Validate the BuildSpec against ``schemas/build-spec-v1.json`` (subset validator).

    Historical revisions (``legacy=True``) predate the 2026-11-03 scope/readable fields,
    so those two required sections are not enforced; everything else still applies.
    """
    if schema_path is None:
        schema_path = Path(__file__).resolve().parents[2] / "schemas" / "build-spec-v1.json"
    schema_path = Path(schema_path)
    if not schema_path.is_file():
        return []
    with open(schema_path, "r", encoding="utf-8") as handle:
        schema = json.load(handle)
    if legacy:
        schema = dict(schema)
        schema["required"] = [
            key for key in schema.get("required", []) if key not in ("scope", "readable_summary")
        ]
    errors: list[str] = []
    _validate_schema_node(spec, schema, schema, "$", errors)
    return errors


def _iter_item_instances(spec: dict):
    for slot in spec.get("equipment", {}).get("slots", []):
        yield f"equipment.{slot.get('slot')}", slot.get("instance") or {}
    for entry in spec.get("idols", {}).get("entries", []):
        yield f"idols.{entry.get('index')}", entry.get("instance") or {}
    for entry in spec.get("blessings", {}).get("entries", []):
        yield f"blessings.{entry.get('timeline')}", entry.get("instance") or {}


def _iter_affix_instances(spec: dict):
    for path, instance in _iter_item_instances(spec):
        for channel, entries in (instance.get("affixes") or {}).items():
            for entry in entries or []:
                yield f"{path}.{channel}.{entry.get('index')}", entry


def _iter_nodes(spec: dict):
    for node in spec.get("passive_tree", {}).get("nodes", []):
        yield "passive_tree", node
    for spec_index, specialization in enumerate(spec.get("specializations", [])):
        for node in specialization.get("nodes", []):
            yield f"specializations.{spec_index}", node
    for node in spec.get("weaver", {}).get("nodes", []):
        yield "weaver", node


def coverage(spec: dict) -> dict:
    """Per-category coverage with each category keeping its own denominator."""
    item_instances = list(_iter_item_instances(spec))
    item_total = len(item_instances)
    item_resolved = sum(1 for _, instance in item_instances if instance.get("status") == "resolved")
    item_identity = sum(
        1
        for _, instance in item_instances
        if (instance.get("base") or {}).get("ref")
        and ((instance.get("definition") or {}).get("ref") or instance.get("codec_class") == "base")
    )
    base_resolved = sum(1 for _, instance in item_instances if (instance.get("base") or {}).get("ref"))
    definition_resolved = sum(
        1 for _, instance in item_instances if (instance.get("definition") or {}).get("ref")
    )
    unique_instances = [instance for _, instance in item_instances if instance.get("codec_class") == "unique"]

    affixes = list(_iter_affix_instances(spec))
    affix_total = len(affixes)
    affix_resolved = sum(1 for _, entry in affixes if (entry.get("affix") or {}).get("status") == "resolved")
    tier_resolved = sum(1 for _, entry in affixes if (entry.get("tier") or {}).get("status") == "resolved")
    roll_present = sum(1 for _, entry in affixes if (entry.get("roll") or {}).get("status") == "present")
    roll_absent = sum(1 for _, entry in affixes if (entry.get("roll") or {}).get("status") == "absent")
    roll_null = sum(1 for _, entry in affixes if (entry.get("roll") or {}).get("status") == "null")
    roll_invalid = sum(1 for _, entry in affixes if (entry.get("roll") or {}).get("status") == "invalid")
    channel_counts: dict = {}
    for _, entry in affixes:
        channel = entry.get("channel") or "unknown"
        channel_counts[channel] = channel_counts.get(channel, 0) + 1

    value_entries = [entry for _, entry in affixes if entry.get("value") is not None]
    value_resolved = sum(1 for entry in value_entries if entry["value"].get("status") == "resolved")
    value_partial = sum(1 for entry in value_entries if entry["value"].get("status") == "partial")
    value_unresolved = sum(
        1 for entry in value_entries if entry["value"].get("status") not in ("resolved", "partial")
    )

    implicit_items = [instance for _, instance in item_instances if instance.get("implicit_rolls")]
    implicit_present = sum(
        1 for instance in implicit_items if instance["implicit_rolls"].get("status") == "present"
    )
    unique_ur_items = [instance for instance in unique_instances if instance.get("unique_rolls")]
    unique_ur_present = sum(
        1 for instance in unique_ur_items if instance["unique_rolls"].get("status") == "present"
    )
    unique_definition_lines = sum(
        1
        for instance in unique_instances
        if (instance.get("readable") or {}).get("unique_composition", {}).get("mods")
        or (instance.get("readable") or {}).get("unique_composition", {}).get("descriptions")
    )

    nodes = list(_iter_nodes(spec))
    node_total = len(nodes)
    node_resolved = sum(1 for _, node in nodes if node.get("status") == "resolved")
    explicit_zero = sum(1 for _, node in nodes if node.get("explicit_zero"))

    readable_items = [
        (path, instance.get("readable") or {}) for path, instance in item_instances
    ]
    completeness_counts: dict = {}
    readable_line_status: dict = {}
    lines_without_text = 0
    for _, block in readable_items:
        completeness = block.get("completeness") or "unavailable"
        completeness_counts[completeness] = completeness_counts.get(completeness, 0) + 1
        for line in block.get("lines") or []:
            status = line.get("value_status") or "unresolved"
            readable_line_status[status] = readable_line_status.get(status, 0) + 1
            if not line.get("text"):
                lines_without_text += 1
    blessing_implicit_items = [
        instance for _, instance in item_instances
        if instance.get("role") == "blessing"
        and (instance.get("implicit_rolls") or {}).get("status") == "present"
    ]
    blessing_implicit_restored = sum(
        1
        for instance in blessing_implicit_items
        if ((instance.get("implicit_rolls") or {}).get("values") or [])
    )
    scope = spec.get("scope") or {}
    excluded_features = [entry.get("feature") for entry in scope.get("excluded_features") or []]

    return {
        "item_instances": {
            "total": item_total,
            "resolved": item_resolved,
            "identity_connected": item_identity,
            "base_resolved": base_resolved,
            "definition_resolved": definition_resolved,
            "unique_instances": len(unique_instances),
        },
        "affix_instances": {
            "total": affix_total,
            "resolved": affix_resolved,
            "tier_resolved": tier_resolved,
            "by_channel": channel_counts,
        },
        "rolls": {
            "present": roll_present,
            "absent": roll_absent,
            "null": roll_null,
            "invalid": roll_invalid,
            "denominator": affix_total,
        },
        "values": {
            "computed": len(value_entries),
            "resolved": value_resolved,
            "partial": value_partial,
            "unresolved": value_unresolved,
            "denominator": affix_total,
            "implicit_items_with_ir": implicit_present,
            "implicit_denominator": len(implicit_items),
            "unique_items_with_ur": unique_ur_present,
            "unique_denominator": len(unique_ur_items),
            "unique_items_with_definition_lines": unique_definition_lines,
            "blessing_implicit_items": len(blessing_implicit_items),
            "blessing_implicit_restored": blessing_implicit_restored,
        },
        "readable": {
            "items": len(readable_items),
            "by_completeness": completeness_counts,
            "line_value_status": readable_line_status,
            "lines_without_text": lines_without_text,
        },
        "nodes": {
            "total": node_total,
            "resolved": node_resolved,
            "explicit_zero": explicit_zero,
            "passive": len(spec.get("passive_tree", {}).get("nodes", [])),
            "skill_tree": sum(len(item.get("nodes", [])) for item in spec.get("specializations", [])),
        },
        "scope": {
            "excluded_features": excluded_features,
        },
        "configuration_references": {
            "specialization_trees": len(spec.get("specializations", [])),
            "action_bar_abilities": len(spec.get("action_bar", {}).get("slots", [])),
            "active_buff_skills": len(spec.get("context", {}).get("active_buffs", {}).get("skills", [])),
            "active_buffs": len(spec.get("context", {}).get("active_buffs", {}).get("buffs", [])),
            "active_ailments": len(spec.get("context", {}).get("active_buffs", {}).get("ailments", [])),
        },
        "unknown_fields": {
            "unexpected_top_level": len(spec.get("extensions", {}).get("unexpected_top_level_fields", [])),
            "unexpected_data": len(spec.get("extensions", {}).get("unexpected_data_fields", [])),
            "uninterpreted_item_fields": sum(
                1 for _, instance in item_instances if instance.get("unrecognized_fields")
            ),
        },
        "issues": {
            "total": len(spec.get("issues", [])),
            "error": sum(1 for issue in spec.get("issues", []) if issue.get("severity") == "error"),
            "warning": sum(1 for issue in spec.get("issues", []) if issue.get("severity") == "warning"),
            "info": sum(1 for issue in spec.get("issues", []) if issue.get("severity") == "info"),
        },
    }


class ValidationResult:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, check: str, status: str, message: str, **detail) -> None:
        entry = {"check": check, "status": status, "message": message}
        if detail:
            entry["detail"] = detail
        self.checks.append(entry)

    def summary(self) -> dict:
        fails = [entry for entry in self.checks if entry["status"] == "fail"]
        warns = [entry for entry in self.checks if entry["status"] == "warn"]
        return {
            "status": "fail" if fails else ("warn" if warns else "ok"),
            "pass": sum(1 for entry in self.checks if entry["status"] == "pass"),
            "warn": len(warns),
            "fail": len(fails),
            "checks": self.checks,
        }


def _check_spec_statuses(spec: dict, result: ValidationResult, *, legacy: bool = False) -> None:
    statuses = spec.get("statuses") or {}
    for key, allowed in VALID_STATUS_VALUES.items():
        if key not in statuses:
            result.add(f"statuses.{key}", "fail", f"statuses.{key} is missing")
            continue
        value = statuses.get(key)
        if value not in allowed:
            result.add(f"statuses.{key}", "fail", f"statuses.{key} has invalid value {value!r}", allowed=sorted(allowed))
        else:
            result.add(f"statuses.{key}", "pass", f"statuses.{key}={value}")
    if legacy:
        result.add(
            "statuses.text_readiness",
            "warn",
            "历史 revision 没有 text_readiness 字段（旧范围）；仅做向后兼容读取",
        )
        return
    text_readiness = statuses.get("text_readiness")
    if text_readiness is None:
        result.add("statuses.text_readiness", "fail", "statuses.text_readiness is missing")
    elif text_readiness not in VALID_TEXT_READINESS:
        result.add(
            "statuses.text_readiness", "fail",
            f"statuses.text_readiness has invalid value {text_readiness!r}",
            allowed=sorted(VALID_TEXT_READINESS),
        )
    else:
        result.add("statuses.text_readiness", "pass", f"statuses.text_readiness={text_readiness}")


def validate_spec(spec: dict, catalog: Catalog | None = None) -> dict:
    """Validate a BuildSpec v1 offline.  Returns a JSON-serialisable result."""
    result = ValidationResult()
    legacy = "readable_summary" not in spec and "scope" not in spec
    result.add("spec.version", "pass" if spec.get("build_spec_version") == "build-spec-v1" else "fail",
               f"build_spec_version={spec.get('build_spec_version')!r}")
    if legacy:
        result.add(
            "spec.legacy_revision",
            "warn",
            "历史 revision（旧范围：含编织者树、无可读词条层）；保持只读兼容，不按新范围重写",
        )
    required_sections = tuple(
        section for section in REQUIRED_SECTIONS if not (legacy and section in ("scope", "readable_summary"))
    )
    missing = [section for section in required_sections if section not in spec]
    result.add("spec.sections", "fail" if missing else "pass",
               "missing sections: " + ", ".join(missing) if missing else "all required sections present",
               missing=missing)
    violations = schema_violations(spec, legacy=legacy)
    result.add(
        "spec.schema_document",
        "fail" if violations else "pass",
        f"{len(violations)} schema violation(s) against schemas/build-spec-v1.json"
        if violations
        else "build.json conforms to schemas/build-spec-v1.json",
        violations=violations[:25],
    )

    meta = spec.get("meta") or {}
    catalog_section = spec.get("catalog") or {}
    if not catalog_section.get("dataset_id"):
        result.add("catalog.dataset_id", "fail", "catalog.dataset_id is missing")
    else:
        result.add("catalog.dataset_id", "pass", f"dataset {catalog_section.get('dataset_id')}")
    verification = catalog_section.get("verification") or {}
    if verification.get("verified") is True:
        result.add("catalog.fingerprint", "pass", "catalog file/logical fingerprints verified")
    else:
        result.add("catalog.fingerprint", "fail", "catalog fingerprints were not verified", detail=verification)
    compatibility = (meta.get("protocol") or {}).get("version_compatibility") or {}
    if compatibility.get("status") == "matched":
        result.add("catalog.version_compatibility", "pass", "source and catalog target versions match")
    else:
        result.add(
            "catalog.version_compatibility",
            "fail" if compatibility.get("status") == "mismatched" else "warn",
            f"version compatibility status is {compatibility.get('status')!r}",
        )
    if not meta.get("source_payload", {}).get("raw_sha256"):
        result.add("meta.source_payload", "fail", "source payload sha256 is missing")
    else:
        result.add("meta.source_payload", "pass", "source payload hash recorded")
    if not spec.get("extensions", {}).get("raw_source", {}).get("raw_sha256"):
        result.add("extensions.raw_source", "fail", "raw source archive reference is missing")

    _check_spec_statuses(spec, result, legacy=legacy)

    character = spec.get("character") or {}
    level = character.get("level")
    result.add("character.level", "pass" if isinstance(level, int) and 1 <= level <= 100 else "fail",
               f"level={level!r}")
    class_ref = (character.get("class") or {}).get("ref") or {}
    mastery_ref = (character.get("mastery") or {}).get("ref") or {}
    result.add("character.class", "pass" if class_ref.get("entity_id") else "fail",
               f"class={class_ref.get('name')!r} status={(character.get('class') or {}).get('status')}")
    result.add("character.mastery", "pass" if mastery_ref.get("entity_id") else "fail",
               f"mastery={mastery_ref.get('name')!r} status={(character.get('mastery') or {}).get('status')}")
    if catalog is not None and class_ref.get("entity_id") and mastery_ref.get("entity_id"):
        mastery_row = catalog.mastery(mastery_ref["entity_id"]) or {}
        consistency = mastery_row.get("class_entity_id") == class_ref["entity_id"]
        result.add(
            "character.mastery_belongs_to_class",
            "pass" if consistency else "fail",
            "mastery row is bound to the selected class" if consistency else "mastery row belongs to another class",
            mastery_class_id=mastery_row.get("class_entity_id"),
        )

    tree = (spec.get("passive_tree") or {}).get("tree") or {}
    result.add("passive_tree.tree", "pass" if tree.get("entity_id") else "fail",
               f"passive tree={tree.get('name')!r}")
    nodes = spec.get("passive_tree", {}).get("nodes", [])
    total = sum(node.get("points", 0) for node in nodes if isinstance(node.get("points"), int))
    if total == spec.get("passive_tree", {}).get("points_total"):
        result.add("passive_tree.points_total", "pass", f"points_total={total}")
    else:
        result.add("passive_tree.points_total", "fail",
                   f"points_total {spec.get('passive_tree', {}).get('points_total')!r} != sum {total}")

    for index, specialization in enumerate(spec.get("specializations", [])):
        tree_ref = specialization.get("tree") or {}
        status = "pass" if tree_ref.get("entity_id") else "fail"
        result.add(f"specializations.{index}.tree", status,
                   f"slot {specialization.get('slot_number')} tree={tree_ref.get('name')!r}")
        points = sum(node.get("points", 0) for node in specialization.get("nodes", []) if isinstance(node.get("points"), int))
        if points != specialization.get("points_total"):
            result.add(f"specializations.{index}.points_total", "fail",
                       f"points_total {specialization.get('points_total')!r} != sum {points}")

    action_bar = spec.get("action_bar", {}).get("slots", [])
    resolved_actions = sum(1 for slot in action_bar if (slot.get("ref") or {}).get("entity_id"))
    result.add("action_bar", "pass" if resolved_actions == len(action_bar) else "warn",
               f"{resolved_actions}/{len(action_bar)} action-bar abilities resolved")
    result.add(
        "action_bar.specializations_independent",
        "pass",
        "action bar is stored separately from the specialization list",
        action_bar_count=len(action_bar),
        specialization_count=len(spec.get("specializations", [])),
    )

    scope = spec.get("scope") or {}
    excluded = [entry.get("feature") for entry in scope.get("excluded_features") or []]
    if "weaver_tree" in excluded:
        result.add("scope.weaver_excluded", "pass",
                   "weaver tree is declared excluded by user scope",
                   source_fields=[entry.get("source_fields") for entry in scope.get("excluded_features") or []])
    else:
        result.add(
            "scope.weaver_excluded",
            "warn" if legacy else "fail",
            "legacy revision has no scope declaration (old scope included the weaver tree)"
            if legacy
            else "scope.excluded_features does not declare weaver_tree",
        )
    weaver_section = spec.get("weaver") or {}
    if weaver_section:
        if weaver_section.get("status") == "excluded_by_scope":
            result.add("scope.weaver_section", "pass", "weaver section is marked excluded_by_scope")
        else:
            result.add(
                "scope.weaver_section",
                "warn" if legacy else "fail",
                f"legacy weaver section status is {weaver_section.get('status')!r} (old scope)"
                if legacy
                else f"weaver section status is {weaver_section.get('status')!r}, expected excluded_by_scope",
            )
    else:
        result.add("scope.weaver_section", "pass", "weaver section omitted (excluded by scope)")
    weaver_issues = [issue for issue in spec.get("issues", []) if "weaver" in (issue.get("code") or "")]
    result.add("scope.weaver_issues", "pass" if not weaver_issues else "warn",
               f"{len(weaver_issues)} weaver-related issues present in an excluded scope",
               codes=sorted({issue["code"] for issue in weaver_issues}))

    items = list(_iter_item_instances(spec))
    paths = [path for path, _ in items]
    result.add("items.unique_instances", "pass" if len(paths) == len(set(paths)) else "fail",
               f"{len(paths)} item instances with {len(set(paths))} unique paths",
               duplicate_paths=[path for path in set(paths) if paths.count(path) > 1])
    missing_readable = [path for path, instance in items if not (instance.get("readable") or {}).get("lines")]
    if legacy:
        result.add("items.readable", "warn",
                   "历史 revision 没有 readable 层；读取方按结构化字段降级显示")
    else:
        result.add("items.readable", "pass" if not missing_readable else "warn",
                   f"{len(items) - len(missing_readable)}/{len(items)} item instances have player-readable lines",
                   missing=missing_readable)
    double_units = []
    for path, instance in items:
        if not instance.get("source_path"):
            result.add(f"items.{path}.source_path", "fail", "item instance has no source_path")
        for channel, entries in (instance.get("affixes") or {}).items():
            for entry in entries or []:
                if "roll" not in entry:
                    result.add(f"affix.{path}.{channel}.{entry.get('index')}.roll", "fail",
                               "affix instance has no roll block")
        for line in (instance.get("readable") or {}).get("lines") or []:
            if "%%" in (line.get("text") or ""):
                double_units.append({"path": path, "text": line.get("text")})
    if legacy:
        result.add("items.readable_units", "warn", "历史 revision 没有 readable 层（跳过文本单位检查）")
    else:
        result.add("items.readable_units", "pass" if not double_units else "fail",
                   "no duplicated percent units in player-readable text" if not double_units
                   else f"{len(double_units)} readable lines contain a duplicated '%'",
                   lines=double_units[:10])
    readable_none_text = [
        {"path": path, "section": line.get("section")}
        for path, instance in items
        for line in (instance.get("readable") or {}).get("lines") or []
        if not line.get("text")
    ]
    if not legacy:
        result.add("items.readable_text", "pass" if not readable_none_text else "warn",
                   f"{len(readable_none_text)} readable lines have no text", lines=readable_none_text[:10])
    equipment = spec.get("equipment", {})
    result.add("equipment.absent_slots", "pass",
               f"absent slots preserved: {equipment.get('absent_slots')}",
               absent_slots=equipment.get("absent_slots"))

    affixes = list(_iter_affix_instances(spec))
    if affixes:
        missing_tier = [path for path, entry in affixes if (entry.get("tier") or {}).get("status") != "resolved"]
        result.add("affixes.tiers", "pass" if not missing_tier else "warn",
                   f"{len(affixes) - len(missing_tier)}/{len(affixes)} affix tiers resolved",
                   unresolved=missing_tier)
        missing_identity = [
            path for path, entry in affixes if (entry.get("affix") or {}).get("status") != "resolved"
        ]
        result.add("affixes.identity", "pass" if not missing_identity else "warn",
                   f"{len(affixes) - len(missing_identity)}/{len(affixes)} affix identities resolved",
                   unresolved=missing_identity)
        absent_rolls = [
            {"path": path, "encoded_id": entry.get("encoded_id"), "channel": entry.get("channel")}
            for path, entry in affixes
            if (entry.get("roll") or {}).get("status") in ("absent", "null")
        ]
        result.add("affixes.absent_rolls", "pass",
                   f"{len(absent_rolls)} affix instances have absent/null rolls (reported, not fabricated)",
                   absent=absent_rolls)
        zero_rolls = [
            path for path, entry in affixes
            if (entry.get("roll") or {}).get("status") == "present" and entry["roll"].get("raw") == 0
        ]
        result.add("affixes.zero_rolls", "pass",
                   f"{len(zero_rolls)} explicit r=0 values preserved as valid rolls", paths=zero_rolls)

    zero_nodes = [node for _, node in _iter_nodes(spec) if node.get("explicit_zero")]
    result.add("nodes.explicit_zero", "pass",
               f"{len(zero_nodes)} explicit zero-point node entries preserved")

    context = spec.get("context") or {}
    raw_buffs = (context.get("active_buffs") or {}).get("raw") or {}
    if "skills" not in raw_buffs:
        result.add("context.active_buffs", "warn", "raw activeBuffs.skills missing")
    else:
        result.add("context.active_buffs", "pass", "raw activeBuffs preserved verbatim", skills=raw_buffs.get("skills"))
    if legacy:
        result.add("readable.summary", "warn", "历史 revision 没有 readable_summary（跳过）")
    else:
        readability = (spec.get("readable_summary") or {}).get("by_completeness") or {}
        result.add(
            "readable.summary",
            "pass" if (spec.get("readable_summary") or {}).get("items") else "warn",
            f"readable completeness: {readability}",
            by_completeness=readability,
        )
    blessing_issues = [
        issue for issue in spec.get("issues", [])
        if issue.get("code") == "implicit_definition_unavailable"
        and "blessing" in str((issue.get("detail") or {}).get("role", ""))
    ]
    result.add(
        "blessings.implicit_definitions",
        "pass" if not blessing_issues else "warn",
        f"{len(blessing_issues)} blessing instances still lack an implicit definition",
        paths=[issue.get("path") for issue in blessing_issues],
    )

    errors = [issue for issue in spec.get("issues", []) if issue.get("severity") == "error"]
    result.add("issues.errors", "pass" if not errors else "warn",
               f"{len(errors)} error-severity issues recorded", codes=sorted({issue['code'] for issue in errors}))
    return result.summary()


# --------------------------------------------------------------------- explain
def explain(spec: dict, path: str) -> dict:
    """Explain one path in a BuildSpec: value, provenance pointer and related issues."""
    path = (path or "").strip().strip("/").replace("/", ".")
    resolved = None
    found = False

    def walk(value, parts):
        nonlocal found, resolved
        if not parts:
            resolved = value
            found = True
            return
        head, rest = parts[0], parts[1:]
        if isinstance(value, dict):
            if head in value:
                walk(value[head], rest)
                return
            if "instance" in value and isinstance(value["instance"], dict) and head in value["instance"]:
                walk(value["instance"][head], rest)
                return
            raise KeyError(head)
        if isinstance(value, list):
            try:
                index = int(head)
            except ValueError:
                raise KeyError(head)
            walk(value[index], rest)
            return
        raise KeyError(head)

    try:
        parts = path.split(".") if path else []
        if parts and parts[0] == "equipment" and len(parts) > 1 and not parts[1].isdigit():
            slots = (spec.get("equipment") or {}).get("slots", [])
            match = next((slot for slot in slots if slot.get("slot") == parts[1]), None)
            if match is None:
                return {"found": False, "path": path, "reason": f"no equipment slot named {parts[1]!r}"}
            if len(parts) == 2:
                resolved = match
                found = True
            else:
                walk(match, parts[2:])
        elif parts and parts[0] == "passive_tree" and len(parts) > 2 and parts[1] == "nodes":
            nodes = (spec.get("passive_tree") or {}).get("nodes", [])
            match = next((n for n in nodes if str(n.get("node_id")) == parts[2]), None)
            if match is None:
                return {"found": False, "path": path, "reason": f"no passive node {parts[2]!r}"}
            if len(parts) == 3:
                resolved = match
                found = True
            else:
                walk(match, parts[3:])
        else:
            if not path:
                return {"found": False, "path": path, "reason": "empty path"}
            walk(spec, parts)
    except (KeyError, IndexError) as exc:
        return {"found": False, "path": path, "reason": f"path not found: {exc}"}
    if not found:
        return {"found": False, "path": path, "reason": "path not found"}

    pointer = None
    if isinstance(resolved, dict):
        pointer = resolved.get("source_path")
        if pointer is None and isinstance(resolved.get("instance"), dict):
            pointer = resolved["instance"].get("source_path")
    elif path.endswith("source_path") and isinstance(resolved, str):
        pointer = resolved
    related_issues = []
    needle = path
    normalized_needle = needle[5:] if needle.startswith("data.") else needle
    for issue in spec.get("issues", []):
        issue_path = issue.get("path") or ""
        normalized_issue = issue_path[5:] if issue_path.startswith("data.") else issue_path
        if (
            normalized_issue == normalized_needle
            or normalized_issue.startswith(normalized_needle + ".")
            or normalized_needle.startswith(normalized_issue + ".")
        ):
            related_issues.append(issue)
    return {
        "found": True,
        "path": path,
        "source_path": pointer,
        "value": resolved,
        "related_issues": related_issues,
        "raw_source": (spec.get("extensions") or {}).get("raw_source"),
        "pointer_note": "source_path is a dot pointer into the archived raw build_data payload",
    }
