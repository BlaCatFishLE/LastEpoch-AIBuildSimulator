"""Calculator-facing export for one BuildSpec revision (``calculation-input-v1``).

This module is the *third stage* (calculator) boundary of the phase-2 importer.  It
turns one immutable ``BuildSpec v1`` dict into a flat, self-describing document in
which every single modifier is one dict with a stable ``mod_id``, its stat /
property / tag / modifier-type association, its restored numeric value (when the
source roll is known), its declared range and direction, its honest status, and its
provenance back into the BuildSpec and the original source payload.

What this module does
---------------------
* Pure standard library; a read-only consumer of the BuildSpec dict.  It never
  opens the catalog SQLite database, never performs network I/O, never mutates the
  input spec and never re-derives game formulas (all restored numbers, display
  strings and player-readable lines are taken from the BuildSpec as published).
* Flattens **every** in-scope modifier, including hidden / non-rollable unique mods
  (``hidden=true``, ``can_roll=false``) that exist only for calculator consumption,
  and the unique-tooltip description lines (``kind="description"``).
* Applies the 2026-11-04 re-review R1 scope: effects whose *source property
  identity* is a non-DPS effect (:data:`NON_DPS_PROPERTIES`, e.g. propertyList 104
  = Increased Drop Rate) are excluded from this DPS input document.  The filter is
  by property id, never by English display text.  The BuildSpec itself is
  unchanged — the excluded effects stay in the revision and in the source archive;
  only this export omits them (reported in ``excluded_non_dps*`` and in ``notes``).
* Emits ``unresolved_inputs``: the exact values the source payload does not contain
  and that the local-input interface can supply (affix rolls, unique-mod rolls per
  ``rollId``, implicit rolls).  The entries are derived from the same BuildSpec
  facts as ``le_build.inputs.describe_targets`` and carry a copy-paste-able
  ``suggested_command``.
* Is deterministic: re-exporting the same BuildSpec revision produces the same
  bytes.  ``generated_at`` is the only timestamp in the document; when it is not
  supplied it defaults to the revision's ``published_at`` (stable per revision) so
  repeated exports stay byte-identical (contract §5.5).  Callers that want the
  export wall-clock time pass ``generated_at=<ISO-8601 string>`` explicitly.

What this module explicitly does NOT claim
------------------------------------------
No combat semantics, no DPS, no aggregation across items, no "character sheet"
values.  A restored number only says "for this source roll and these declared
endpoints the archived planner computes this number".  ``display_value`` /
``readable_text`` are display strings copied from the BuildSpec, not a second
source of numbers.

Contract
--------
``docs/phase-2-local-input-and-calc-export.zh-CN.md`` §4 (and §3 for local-input
provenance fields).  JSON Schema: ``schemas/calculation-input-v1.json``.

Public API
----------
* :data:`CALCULATION_SCHEMA_VERSION`
* :func:`build_calculation_input` -- full document (dict) for one revision
* :func:`iter_mods` -- iterate the flattened per-mod dicts (same objects/values as
  ``document["mods"]``)
* :func:`unresolved_inputs` -- list of missing-value targets with commands

Shape conventions
-----------------
* ``items[]`` wrapper: the contract's ``{"_item": {...}, "mods": [...]}`` shape is
  emitted as a duplicate of the inline item metadata (``instance_id``, ``role``,
  ``slot``, ``display_name``, ``base_name``, ``item_type``, ``rarity``,
  ``source_path``, ``encoded_id``, definition/base entity ids, ``position``), so a
  consumer can use either spelling.  ``document["mods"]`` is the same flat list.
* A ``mod`` dict always carries the keys documented in §4 (the flat ``mods`` list
  can therefore be iterated without key probing): ``mod_id``, ``item_instance_id``,
  ``source``, ``channel``, ``kind``, ``stat``, ``property``, ``tags``,
  ``special_tag``, ``modifier_type``, ``value_numeric``, ``display_value``,
  ``value_status``, ``value_source``, ``roll``, ``range``, ``hidden``, ``unique``,
  ``affix``, ``readable_text``, ``provenance``.  Additional honest keys:
  ``derived_display_value`` (the planner average branch when no roll is known,
  display-only), ``effective_value_policy`` and ``stat_resolution``.
* ``kind="description"`` entries (unique tooltip descriptions) have ``source=
  "unique_mod"``, ``channel="unique"``, no numeric value (``value_status="missing"``,
  ``value_numeric=null``) and carry the readable description in ``readable_text``.
* Legacy revisions (no ``readable`` layer): structured values are still exported,
  ``readable_text`` is ``null`` and unique mods are reconstructed from the
  restoration results where present.  No crash, no invented value.
* Top-level ``excluded_non_dps`` (count), ``excluded_non_dps_properties`` (source
  property identity list) and ``excluded_non_dps_mod_ids`` report what R1 removed
  from the DPS input; an excluded effect is listed nowhere in ``mods`` /
  ``items[].mods`` and an item left with zero mods keeps its item entry.
"""

from __future__ import annotations

import datetime as dt

CALCULATION_SCHEMA_VERSION = "calculation-input-v1"

# ---------------------------------------------------------------------------
# Vocabulary (kept explicit so a consumer never has to guess)
# ---------------------------------------------------------------------------
VALUE_STATUS_ACTUAL = "actual"
VALUE_STATUS_RANGE = "range"
VALUE_STATUS_CONSTANT = "constant"
VALUE_STATUS_MISSING = "missing"
VALUE_STATUS_UNRESOLVED = "unresolved"
VALUE_STATUSES = (
    VALUE_STATUS_ACTUAL,
    VALUE_STATUS_RANGE,
    VALUE_STATUS_CONSTANT,
    VALUE_STATUS_MISSING,
    VALUE_STATUS_UNRESOLVED,
)

VALUE_SOURCE_SOURCE = "source"
VALUE_SOURCE_LOCAL_INPUT = "local_input"
VALUE_SOURCE_LOCAL_VALUE_OVERRIDE = "local_value_override"
VALUE_SOURCES = (
    VALUE_SOURCE_SOURCE,
    VALUE_SOURCE_LOCAL_INPUT,
    VALUE_SOURCE_LOCAL_VALUE_OVERRIDE,
)

ROLL_PRESENT = "present"
ROLL_ABSENT = "absent"
ROLL_NULL = "null"

SOURCE_AFFIX = "affix"
SOURCE_IMPLICIT = "implicit"
SOURCE_UNIQUE_MOD = "unique_mod"

CHANNEL_IMPLICIT = "implicit"
CHANNEL_UNIQUE = "unique"
AFFIX_CHANNELS = ("normal", "sealed", "primordial", "corrupted")

KIND_MOD = "mod"
KIND_DESCRIPTION = "description"

DIRECTION_ASCENDING = "ascending"
DIRECTION_INVERTED = "inverted"
DIRECTION_CONSTANT = "constant"

INPUT_KIND_AFFIX_ROLL = "affix_roll"
INPUT_KIND_UNIQUE_MOD_ROLL = "unique_mod_roll"
INPUT_KIND_IMPLICIT_ROLL = "implicit_roll"

DEFAULT_ROLL_PLACEHOLDER = "<0-255>"

#: 2026-11-04 re-review R1 scope: drop-rate (non-DPS) effects are not part of the
#: DPS/BD input.  They are identified by **source property identity**, never by the
#: English display text: ``coreDB.propertyList 104`` = "Increased Drop Rate" (the
#: five drop-rate blessing implicits in the fixed sample use exactly this property,
#: with tags/specialTag combinations 1/3, 21/0, 6/0, 0/4, 2/4).  The BuildSpec keeps
#: these values and their readable lines; only this DPS-oriented export excludes
#: them, and the fact is declared by the top-level ``excluded_non_dps*`` fields plus
#: a ``notes`` entry ("raw source remains archived; nothing was deleted").
NON_DPS_PROPERTIES = frozenset({104})


# ---------------------------------------------------------------------------
# Small dict/list/JSON helpers
# ---------------------------------------------------------------------------
def _dict(value) -> dict:
    """Return ``value`` when it is a dict, else an empty dict (never raises)."""
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    """Return ``value`` when it is a list, else an empty list (never raises)."""
    return value if isinstance(value, list) else []


def _text(value) -> str | None:
    """Return a non-empty string, else ``None``."""
    return value if isinstance(value, str) and value else None


def _first(*values):
    """First argument that is not ``None``/``""`` (``0``/``False`` are preserved)."""
    for value in values:
        if value is None or value == "":
            continue
        return value
    return None


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# BuildSpec traversal (fixed order: equipment slots -> idols -> blessings)
# ---------------------------------------------------------------------------
def _iter_item_instances(spec: dict):
    """Yield ``(fallback_path, instance)`` for every in-scope item instance.

    The iteration order matches ``le_build.inputs._iter_item_instances`` /
    ``le_build.report._all_item_instances``: equipment slots as listed (the
    published ``slot_order``), idols by entry order, blessings by entry order.
    Empty or malformed instance placeholders are skipped rather than exported as
    fake items.
    """
    spec = _dict(spec)
    for slot in _list(_dict(spec.get("equipment")).get("slots")):
        instance = _dict(_dict(slot).get("instance"))
        if instance.get("instance_id"):
            yield f"equipment.{_dict(slot).get('slot')}", instance
    for entry in _list(_dict(spec.get("idols")).get("entries")):
        instance = _dict(_dict(entry).get("instance"))
        if instance.get("instance_id"):
            yield f"idols.{_dict(entry).get('index')}", instance
    for entry in _list(_dict(spec.get("blessings")).get("entries")):
        instance = _dict(_dict(entry).get("instance"))
        if instance.get("instance_id"):
            yield f"blessings.{_dict(entry).get('timeline')}", instance


def _instance_id(instance: dict, path: str) -> str:
    return _first(_dict(instance).get("instance_id"), path)


def _instance_label(instance: dict) -> str:
    """Player-facing label: readable display name, else definition/base ref name."""
    instance = _dict(instance)
    readable = _dict(instance.get("readable"))
    definition = _dict(instance.get("definition"))
    base = _dict(instance.get("base"))
    return _first(
        readable.get("display_name"),
        _dict(definition.get("ref")).get("name"),
        _dict(base.get("ref")).get("name"),
        definition.get("name"),
        base.get("name"),
        instance.get("instance_id"),
        instance.get("encoded_id"),
        "?",
    )


def _readable_lines(instance: dict) -> list:
    return _list(_dict(_dict(instance).get("readable")).get("lines"))


def _find_line(lines, *, section=None, kind=None, diagnostics=None) -> dict | None:
    """Find one readable line by section/kind and a diagnostics key/value filter."""
    wanted = diagnostics or {}
    for raw_line in lines or []:
        line = _dict(raw_line)
        if section is not None and line.get("section") != section:
            continue
        if kind is not None and line.get("kind") != kind:
            continue
        diag = _dict(line.get("diagnostics"))
        if all(diag.get(key) == value for key, value in wanted.items()):
            return line
    return None


def _find_affix_line(lines, channel: str, value_index: int, entity_id, encoded_id=None):
    """Match the readable affix line for one affix value.

    ``readable.diagnostics.affix_index`` is the *value index inside the affix*, not
    the instance index, so several affixes of one channel share ``affix_index=0``;
    the affix entity id (or encoded id as a fallback) disambiguates them.
    """
    section = f"affix_{channel}"
    candidates: list[dict] = []
    for raw_line in lines or []:
        line = _dict(raw_line)
        if line.get("section") != section:
            continue
        diag = _dict(line.get("diagnostics"))
        if diag.get("channel") != channel:
            continue
        if diag.get("affix_index") != value_index:
            continue
        if entity_id is not None and diag.get("affix_entity_id") == entity_id:
            return line
        candidates.append(line)
    if encoded_id is not None:
        for line in candidates:
            if _dict(line.get("diagnostics")).get("encoded_id") == encoded_id:
                return line
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Small value helpers (data facts only; no game formula is re-implemented)
# ---------------------------------------------------------------------------
def _direction(minimum, maximum):
    """Range direction as declared by the definition endpoints (data comparison)."""
    if not _is_number(minimum) or not _is_number(maximum):
        return None
    low = float(minimum)
    high = float(maximum)
    if low == high:
        return DIRECTION_CONSTANT
    return DIRECTION_ASCENDING if low <= high else DIRECTION_INVERTED


def _value_status(value_numeric, roll_min, roll_max, roll_status) -> str:
    """Honest per-mod value status from the published structured data.

    * ``actual`` -- a numeric value exists (restored from a roll, an explicit local
      value, or a constant definition);
    * ``range`` -- no actual number, but the declared range endpoints are known
      (includes ``r``/``ur`` slots absent from the source);
    * ``missing`` -- neither a number nor a range is available (source value absent);
    * ``unresolved`` -- the structured value could not be established for another
      reason (e.g. stat/definition unresolved).
    """
    if value_numeric is not None:
        return VALUE_STATUS_ACTUAL
    if roll_min is not None and roll_max is not None:
        return VALUE_STATUS_RANGE
    if roll_status in (None, ROLL_ABSENT, ROLL_NULL):
        return VALUE_STATUS_MISSING
    return VALUE_STATUS_UNRESOLVED


def _value_source(*candidates) -> str:
    for candidate in candidates:
        if candidate in VALUE_SOURCES:
            return candidate
    return VALUE_SOURCE_SOURCE


def _stat_block(value: dict, diag: dict, evidence: dict) -> dict:
    """Stat association for one value; honest ``resolved`` flag.

    ``resolved`` is true only when the BuildSpec itself proves both a catalog
    ``stat_id`` and a stat name.  Unresolved unique mods (``ur=[]`` items) keep
    ``stat_id`` when the restoration evidence recorded one, but are reported as
    not resolved instead of guessing unit/name.
    """
    value = _dict(value)
    diag = _dict(diag)
    evidence = _dict(evidence)
    stat_id = _first(value.get("stat_id"), evidence.get("stat_id"), diag.get("stat_id"))
    name = _first(value.get("stat_name"), evidence.get("stat_name"), diag.get("stat_name"))
    unit = _first(value.get("unit"), diag.get("unit"))
    resolution = _first(value.get("stat_resolution"), evidence.get("stat_resolution"))
    resolved = bool(stat_id) and bool(name) and resolution != "unresolved"
    return {"stat_id": stat_id, "name": name, "unit": unit, "resolved": resolved}


def _attributes(value: dict, diag: dict) -> dict:
    """property / tags / special_tag / modifier_type with readable-diag fallback."""
    value = _dict(value)
    diag = _dict(diag)
    return {
        "property": _first(value.get("property"), diag.get("property")),
        "tags": _first(value.get("tags"), diag.get("tags")),
        "special_tag": _first(value.get("special_tag"), diag.get("special_tag")),
        "modifier_type": _first(value.get("modifier_type"), diag.get("modifier_type")),
    }


def _is_non_dps(property_id) -> bool:
    """True when the *source property identity* is a non-DPS effect (R1 scope).

    The decision uses the catalog property id of the value (the same identity the
    BuildSpec stores in ``property`` / ``stat_id``), never the display text, so a
    renamed drop-rate line cannot slip through and a non-drop-rate effect can never
    be removed by text matching.  Unknown/missing property ids are kept.
    """
    if isinstance(property_id, bool) or not isinstance(property_id, int):
        return False
    return property_id in NON_DPS_PROPERTIES


def _roll_block(kind, raw=None, roll_id=None, index=None, status=None) -> dict:
    return {"kind": kind, "raw": raw, "roll_id": roll_id, "index": index, "status": status}


def _range_block(minimum, maximum, direction=None, range_text=None) -> dict:
    return {
        "min": minimum,
        "max": maximum,
        "direction": _first(direction, _direction(minimum, maximum)),
        "range_text": _first(range_text),
    }


def _declared_range_text(line: dict, value_numeric) -> str | None:
    """Readable range text, only when the line is a declared-range (no actual) line.

    For an actual rolled value the readable layer stores the *display value* in
    ``range_text`` (a single number such as ``87``), not the declared range; copying
    that into ``range.range_text`` would be misleading, so it is reported as null.
    The numeric ``range.min/max`` endpoints are always exported.
    """
    if value_numeric is not None:
        return None
    return _first(_dict(line).get("range_text"))


def _mod(**overrides) -> dict:
    """Build one flat mod dict with every contract §4 key present.

    Missing optional data becomes explicit ``null`` / ``False`` values instead of a
    missing key, so the flat ``mods`` list can be iterated without key probing.
    """
    mod = {
        "mod_id": None,
        "item_instance_id": None,
        "source": None,
        "channel": None,
        "kind": KIND_MOD,
        "stat": {"stat_id": None, "name": None, "unit": None, "resolved": False},
        "property": None,
        "tags": None,
        "special_tag": None,
        "modifier_type": None,
        "value_numeric": None,
        "display_value": None,
        "value_status": VALUE_STATUS_UNRESOLVED,
        "value_source": VALUE_SOURCE_SOURCE,
        "roll": {"kind": None, "raw": None, "roll_id": None, "index": None, "status": None},
        "range": {"min": None, "max": None, "direction": None, "range_text": None},
        "hidden": False,
        "unique": None,
        "affix": None,
        "readable_text": None,
        "derived_display_value": None,
        "effective_value_policy": None,
        "stat_resolution": None,
        "provenance": {"source_path": None, "encoded_id": None, "input_id": None},
    }
    for key, value in overrides.items():
        if key in ("stat", "roll", "range", "provenance"):
            mod[key].update(_dict(value))
        else:
            mod[key] = value
    return mod


# ---------------------------------------------------------------------------
# Flattening: implicits
# ---------------------------------------------------------------------------
def _implicit_mods(instance: dict, iid: str) -> list[dict]:
    """One mod per implicit value (keyed by implicit-definition index)."""
    block = _dict(instance.get("implicit_rolls"))
    results = _list(block.get("values"))
    lines = _readable_lines(instance)
    source_path = _first(block.get("source_path"))
    encoded_id = instance.get("encoded_id")
    mods: list[dict] = []
    for implicit_index, raw_result in enumerate(results):
        result = _dict(raw_result)
        roll_status = result.get("roll_status")
        raw_roll = result.get("raw_roll")
        line = _find_line(lines, section="implicit", diagnostics={"implicit_index": implicit_index})
        line = _dict(line)
        diag = _dict(line.get("diagnostics"))
        for value_index, raw_value in enumerate(_list(result.get("values"))):
            value = _dict(raw_value)
            attributes = _attributes(value, diag)
            value_numeric = value.get("value_numeric")
            roll_min = value.get("roll_min")
            roll_max = value.get("roll_max")
            display_value = _first(value.get("display_value"), line.get("value_text"))
            mod_id = f"{iid}#implicit#{implicit_index}"
            if value_index:
                mod_id = f"{mod_id}.{value_index}"
            mods.append(
                _mod(
                    mod_id=mod_id,
                    item_instance_id=iid,
                    source=SOURCE_IMPLICIT,
                    channel=CHANNEL_IMPLICIT,
                    kind=KIND_MOD,
                    stat=_stat_block(value, diag, {}),
                    property=attributes["property"],
                    tags=attributes["tags"],
                    special_tag=attributes["special_tag"],
                    modifier_type=attributes["modifier_type"],
                    value_numeric=value_numeric,
                    display_value=display_value,
                    value_status=_value_status(value_numeric, roll_min, roll_max, roll_status),
                    value_source=_value_source(
                        value.get("value_source"), result.get("value_source"), diag.get("value_source")
                    ),
                    roll=_roll_block(
                        "implicit_roll",
                        raw=raw_roll,
                        index=implicit_index,
                        status=roll_status,
                    ),
                    range=_range_block(
                        roll_min,
                        roll_max,
                        direction=value.get("direction"),
                        range_text=_declared_range_text(line, value_numeric),
                    ),
                    hidden=False,
                    unique=None,
                    affix=None,
                    readable_text=_text(line.get("text")),
                    derived_display_value=value.get("derived_display_value"),
                    effective_value_policy=result.get("effective_value_policy") or None,
                    stat_resolution=value.get("stat_resolution"),
                    provenance={
                        "source_path": source_path,
                        "encoded_id": encoded_id,
                        "input_id": _first(value.get("input_id"), result.get("input_id")),
                    },
                )
            )
    return mods


# ---------------------------------------------------------------------------
# Flattening: affixes (all four channels)
# ---------------------------------------------------------------------------
def _affix_mods(instance: dict, iid: str) -> list[dict]:
    """One mod per affix value across normal/sealed/primordial/corrupted channels."""
    channels = _dict(instance.get("affixes"))
    lines = _readable_lines(instance)
    encoded_id = instance.get("encoded_id")
    mods: list[dict] = []
    for channel in AFFIX_CHANNELS:
        for affix_index, raw_affix in enumerate(_list(channels.get(channel))):
            affix = _dict(raw_affix)
            affix_ref = _dict(_dict(affix.get("affix")).get("ref"))
            entity_id = affix_ref.get("entity_id")
            affix_label = _first(affix_ref.get("name"), affix.get("encoded_id"))
            tier = _dict(affix.get("tier"))
            roll_block = _dict(affix.get("roll"))
            value_block = _dict(affix.get("value"))
            affix_source_path = _first(affix.get("source_path"))
            values = _list(value_block.get("values"))
            for value_index, raw_value in enumerate(values):
                value = _dict(raw_value)
                line = _find_affix_line(
                    lines, channel, value_index, entity_id, affix.get("encoded_id")
                )
                line = _dict(line)
                diag = _dict(line.get("diagnostics"))
                attributes = _attributes(value, diag)
                value_numeric = value.get("value_numeric")
                roll_min = _first(value.get("roll_min"), tier.get("roll_min"))
                roll_max = _first(value.get("roll_max"), tier.get("roll_max"))
                mod_id = f"{iid}#affix#{channel}#{affix_index}.{value_index}"
                mods.append(
                    _mod(
                        mod_id=mod_id,
                        item_instance_id=iid,
                        source=SOURCE_AFFIX,
                        channel=channel,
                        kind=KIND_MOD,
                        stat=_stat_block(value, diag, {}),
                        property=attributes["property"],
                        tags=attributes["tags"],
                        special_tag=attributes["special_tag"],
                        modifier_type=attributes["modifier_type"],
                        value_numeric=value_numeric,
                        display_value=_first(value.get("display_value"), line.get("value_text")),
                        value_status=_value_status(
                            value_numeric, roll_min, roll_max, roll_block.get("status")
                        ),
                        value_source=_value_source(
                            value.get("value_source"),
                            value_block.get("value_source"),
                            roll_block.get("value_source"),
                            diag.get("value_source"),
                        ),
                        roll=_roll_block(
                            "affix_roll",
                            raw=_first(roll_block.get("raw"), value.get("raw_roll")),
                            status=roll_block.get("status"),
                        ),
                        range=_range_block(
                            roll_min,
                            roll_max,
                            direction=value.get("direction"),
                            range_text=_declared_range_text(line, value_numeric),
                        ),
                        hidden=False,
                        unique=None,
                        affix={
                            "entity_id": entity_id,
                            "name": affix_label,
                            "source_tier": _first(affix.get("source_tier"), tier.get("source_tier")),
                            "tier_index": tier.get("tier_index"),
                            "channel": channel,
                            "encoded_id": affix.get("encoded_id"),
                            "affix_slot": _dict(affix.get("affix")).get("affix_slot"),
                        },
                        readable_text=_text(line.get("text")),
                        derived_display_value=value.get("derived_display_value"),
                        effective_value_policy=value_block.get("effective_value_policy") or None,
                        stat_resolution=value.get("stat_resolution"),
                        provenance={
                            "source_path": affix_source_path,
                            "encoded_id": affix.get("encoded_id") or encoded_id,
                            "input_id": _first(
                                value.get("input_id"),
                                roll_block.get("input_id"),
                                diag.get("input_id"),
                            ),
                        },
                    )
                )
    return mods


# ---------------------------------------------------------------------------
# Flattening: unique mods (including hidden / non-rollable) and descriptions
# ---------------------------------------------------------------------------
def _unique_definitions(block: dict, results: list) -> list[dict]:
    """Mod definitions for a unique item.

    New revisions publish ``unique_rolls.mods[]`` (definition + restored summary).
    Historical revisions only carry ``unique_rolls.values[]`` results, so the
    definition is reconstructed from each result's evidence; when even the results
    are absent (historical ``ur=[]`` items) there is nothing to export and the list
    stays empty instead of inventing mods.
    """
    definitions = _list(block.get("mods"))
    if definitions:
        return definitions
    derived: list[dict] = []
    for index, raw_result in enumerate(results):
        result = _dict(raw_result)
        evidence = _dict(result.get("evidence"))
        first_value = _dict(_list(result.get("values"))[0]) if _list(result.get("values")) else {}
        derived.append(
            {
                "mod_index": _first(evidence.get("mod_index"), index),
                "roll_id": evidence.get("roll_id"),
                "can_roll": bool(evidence.get("can_roll")),
                "hidden": bool(evidence.get("hide_in_tooltip")),
                "raw_value": None,
                "max_value": None,
                "roll_status": result.get("roll_status"),
                "raw_roll": result.get("raw_roll"),
                "value_source": result.get("value_source"),
                "input_id": result.get("input_id"),
                "restored": {
                    "status": result.get("status"),
                    "value_numeric": first_value.get("value_numeric"),
                    "display_value": first_value.get("display_value"),
                    "roll_min": first_value.get("roll_min"),
                    "roll_max": first_value.get("roll_max"),
                    "stat_name": first_value.get("stat_name"),
                    "direction": first_value.get("direction"),
                },
            }
        )
    return derived


def _unique_mods(instance: dict, iid: str) -> list[dict]:
    """One mod per unique mod (hidden and non-rollable included)."""
    block = _dict(instance.get("unique_rolls"))
    results = _list(block.get("values"))
    definitions = _unique_definitions(block, results)
    lines = _readable_lines(instance)
    source_path = _first(block.get("source_path"), f"{instance.get('source_path')}.ur")
    encoded_id = instance.get("encoded_id")
    mods: list[dict] = []
    for position, raw_definition in enumerate(definitions):
        definition = _dict(raw_definition)
        mod_index = definition.get("mod_index")
        if not _is_int(mod_index):
            mod_index = position
        result = {}
        if 0 <= mod_index < len(results):
            result = _dict(results[mod_index])
        elif position < len(results):
            result = _dict(results[position])
        evidence = _dict(result.get("evidence"))
        restored = _dict(definition.get("restored"))
        line = _dict(
            _find_line(lines, section="unique_mod", diagnostics={"mod_index": mod_index})
        )
        diag = _dict(line.get("diagnostics"))
        roll_id = _first(definition.get("roll_id"), evidence.get("roll_id"))
        can_roll = bool(_first(definition.get("can_roll"), evidence.get("can_roll"), False))
        hidden = bool(
            _first(definition.get("hidden"), evidence.get("hide_in_tooltip"), diag.get("hidden"), False)
        )
        roll_status = _first(
            definition.get("roll_status"), result.get("roll_status"), evidence.get("roll_status")
        )
        raw_roll = _first(definition.get("raw_roll"), result.get("raw_roll"))
        raw_value = _first(definition.get("raw_value"), restored.get("roll_min"))
        max_value = _first(definition.get("max_value"), restored.get("roll_max"))
        value_entries = _list(result.get("values"))
        if value_entries:
            entries = [(value_index, _dict(value)) for value_index, value in enumerate(value_entries)]
        else:
            entries = [(0, {})]
        for value_index, value in entries:
            attributes = _attributes(value, diag)
            stat = _stat_block(value, diag, evidence)
            value_numeric = _first(value.get("value_numeric"), restored.get("value_numeric"))
            roll_min = _first(value.get("roll_min"), restored.get("roll_min"), raw_value)
            roll_max = _first(value.get("roll_max"), restored.get("roll_max"), max_value)
            mod_id = f"{iid}#unique_mod#{mod_index}"
            if value_index:
                mod_id = f"{mod_id}.{value_index}"
            unique_block = {
                "mod_index": mod_index,
                "roll_id": roll_id,
                "can_roll": can_roll,
                # Not expressible from the published BuildSpec: readable.py knows which
                # tooltip descriptions consume which hidden mods but does not store the
                # mapping.  Kept as an empty list instead of guessing (reported to Lead).
                "description_indexes": [],
            }
            mods.append(
                _mod(
                    mod_id=mod_id,
                    item_instance_id=iid,
                    source=SOURCE_UNIQUE_MOD,
                    channel=CHANNEL_UNIQUE,
                    kind=KIND_MOD,
                    stat=stat,
                    property=attributes["property"],
                    tags=attributes["tags"],
                    special_tag=attributes["special_tag"],
                    modifier_type=attributes["modifier_type"],
                    value_numeric=value_numeric,
                    display_value=_first(
                        value.get("display_value"),
                        restored.get("display_value"),
                        line.get("value_text"),
                    ),
                    value_status=_value_status(value_numeric, roll_min, roll_max, roll_status),
                    value_source=_value_source(
                        value.get("value_source"),
                        definition.get("value_source"),
                        result.get("value_source"),
                        restored.get("value_source"),
                        diag.get("value_source"),
                    ),
                    roll=_roll_block(
                        "unique_roll" if can_roll else None,
                        raw=raw_roll,
                        roll_id=roll_id,
                        status=roll_status,
                    ),
                    range=_range_block(
                        roll_min,
                        roll_max,
                        direction=_first(
                            value.get("direction"),
                            restored.get("direction"),
                            _direction(raw_value, max_value),
                        ),
                        range_text=_declared_range_text(line, value_numeric),
                    ),
                    hidden=hidden,
                    unique=unique_block,
                    affix=None,
                    readable_text=None if hidden else _text(line.get("text")),
                    derived_display_value=value.get("derived_display_value"),
                    effective_value_policy=result.get("effective_value_policy") or None,
                    stat_resolution=value.get("stat_resolution"),
                    provenance={
                        "source_path": source_path,
                        "encoded_id": encoded_id,
                        "input_id": _first(
                            value.get("input_id"),
                            definition.get("input_id"),
                            restored.get("input_id"),
                            result.get("input_id"),
                            diag.get("input_id"),
                        ),
                    },
                )
            )
    return mods


def _description_mods(instance: dict, iid: str) -> list[dict]:
    """Unique-tooltip description lines as ``kind="description"`` entries.

    The contract's per-mod ``kind`` enum is ``mod | description``; descriptions keep
    no numeric value but carry the rendered text and its diagnostics.
    """
    lines = _readable_lines(instance)
    item_source_path = instance.get("source_path")
    encoded_id = instance.get("encoded_id")
    mods: list[dict] = []
    for position, raw_line in enumerate(lines):
        line = _dict(raw_line)
        if line.get("kind") != KIND_DESCRIPTION:
            continue
        diag = _dict(line.get("diagnostics"))
        description_index = _first(diag.get("description_index"), position)
        mods.append(
            _mod(
                mod_id=f"{iid}#unique_description#{description_index}",
                item_instance_id=iid,
                source=SOURCE_UNIQUE_MOD,
                channel=CHANNEL_UNIQUE,
                kind=KIND_DESCRIPTION,
                stat={"stat_id": None, "name": None, "unit": None, "resolved": False},
                property=None,
                tags=None,
                special_tag=None,
                modifier_type=None,
                value_numeric=None,
                display_value=None,
                value_status=VALUE_STATUS_MISSING,
                value_source=VALUE_SOURCE_SOURCE,
                roll=_roll_block(None),
                range=_range_block(None, None),
                hidden=False,
                unique=None,
                affix=None,
                readable_text=_text(line.get("text")),
                provenance={
                    "source_path": item_source_path,
                    "encoded_id": encoded_id,
                    "input_id": diag.get("input_id"),
                    "description_index": description_index,
                    "translation_key": diag.get("translation_key"),
                },
            )
        )
    return mods


# ---------------------------------------------------------------------------
# One item -> wrapper + flattened mods
# ---------------------------------------------------------------------------
def _item_metadata(instance: dict, path: str) -> dict:
    """Item-level metadata shared by the inline wrapper keys and ``_item``."""
    instance = _dict(instance)
    readable = _dict(instance.get("readable"))
    definition = _dict(instance.get("definition"))
    base = _dict(instance.get("base"))
    definition_ref = _dict(definition.get("ref"))
    base_ref = _dict(base.get("ref"))
    rarity = readable.get("rarity")
    if rarity is None and instance.get("rarity"):
        # Historical revisions have no readable layer; only the raw rarity marker.
        rarity = {
            "code": instance.get("rarity"),
            "label_zh": None,
            "label_en": None,
            "confidence": "source",
        }
    return {
        "instance_id": _instance_id(instance, path),
        "role": instance.get("role"),
        "slot": instance.get("slot"),
        "display_name": _instance_label(instance),
        "base_name": _first(readable.get("base_name"), base_ref.get("name"), base.get("name")),
        "item_type": readable.get("item_type"),
        "rarity": rarity,
        "source_path": _first(instance.get("source_path"), path),
        "encoded_id": instance.get("encoded_id"),
        "definition_entity_id": _first(definition_ref.get("entity_id"), definition.get("entity_id")),
        "base_entity_id": _first(base_ref.get("entity_id"), base.get("entity_id")),
        "position": instance.get("position"),
    }


def _item_mods(instance: dict, excluded: list | None = None) -> list[dict]:
    """Every exported mod of one item in stable order (non-DPS effects removed).

    Order: implicit values -> affix values (channel order normal/sealed/primordial/
    corrupted, affix list order, value order) -> unique mods (definition order) ->
    unique-tooltip description lines (readable line order).

    The 2026-11-04 R1 non-DPS filter is applied here, after the mod dicts are built,
    so it works for implicit, affix and unique mods alike (by source property
    identity, not text).  When ``excluded`` is a list, the removed mod dicts are
    appended to it so the caller can report the exact exclusion count/ids.
    """
    iid = _instance_id(instance, _first(instance.get("source_path"), "?"))
    mods: list[dict] = []
    mods.extend(_implicit_mods(instance, iid))
    mods.extend(_affix_mods(instance, iid))
    mods.extend(_unique_mods(instance, iid))
    mods.extend(_description_mods(instance, iid))
    kept: list[dict] = []
    for mod in mods:
        if _is_non_dps(mod.get("property")):
            if excluded is not None:
                excluded.append(mod)
            continue
        kept.append(mod)
    return kept


def _item_document(instance: dict, path: str, excluded: list | None = None) -> dict:
    metadata = _item_metadata(instance, path)
    mods = _item_mods(instance, excluded=excluded)
    document = dict(metadata)
    document["mods"] = mods
    # Contract §4 sample spelling: {"_item": {...}, "mods": [...]}; the inline keys
    # above are a convenience duplicate of the same metadata.
    document["_item"] = dict(metadata)
    return document


def _excluded_non_dps_summary(excluded_mods: list[dict]) -> dict:
    """Top-level declaration of what the R1 non-DPS filter removed (by identity)."""
    by_source: dict[str, int] = {}
    for mod in excluded_mods:
        source = mod.get("source")
        by_source[source] = by_source.get(source, 0) + 1
    return {
        "count": len(excluded_mods),
        "properties": sorted(NON_DPS_PROPERTIES),
        "mod_ids": [mod.get("mod_id") for mod in excluded_mods],
        "by_source": by_source,
    }


# ---------------------------------------------------------------------------
# Public: flattened mods
# ---------------------------------------------------------------------------
def iter_mods(spec: dict):
    """Iterate every flattened mod dict of one BuildSpec revision, in stable order.

    The yielded dicts have exactly the same content (and order) as the top-level
    ``mods`` list of :func:`build_calculation_input`.
    """
    for path, instance in _iter_item_instances(spec):
        for mod in _item_mods(instance):
            yield mod


# ---------------------------------------------------------------------------
# Public: unresolved inputs (same facts as le_build.inputs.describe_targets)
# ---------------------------------------------------------------------------
def _build_id(spec: dict) -> str:
    meta = _dict(_dict(spec).get("meta"))
    build_id = _first(meta.get("build_id"))
    if build_id:
        return build_id
    source = _first(meta.get("source"), "letools")
    share_id = _first(meta.get("share_id"), "?")
    return f"{source}:{share_id}"


def _suggested_affix_command(build_id: str, instance_path) -> str:
    return " ".join(
        [
            "le_build",
            "input",
            "set",
            "--build",
            str(build_id),
            "--kind",
            INPUT_KIND_AFFIX_ROLL,
            "--instance",
            str(instance_path),
            "--roll",
            DEFAULT_ROLL_PLACEHOLDER,
        ]
    )


def _suggested_affix_display_command(build_id: str, affix_path, value_index) -> str:
    return " ".join(
        [
            "le_build",
            "input",
            "set",
            "--build",
            str(build_id),
            "--kind",
            "explicit_value",
            "--target",
            "affix",
            "--instance",
            str(affix_path),
            "--affix-value-index",
            str(value_index),
            "--display-value",
            "<displayed-value>",
        ]
    )


def _suggested_unique_display_command(build_id: str, instance_path, mod_index) -> str:
    return " ".join(
        [
            "le_build",
            "input",
            "set",
            "--build",
            str(build_id),
            "--kind",
            "explicit_value",
            "--target",
            "unique_mod",
            "--instance",
            str(instance_path),
            "--mod-index",
            str(mod_index),
            "--display-value",
            "<displayed-value>",
        ]
    )


def _suggested_unique_command(build_id: str, instance_path, roll_id) -> str:
    return " ".join(
        [
            "le_build",
            "input",
            "set",
            "--build",
            str(build_id),
            "--kind",
            INPUT_KIND_UNIQUE_MOD_ROLL,
            "--instance",
            str(instance_path),
            "--roll-id",
            str(roll_id),
            "--roll",
            DEFAULT_ROLL_PLACEHOLDER,
        ]
    )


def _suggested_implicit_command(build_id: str, instance_path, index) -> str:
    return " ".join(
        [
            "le_build",
            "input",
            "set",
            "--build",
            str(build_id),
            "--kind",
            INPUT_KIND_IMPLICIT_ROLL,
            "--instance",
            str(instance_path),
            "--index",
            str(index),
            "--roll",
            DEFAULT_ROLL_PLACEHOLDER,
        ]
    )


def _range_text_for_affix(line: dict, affix: dict) -> str | None:
    """Current range text from the readable layer; numeric fallback when absent.

    Historical revisions have no readable layer, so the display range text is
    genuinely unavailable; ``None`` is reported instead of formatting a guessed
    game string (the numeric endpoints remain on the exported affix mods).
    """
    return _first(_dict(line).get("range_text"))


def unresolved_inputs(spec: dict) -> list[dict]:
    """List the currently missing values the local-input interface can supply.

    Derived read-only from the BuildSpec (same facts as
    ``le_build.inputs.describe_targets``):

    * ``affix_roll`` -- every affix instance whose ``roll.status`` is
      ``absent``/``null`` (the input targets the affix instance ``source_path``);
    * ``unique_mod_roll`` -- grouped by ``rollId`` for unique mods with
      ``can_roll=true`` and ``roll_status`` ``absent``/``null`` (the input targets the
      *item* ``source_path`` plus the roll id; hidden non-rollable mods never appear);
    * ``implicit_roll`` -- every implicit definition without an actual value.

    Each entry carries the current range text where the readable layer provides it,
    a readable label, and a ``suggested_command`` for the CLI (the roll value is the
    literal placeholder ``<0-255>``; everything else is copy-paste ready).
    """
    spec = _dict(spec)
    build_id = _build_id(spec)
    targets: list[dict] = []
    for path, instance in _iter_item_instances(spec):
        iid = _instance_id(instance, path)
        item_label = _instance_label(instance)
        item_source_path = _first(instance.get("source_path"), path)
        lines = _readable_lines(instance)

        # -- affix instances with a missing source roll ------------------------
        for channel in AFFIX_CHANNELS:
            for raw_affix in _list(_dict(instance.get("affixes")).get(channel)):
                affix = _dict(raw_affix)
                roll_status = _dict(affix.get("roll")).get("status")
                if roll_status not in (ROLL_ABSENT, ROLL_NULL):
                    continue
                affix_path = _first(affix.get("source_path"))
                if not affix_path:
                    continue
                value_entries = _list(_dict(affix.get("value")).get("values"))
                variables = []
                for value_index, raw_value in enumerate(value_entries):
                    value = _dict(raw_value)
                    if value.get("value_numeric") is not None:
                        continue
                    if str(value.get("value_source") or "").startswith("local_"):
                        continue
                    value_line = _dict(
                        _find_affix_line(
                            lines,
                            channel,
                            value_index,
                            _first(_dict(_dict(affix.get("affix")).get("ref")).get("entity_id")),
                            affix.get("encoded_id"),
                        )
                    )
                    unit = _first(value.get("unit"))
                    modifier_type = value.get("modifier_type")
                    try:
                        modifier_value = int(modifier_type or 0)
                    except (TypeError, ValueError):
                        modifier_value = 0
                    range_text = _first(value_line.get("range_text"))
                    percent = (
                        unit == "percent"
                        or modifier_value != 0
                        or "%" in str(range_text or "")
                        or "%" in str(value_line.get("text") or "")
                    )
                    variables.append(
                        {
                            "value_index": value_index,
                            "affix_value_index": value_index,
                            "label": _first(value_line.get("text"), value.get("stat_name")),
                            "stat_name": _first(value.get("stat_name")),
                            "unit": "percent" if percent else _first(unit, "flat"),
                            "percent": percent,
                            "range_text": range_text,
                            "roll_min": value.get("roll_min"),
                            "roll_max": value.get("roll_max"),
                            "direction": value.get("direction"),
                        }
                    )
                if not variables:
                    continue
                affix_ref = _dict(_dict(affix.get("affix")).get("ref"))
                entity_id = affix_ref.get("entity_id")
                affix_name = _first(affix_ref.get("name"), affix.get("encoded_id"))
                line = _dict(
                    _find_affix_line(lines, channel, 0, entity_id, affix.get("encoded_id"))
                )
                targets.append(
                    {
                        "kind": INPUT_KIND_AFFIX_ROLL,
                        "instance_path": affix_path,
                        "item_instance_id": iid,
                        "item_label": item_label,
                        "channel": channel,
                        "affix_entity_id": entity_id,
                        "affix_name": affix_name,
                        "source_tier": _first(
                            affix.get("source_tier"), _dict(affix.get("tier")).get("source_tier")
                        ),
                        "label": _first(line.get("text"), affix_name),
                        "range_text": _range_text_for_affix(line, affix),
                        "variables": variables,
                        "value_status": VALUE_STATUS_RANGE,
                        "suggested_command": _suggested_affix_command(build_id, affix_path),
                        "suggested_variable_commands": [
                            _suggested_affix_display_command(
                                build_id, affix_path, variable["affix_value_index"]
                            )
                            for variable in variables
                        ],
                    }
                )

        # -- implicit definitions without an actual value ----------------------
        implicit_block = _dict(instance.get("implicit_rolls"))
        for implicit_index, raw_result in enumerate(_list(implicit_block.get("values"))):
            result = _dict(raw_result)
            entries = [_dict(value) for value in _list(result.get("values"))]
            if entries and all(value.get("value_numeric") is not None for value in entries):
                continue
            if entries and all(value.get("status") == "resolved" for value in entries):
                continue
            line = _dict(
                _find_line(
                    lines, section="implicit", diagnostics={"implicit_index": implicit_index}
                )
            )
            targets.append(
                {
                    "kind": INPUT_KIND_IMPLICIT_ROLL,
                    "instance_path": item_source_path,
                    "item_instance_id": iid,
                    "item_label": item_label,
                    "index": implicit_index,
                    "label": _first(line.get("text"), "implicit"),
                    "readable_text": _first(line.get("text")),
                    "range_text": _first(line.get("range_text")),
                    "value_status": VALUE_STATUS_MISSING,
                    "suggested_command": _suggested_implicit_command(
                        build_id, item_source_path, implicit_index
                    ),
                }
            )

        # -- unique mods missing a roll, grouped by rollId ---------------------
        unique_block = _dict(instance.get("unique_rolls"))
        definitions = _unique_definitions(unique_block, _list(unique_block.get("values")))
        grouped: dict[int, dict] = {}
        for raw_definition in definitions:
            definition = _dict(raw_definition)
            if not definition.get("can_roll"):
                continue
            if definition.get("roll_status") not in (ROLL_ABSENT, ROLL_NULL):
                continue
            restored_definition = _dict(definition.get("restored"))
            if restored_definition.get("value_numeric") is not None:
                continue
            if str(restored_definition.get("value_source") or "").startswith("local_"):
                continue
            roll_id = definition.get("roll_id")
            if roll_id is None:
                continue
            try:
                roll_key = int(roll_id)
            except (TypeError, ValueError):
                continue
            mod_index = definition.get("mod_index")
            line = _dict(
                _find_line(lines, section="unique_mod", diagnostics={"mod_index": mod_index})
            )
            restored = _dict(definition.get("restored"))
            label = _first(line.get("text"), restored.get("stat_name"), f"mod_index {mod_index}")
            bucket = grouped.setdefault(
                roll_key, {"mod_indexes": [], "labels": [], "range_texts": [], "variables": []}
            )
            bucket["mod_indexes"].append(mod_index)
            bucket["labels"].append(label)
            range_text = _first(line.get("range_text"))
            if range_text is not None and range_text not in bucket["range_texts"]:
                bucket["range_texts"].append(range_text)
            percent = (
                str(restored.get("unit") or "") == "percent"
                or int(restored.get("modifier_type") or 0) != 0
                or "%" in str(range_text or "")
                or "%" in str(line.get("text") or "")
            )
            bucket["variables"].append(
                {
                    "mod_index": mod_index,
                    "label": label,
                    "unit": "percent" if percent else "flat",
                    "percent": percent,
                    "range_text": range_text,
                    "raw_value": definition.get("raw_value"),
                    "max_value": definition.get("max_value"),
                }
            )
        for roll_id in sorted(grouped):
            bucket = grouped[roll_id]
            targets.append(
                {
                    "kind": INPUT_KIND_UNIQUE_MOD_ROLL,
                    "instance_path": item_source_path,
                    "item_instance_id": iid,
                    "item_label": item_label,
                    "roll_id": roll_id,
                    "mod_indexes": [
                        mod_index for mod_index in bucket["mod_indexes"] if mod_index is not None
                    ],
                    "labels": bucket["labels"],
                    "label": "; ".join(bucket["labels"]),
                    "range_text": "; ".join(bucket["range_texts"]) or None,
                    "variables": bucket["variables"],
                    "value_status": VALUE_STATUS_RANGE,
                    "suggested_command": _suggested_unique_command(
                        build_id, item_source_path, roll_id
                    ),
                    "suggested_variable_commands": [
                        _suggested_unique_display_command(
                            build_id, item_source_path, variable["mod_index"]
                        )
                        for variable in bucket["variables"]
                        if variable.get("mod_index") is not None
                    ],
                }
            )
    return targets


# ---------------------------------------------------------------------------
# Character / skills / passive tree / scope / notes
# ---------------------------------------------------------------------------
def _ref_block(container) -> dict | None:
    entry = _dict(container)
    if not entry:
        return None
    ref = _dict(entry.get("ref"))
    resolution = _dict(entry.get("resolution"))
    entity = _dict(resolution.get("entity"))
    return {
        "entity_id": _first(ref.get("entity_id"), entity.get("entity_id")),
        "name": _first(ref.get("name"), entity.get("name")),
        "status": entry.get("status"),
    }


def _node_block(node) -> dict:
    node = _dict(node)
    ref = _dict(node.get("ref"))
    return {
        "node_id": node.get("node_id"),
        "name": _first(ref.get("name")),
        "points": node.get("points"),
        "node_type": node.get("node_type"),
        "max_points": node.get("max_points"),
        "explicit_zero": node.get("explicit_zero"),
        "status": node.get("status"),
    }


def _character_block(spec: dict) -> dict:
    character = _dict(_dict(spec).get("character"))
    return {
        "level": character.get("level"),
        "class": _ref_block(character.get("class")),
        "mastery": _ref_block(character.get("mastery")),
    }


def _skills_block(spec: dict) -> dict:
    spec = _dict(spec)
    specializations: list[dict] = []
    for raw_entry in _list(spec.get("specializations")):
        entry = _dict(raw_entry)
        tree = _dict(entry.get("tree"))
        owner = _dict(entry.get("owner_skill"))
        specializations.append(
            {
                "slot_number": entry.get("slot_number"),
                "source_level": entry.get("source_level"),
                "tree": {"entity_id": tree.get("entity_id"), "name": tree.get("name")},
                "owner_skill": {"entity_id": owner.get("entity_id"), "name": owner.get("name")},
                "tree_version": entry.get("tree_version"),
                "catalog_tree_version": entry.get("catalog_tree_version"),
                "node_entries": entry.get("node_entries"),
                "points_total": entry.get("points_total"),
                "nodes": [_node_block(node) for node in _list(entry.get("nodes"))],
            }
        )
    action_bar: list[dict] = []
    for raw_slot in _list(_dict(spec.get("action_bar")).get("slots")):
        slot = _dict(raw_slot)
        ref = _dict(slot.get("ref"))
        action_bar.append(
            {
                "slot": slot.get("slot"),
                "entity_id": _first(ref.get("entity_id"), slot.get("source_id")),
                "name": ref.get("name"),
                "specialization_slot": slot.get("specialization_slot"),
                "status": slot.get("status"),
            }
        )
    active = _dict(_dict(spec.get("context")).get("active_buffs"))
    active_skills = []
    for raw_skill in _list(active.get("skills")):
        ref = _dict(_dict(raw_skill).get("ref"))
        active_skills.append({"entity_id": ref.get("entity_id"), "name": ref.get("name")})
    return {
        "specializations": specializations,
        "action_bar": action_bar,
        "active_buffs": {
            "skills": active_skills,
            "buffs": _list(active.get("buffs")),
            "ailments": _list(active.get("ailments")),
            "note": active.get("note"),
        },
    }


def _passive_tree_block(spec: dict) -> dict:
    passive_tree = _dict(_dict(spec).get("passive_tree"))
    tree = _dict(passive_tree.get("tree"))
    return {
        "tree": {"entity_id": tree.get("entity_id"), "name": tree.get("name")},
        "points_total": passive_tree.get("points_total"),
        "node_entries": passive_tree.get("node_entries"),
        "status": passive_tree.get("status"),
        "nodes": [_node_block(node) for node in _list(passive_tree.get("nodes"))],
    }


def _scope_block(spec: dict) -> dict:
    scope = _dict(_dict(spec).get("scope"))
    if scope:
        return scope
    detected = [
        section
        for section in (
            "character",
            "passive_tree",
            "specializations",
            "action_bar",
            "equipment",
            "idols",
            "blessings",
            "context",
            "progression",
        )
        if _dict(spec).get(section) is not None
    ]
    return {
        "scope_version": None,
        "included_sections": detected,
        "excluded_features": [],
        "notes": [
            "historical revision without a scope block; included sections were detected "
            "from the BuildSpec keys present"
        ],
    }


def _notes(spec: dict) -> list[str]:
    spec = _dict(spec)
    notes = [
        "本导出只重组 BuildSpec v1 中已有的事实（数值、范围、状态、来源路径），"
        "不重新计算游戏公式。",
        "不声明 DPS、战斗结果、角色面板数值或任何跨物品聚合语义；"
        "calculator 必须自行定义并声明这些规则。",
        "value_numeric 是 planner 公式恢复后的数值；display_value / readable_text 是"
        "展示文本，二者不保证逐字一致。",
        "roll.raw 是 0..255 的来源字节；roll.status=absent 表示来源缺失（不是 0），"
        "此时 value_status 为 range/missing。",
        "hidden=true 的内部 unique mod 保留在结构化数据中供 calculator 消费；"
        "玩家界面不单独显示它们。",
        "unresolved_inputs 中的值需要玩家通过本地输入接口"
        "（le_build input set / import + sync）补齐并发布新 revision 后才会成为 actual；"
        "本 revision 不会被就地修改。",
        "stat.resolved=false 表示 BuildSpec 没有完整的 stat 解析（可能只有 "
        "property/tags/specialTag 和 readable_text）；null 不得当作 0。",
        "affix.channel 区分 normal / sealed / primordial / corrupted；"
        "affix.tier_index 是 catalog 归一化 tier 下标（source tier - 1）。",
        "按 2026-11-04 复审 R1 范围，掉落率等非 DPS 效果（来源 property 身份 "
        "propertyList 104 = Increased Drop Rate）已从本 DPS 计算输入中排除"
        "（见 excluded_non_dps / excluded_non_dps_properties / "
        "excluded_non_dps_mod_ids）；原始来源归档与 BuildSpec 可读行保持不变、未删除。",
    ]
    has_readable = any(
        _dict(instance.get("readable")).get("lines")
        for _path, instance in _iter_item_instances(spec)
    )
    if not has_readable:
        notes.append(
            "该 revision 是历史 revision（早于 readable/scope 层）：readable_text 为 null，"
            "部分 unique mod 数据在 BuildSpec 中不存在，导出只包含可得的结构化数据。"
        )
    return notes


# ---------------------------------------------------------------------------
# Public: whole document
# ---------------------------------------------------------------------------
def _resolve_generated_at(spec: dict, generated_at) -> str:
    """Resolve the only timestamp of the document.

    An explicit value always wins (``datetime`` is converted to ISO-8601).  When it
    is omitted the revision's ``published_at`` is used so that re-exporting the same
    immutable revision is byte-identical (contract §5.5); only a revision without a
    published timestamp falls back to the current UTC time.
    """
    if generated_at is not None:
        if isinstance(generated_at, dt.datetime):
            return generated_at.isoformat()
        return str(generated_at)
    revision = _dict(_dict(_dict(spec).get("meta")).get("revision"))
    published_at = _first(revision.get("published_at"))
    if published_at:
        return published_at
    return dt.datetime.now(dt.timezone.utc).isoformat()


def build_calculation_input(spec: dict, *, generated_at=None) -> dict:
    """Build the ``calculation-input-v1`` document from one BuildSpec revision.

    ``spec`` is the immutable BuildSpec v1 dict (as stored in
    ``revisions/<rev>/build.json``).  The function is read-only and deterministic:
    two calls on the same spec produce equal documents, and (with the default
    ``generated_at``) byte-identical JSON.

    Parameters
    ----------
    spec:
        Parsed BuildSpec v1 document.
    generated_at:
        Optional ISO-8601 string (or ``datetime``) to record as the export time.
        Defaults to the revision's ``published_at`` for byte-stable re-exports.

    Returns
    -------
    dict
        Top-level keys per contract §4: ``schema_version``, ``generated_at``,
        ``build_id``, ``revision_id``, ``source``, ``game_version``, ``dataset_id``,
        ``scope``, ``character``, ``skills``, ``passive_tree``, ``items``, ``mods``,
        ``unresolved_inputs``, ``notes`` (plus ``revision_seq``/``content_sha256``
        provenance extras).  The R1 scope adds ``excluded_non_dps`` (excluded mod
        count), ``excluded_non_dps_properties`` (source property identity list) and
        ``excluded_non_dps_mod_ids`` (stable mod ids of excluded effects).
    """
    if not isinstance(spec, dict):
        raise TypeError(f"spec must be a BuildSpec dict, got {type(spec).__name__}")
    meta = _dict(spec.get("meta"))
    revision = _dict(meta.get("revision"))
    catalog = _dict(spec.get("catalog"))

    items: list[dict] = []
    excluded_mods: list[dict] = []
    for path, instance in _iter_item_instances(spec):
        per_item_excluded: list[dict] = []
        items.append(_item_document(instance, path, per_item_excluded))
        excluded_mods.extend(per_item_excluded)
    mods = [mod for item in items for mod in item["mods"]]
    non_dps = _excluded_non_dps_summary(excluded_mods)

    return {
        "schema_version": CALCULATION_SCHEMA_VERSION,
        "generated_at": _resolve_generated_at(spec, generated_at),
        "build_id": _build_id(spec),
        "revision_id": _first(revision.get("revision_id"), meta.get("revision_id")),
        "revision_seq": revision.get("revision_seq"),
        "content_sha256": revision.get("content_sha256"),
        "source": _first(meta.get("source"), "letools"),
        "game_version": catalog.get("target_game_version"),
        "dataset_id": catalog.get("dataset_id"),
        "scope": _scope_block(spec),
        "character": _character_block(spec),
        "skills": _skills_block(spec),
        "passive_tree": _passive_tree_block(spec),
        "items": items,
        "mods": mods,
        "excluded_non_dps": non_dps["count"],
        "excluded_non_dps_properties": non_dps["properties"],
        "excluded_non_dps_mod_ids": non_dps["mod_ids"],
        "unresolved_inputs": unresolved_inputs(spec),
        "notes": _notes(spec),
    }


__all__ = [
    "CALCULATION_SCHEMA_VERSION",
    "VALUE_STATUSES",
    "VALUE_SOURCES",
    "VALUE_STATUS_ACTUAL",
    "VALUE_STATUS_RANGE",
    "VALUE_STATUS_CONSTANT",
    "VALUE_STATUS_MISSING",
    "VALUE_STATUS_UNRESOLVED",
    "VALUE_SOURCE_SOURCE",
    "VALUE_SOURCE_LOCAL_INPUT",
    "VALUE_SOURCE_LOCAL_VALUE_OVERRIDE",
    "SOURCE_AFFIX",
    "SOURCE_IMPLICIT",
    "SOURCE_UNIQUE_MOD",
    "CHANNEL_IMPLICIT",
    "CHANNEL_UNIQUE",
    "AFFIX_CHANNELS",
    "KIND_MOD",
    "KIND_DESCRIPTION",
    "INPUT_KIND_AFFIX_ROLL",
    "INPUT_KIND_UNIQUE_MOD_ROLL",
    "INPUT_KIND_IMPLICIT_ROLL",
    "NON_DPS_PROPERTIES",
    "iter_mods",
    "unresolved_inputs",
    "build_calculation_input",
]
