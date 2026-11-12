"""Link normalized effect values to stat definitions (review P1-2).

``effect_values`` rows keep the source's raw property payload.  This module is
a post-normalization pass that resolves those raw property ids against the
``stats`` table and records the result in ``effect_values.stat_id`` /
``stat_link_status`` / ``stat_link_evidence``.

Design rules (phase-1 review P1-2):

* namespaces are kept separate per source: Last Epoch Tools property ids are
  only resolved against Last Epoch Tools stat rows, Tunklab property ids only
  against Tunklab stat rows.  Ids from one source are never linked to another
  source's stat entity (no cross-source guess);
* a property id alone is only accepted when it resolves to exactly one stat in
  the chosen namespace (``linked``); display names are never used for fuzzy
  merging - the source's own numeric ids are the only matching key;
* when the raw payload also carries ``specialTag`` / ``tags`` / ``extraTag``
  qualifiers the base property is still linked, but the status becomes
  ``composite`` and the qualifiers are preserved in the evidence: the 0 value of
  those fields is not a qualifier;
* a property id that has no stat row in its namespace is ``unknown_namespace``;
  several candidate stats for one id are ``ambiguous`` (candidate ids are kept
  in the evidence, ``stat_id`` stays NULL);
* a raw payload that is not JSON, or whose property id cannot be read, is
  ``unparsed``; an effect value without any property field is
  ``not_applicable`` (e.g. a pure description effect).

The pass is idempotent: rows whose target stat/status/evidence already match are
not touched, so calling it twice does not change existing ``linked`` rows.  It
never inserts rows and never commits (the caller owns the transaction).

``link_stats`` returns a summary dict for logs/reports::

    {"total": 12011, "linked": 4459, "composite": 2157,
     "unknown_namespace": 5395, "ambiguous": 0, "unparsed": 0,
     "not_applicable": 0, "updated": 12011, "unchanged": 0,
     "linked_ratio": 0.3712, "by_owner_kind": {...}, "by_source": {...},
     "by_status": {...}, "namespaces": {...}, "ambiguous_keys": [...]}
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any, Callable, Iterable, Mapping

STATUS_LINKED = "linked"
STATUS_COMPOSITE = "composite"
STATUS_UNKNOWN_NAMESPACE = "unknown_namespace"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_UNPARSED = "unparsed"
STATUS_NOT_APPLICABLE = "not_applicable"

STATUS_VALUES = (
    STATUS_LINKED,
    STATUS_COMPOSITE,
    STATUS_UNKNOWN_NAMESPACE,
    STATUS_AMBIGUOUS,
    STATUS_UNPARSED,
    STATUS_NOT_APPLICABLE,
)

#: Last Epoch Tools property scopes that become stat-link namespaces.
LETOOLS_SCOPE_NAMESPACES: dict[str, str] = {
    "coreDB.propertyList": "letools:coreDB.propertyList",
    "coreDB.playerPropertyList": "letools:coreDB.playerPropertyList",
    "coreDB.abilityPropertyList": "letools:coreDB.abilityPropertyList",
    "coreDB.coreAttributes": "letools:coreDB.coreAttributes",
    "coreDB.idolAltarPropertyList.list": "letools:coreDB.idolAltarPropertyList.list",
}

#: Namespace used to resolve an effect value of a given source.  Property ids
#: from item implicits/mods and from skill-tree node stats share the game's
#: master property list, so both use ``coreDB.propertyList``.  Tunklab has its
#: own property table.  A source we cannot attribute is resolved against both
#: primary namespaces and reported as ambiguous when both match differently.
SOURCE_NAMESPACE: dict[str, str] = {
    "letools": "letools:coreDB.propertyList",
    "tunklab": "tunklab:property",
}

#: raw payload keys that may carry the property id, in lookup order.
_PROPERTY_KEYS = ("property", "propertyId", "property_id", "propertyID")
#: nested raw payload sections that may carry the property id.
_NESTED_PROPERTY_SECTIONS = ("stat", "mod", "implicit")
#: qualifier keys whose non-zero values make an otherwise direct id composite.
_QUALIFIER_KEYS = ("specialTag", "tags", "extraTag")
#: value-semantics keys kept in the evidence for traceability (not qualifiers).
_VALUE_SEMANTIC_KEYS = ("type", "modifierType", "modType", "setProperty")


def _raw_key(value: Any) -> str | None:
    """Normalize a property id to the string key used by the namespace index.

    ``"1.0"``-style ids (ability property list raw enums) keep their dotted
    form; numeric ids are normalized to integers so that ``1`` and ``"1"`` and
    ``1.0`` all address the same stat.  Anything else keeps its stripped string
    form, which still allows exact-id namespaces (e.g. attribute-name keys).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.lstrip("-").isdigit():
            return str(int(text))
        # Dotted ids like ``1.0`` are ability-property keys; keep them verbatim
        # so they never collide with plain property ``1``.
        return text
    return None


def _json_object(text: Any) -> dict[str, Any] | None:
    if isinstance(text, Mapping):
        return dict(text)
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _find_property(
    raw: dict[str, Any] | None, extensions: dict[str, Any] | None
) -> tuple[Any, str | None]:
    """Return ``(property_id, field_path)`` from a raw payload (or extensions)."""
    if raw is not None:
        for key in _PROPERTY_KEYS:
            if key in raw and raw[key] is not None:
                return raw[key], f"raw.{key}"
        for section in _NESTED_PROPERTY_SECTIONS:
            nested = raw.get(section)
            if isinstance(nested, Mapping):
                for key in _PROPERTY_KEYS:
                    if key in nested and nested[key] is not None:
                        return nested[key], f"raw.{section}.{key}"
    if extensions is not None:
        for section in _NESTED_PROPERTY_SECTIONS:
            nested = extensions.get(section)
            if isinstance(nested, Mapping):
                for key in _PROPERTY_KEYS:
                    if key in nested and nested[key] is not None:
                        return nested[key], f"extensions.{section}.{key}"
        for key in _PROPERTY_KEYS:
            if key in extensions and extensions[key] is not None:
                return extensions[key], f"extensions.{key}"
    return None, None


def _qualifiers(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        key: raw[key]
        for key in _QUALIFIER_KEYS
        if key in raw and raw[key] not in (0, None, False, "")
    }


def _value_semantics(raw: dict[str, Any]) -> dict[str, Any]:
    return {key: raw[key] for key in _VALUE_SEMANTIC_KEYS if key in raw}


def _stat_namespaces(conn) -> tuple[dict[str, dict[str, list[str]]], dict[str, int]]:
    """Build ``namespace -> raw key -> [stat_id]`` from the ``stats`` table."""
    index: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    counts: Counter[str] = Counter()
    rows = conn.execute(
        "SELECT stat_id, raw_enum, source_scope FROM stats"
    ).fetchall()
    for row in rows:
        stat_id = row["stat_id"]
        scope = row["source_scope"] or ""
        if stat_id.startswith("stat:tunklab:"):
            namespace = "tunklab:property"
        elif scope in LETOOLS_SCOPE_NAMESPACES:
            namespace = LETOOLS_SCOPE_NAMESPACES[scope]
        else:
            continue
        key = _raw_key(row["raw_enum"])
        if key is None:
            continue
        index[namespace][key].append(stat_id)
        counts[namespace] += 1
    return dict(index), dict(counts)


def _namespaces_for_source(source: str | None) -> list[str]:
    if source in SOURCE_NAMESPACE:
        return [SOURCE_NAMESPACE[source]]
    return sorted(SOURCE_NAMESPACE.values())


def _effect_rows(conn) -> Iterable[Mapping[str, Any]]:
    return conn.execute(
        """
        SELECT ev.value_id AS value_id,
               ev.raw AS raw,
               ev.stat_id AS stat_id,
               ev.stat_link_status AS stat_link_status,
               ev.stat_link_evidence AS stat_link_evidence,
               e.effect_id AS effect_id,
               e.effect_category AS effect_category,
               e.extensions AS effect_extensions,
               e.owner_entity_id AS owner_entity_id,
               ent.kind AS owner_kind,
               ent.primary_source AS owner_source
        FROM effect_values ev
        JOIN effects e ON e.effect_id = ev.effect_id
        LEFT JOIN entities ent ON ent.entity_id = e.owner_entity_id
        """
    ).fetchall()


def _source_of(row: Mapping[str, Any]) -> str:
    source = row["owner_source"]
    if source:
        return source
    effect_id = row["effect_id"] or ""
    if ":letools:" in effect_id:
        return "letools"
    owner = row["owner_entity_id"] or ""
    # canonical entity id scheme: <kind>:<source>:<scope>:<source_id>
    parts = owner.split(":", 2)
    if len(parts) >= 2 and parts[1] in ("letools", "tunklab"):
        return parts[1]
    return "unknown"


def _owner_kind_of(row: Mapping[str, Any]) -> str:
    kind = row["owner_kind"]
    if kind:
        return kind
    owner = row["owner_entity_id"] or ""
    return owner.split(":", 1)[0] if owner else "unknown"


def _classify(
    row: Mapping[str, Any],
    index: Mapping[str, Mapping[str, list[str]]],
) -> tuple[str, str | None, dict[str, Any] | None]:
    """Classify one ``effect_values`` row.

    Returns ``(status, stat_id, evidence)``.
    """
    source = _source_of(row)
    owner_kind = _owner_kind_of(row)
    effect_category = row["effect_category"]
    namespaces = _namespaces_for_source(source)

    raw = _json_object(row["raw"])
    extensions = _json_object(row["effect_extensions"])
    evidence: dict[str, Any] = {
        "source": source,
        "owner_kind": owner_kind,
        "effect_category": effect_category,
        "namespaces": namespaces,
    }

    if raw is None:
        evidence.update({"match_rule": "raw_payload_not_json"})
        return STATUS_UNPARSED, None, evidence

    property_value, field_path = _find_property(raw, extensions)
    if field_path is None:
        evidence.update({"match_rule": "no_property_field_in_effect"})
        return STATUS_NOT_APPLICABLE, None, evidence

    key = _raw_key(property_value)
    evidence.update({"property": property_value, "property_key": key, "field": field_path})
    if key is None:
        evidence.update({"match_rule": "property_not_a_readable_id"})
        return STATUS_UNPARSED, None, evidence

    # Resolve the id in the namespace(s) that belong to this source.  Candidate
    # lists are sorted so the evidence JSON is stable across rebuilds with a
    # different stats row insertion order.
    matched: list[str] = []
    namespaces_with_key: list[str] = []
    candidates_by_namespace: dict[str, list[str]] = {}
    for namespace in namespaces:
        candidates = sorted(index.get(namespace, {}).get(key, []))
        if not candidates:
            continue
        namespaces_with_key.append(namespace)
        candidates_by_namespace[namespace] = candidates
        for candidate in candidates:
            if candidate not in matched:
                matched.append(candidate)
    matched.sort()

    if not matched:
        evidence.update(
            {
                "match_rule": "no_stat_for_property_in_namespace",
                "namespace": namespaces[0] if namespaces else None,
            }
        )
        return STATUS_UNKNOWN_NAMESPACE, None, evidence

    if len(matched) > 1:
        evidence.update(
            {
                "match_rule": "multiple_stat_candidates",
                "candidates": candidates_by_namespace,
            }
        )
        return STATUS_AMBIGUOUS, None, evidence

    stat_id = matched[0]
    namespace = namespaces_with_key[0] if namespaces_with_key else namespaces[0]
    qualifiers = _qualifiers(raw)
    semantics = _value_semantics(raw)
    evidence.update(
        {
            "namespace": namespace,
            "stat_id": stat_id,
            "candidates": [stat_id],
        }
    )
    if semantics:
        evidence["value_semantics"] = semantics
    if qualifiers:
        evidence["qualifiers"] = qualifiers
        evidence["match_rule"] = "base_property_with_qualifier"
        return STATUS_COMPOSITE, stat_id, evidence
    evidence["match_rule"] = "direct_id_unique_in_namespace"
    return STATUS_LINKED, stat_id, evidence


def _empty_summary() -> dict[str, Any]:
    return {
        "total": 0,
        "linked": 0,
        "composite": 0,
        "unknown_namespace": 0,
        "ambiguous": 0,
        "unparsed": 0,
        "not_applicable": 0,
        "updated": 0,
        "unchanged": 0,
        "linked_ratio": 0.0,
        "composite_with_base_stat": 0,
        "by_status": {status: 0 for status in STATUS_VALUES},
        "by_owner_kind": {},
        "by_source": {},
        "namespaces": {},
        "ambiguous_keys": [],
    }


def link_stats(
    conn,
    *,
    logger: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Link ``effect_values`` to ``stats`` and return classification counts.

    Idempotent; does not commit.  ``conn`` must already carry schema 0003
    (``effect_values.stat_link_status`` / ``stat_link_evidence``).
    """
    index, namespace_counts = _stat_namespaces(conn)
    rows = _effect_rows(conn)

    by_status: Counter[str] = Counter()
    by_owner_kind: dict[str, Counter[str]] = defaultdict(Counter)
    by_source: dict[str, Counter[str]] = defaultdict(Counter)
    ambiguous_keys: list[dict[str, Any]] = []
    updates: list[tuple[Any, str | None, str, str | None]] = []
    composite_with_base_stat = 0
    total = 0

    for row in rows:
        total += 1
        status, stat_id, evidence = _classify(row, index)
        evidence_json = json.dumps(
            evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        owner_kind = _owner_kind_of(row)
        source = _source_of(row)
        by_status[status] += 1
        by_owner_kind[owner_kind][status] += 1
        by_source[source][status] += 1
        if status == STATUS_COMPOSITE and stat_id is not None:
            composite_with_base_stat += 1
        if status == STATUS_AMBIGUOUS:
            ambiguous_keys.append(
                {
                    "value_id": row["value_id"],
                    "property": evidence.get("property"),
                    "namespaces": evidence.get("namespaces"),
                }
            )
        # Idempotency: only touch rows whose target values actually differ.
        current = (
            row["stat_id"],
            row["stat_link_status"],
            row["stat_link_evidence"],
        )
        target = (stat_id, status, evidence_json)
        if current != target:
            updates.append((stat_id, status, evidence_json, row["value_id"]))

    if updates:
        conn.executemany(
            "UPDATE effect_values SET stat_id = ?, stat_link_status = ?, "
            "stat_link_evidence = ? WHERE value_id = ?",
            updates,
        )

    summary = _empty_summary()
    summary["total"] = total
    for status in STATUS_VALUES:
        summary[status] = by_status.get(status, 0)
    summary["by_status"] = {status: by_status.get(status, 0) for status in STATUS_VALUES}
    summary["by_owner_kind"] = {
        kind: {
            status: counts.get(status, 0)
            for status in STATUS_VALUES
            if counts.get(status, 0)
        }
        for kind, counts in sorted(by_owner_kind.items())
    }
    summary["by_source"] = {
        source: {
            status: counts.get(status, 0)
            for status in STATUS_VALUES
            if counts.get(status, 0)
        }
        for source, counts in sorted(by_source.items())
    }
    summary["namespaces"] = dict(sorted(namespace_counts.items()))
    summary["updated"] = len(updates)
    summary["unchanged"] = total - len(updates)
    summary["composite_with_base_stat"] = composite_with_base_stat
    summary["linked_ratio"] = round(summary["linked"] / total, 6) if total else 0.0
    summary["ambiguous_keys"] = ambiguous_keys[:20]

    if logger is not None:
        logger(
            "[stat-link] total={total} linked={linked} composite={composite} "
            "unknown_namespace={unknown_namespace} ambiguous={ambiguous} "
            "unparsed={unparsed} not_applicable={not_applicable} updated={updated}".format(
                **{key: summary[key] for key in (
                    "total", "linked", "composite", "unknown_namespace",
                    "ambiguous", "unparsed", "not_applicable", "updated",
                )}
            )
        )
    return summary
