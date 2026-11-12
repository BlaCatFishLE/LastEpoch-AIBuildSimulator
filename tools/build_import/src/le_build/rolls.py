"""Versioned equipment value restoration for the LE Tools build importer (P2-4).

This module ports the *roll value formula* used by the archived LE Tools planner
runtime script.  It is pure and stdlib-only: it never touches the network, never
opens the catalog database itself, and never evaluates any downloaded JavaScript.

What this module claims
-----------------------
It restores the numeric value a roll (`r` / `ir[i]` / `ur[rollId]`, all integers
in ``0..255``) produces for the range declared by the game data, using the exact
archived arithmetic (``rrlZm``/``rrl7o``/``rrl6o``/``rrlb``).  It also records
which patch of the archival script the rule came from, the rounding branch that
was taken, the stat/property association, the display unit and display rounding
flags.

What this module explicitly does NOT claim
------------------------------------------
No combat semantics, no DPS, no buff/condition resolution, no aggregation across
items, no "the character sheet looks like X".  A restored value says only:
"for this source roll and these declared endpoints, the archived planner would
compute this number".  An absent source roll is *not* invented; the module either
proves the value is constant for the whole declared range or leaves
``value_numeric`` as ``None`` and stores a clearly labelled derived display value
(the planner's average branch) under ``effective_value_policy``.

Evidence
--------
Archived file: ``archive://planner-runtime.js``
(sha256 ``3b962b5d...``; the same hash is recorded in ``capture-meta.json``).
That capture-meta file records only the *codec* expression offsets
(145539..149610); the roll-rule offsets below were located by this work in the
same byte-identical archive and are re-checked by ``tests/test_rolls.py``.
The roll-rule block is one contiguous run of functions at character offsets
433367..435028: ``rrl5o``, ``rrlM``, ``rrlZm``, ``rrl7o``, ``rrl6o``.

Catalog contract
----------------
``catalog`` is the Lead's read-only catalog reader (``le_build.catalog.Catalog``).
Only the following methods are used; each returns plain dicts (or ``None``):

* ``catalog.affix_tiers(affix_entity_id) -> list[dict]``
  keys: ``tier_index``, ``roll_min``, ``roll_max``, ``extra_rolls`` (raw JSON
  string or ``None``), ``extensions`` (raw JSON string or ``None``)
* ``catalog.affix(affix_entity_id) -> dict | None``
  keys: ``entity_id``, ``canonical_name``/``affix_display_name``, ``extensions``,
  ``affix_slot``; ``extensions`` JSON holds ``affixProperties``.
* ``catalog.stat_by_property(property_id, tags, special_tag) -> dict | None``
  keys: ``stat_id``, ``name``, ``unit``, ``extensions`` (raw JSON string).
* ``catalog.item(entity_id) -> dict | None``
  keys: ``entity_id``, ``canonical_name``, ``item_category``, ``item_type``,
  ``base_type``, ``implicit_text`` (raw JSON string), ``extensions``.
* ``catalog.unique_mods(unique_entity_id) -> list[dict]``
  parsed ``items.extensions.mods``.
* ``catalog.names(entity_id) -> dict`` (only used if present, for diagnostics)

Return shape
------------
``restore_affix_value`` returns one dict.  ``restore_implicit_values`` and
``restore_unique_values`` return a list of dicts (one per implicit / unique mod),
each shaped like the single dict::

    {
      "status": "resolved" | "partial" | "unresolved",
      "roll_status": "present" | "absent" | "null",
      "raw_roll": int | None,
      "values": [ {value dict}, ... ],
      "effective_value_policy": dict | None,
      "evidence": dict,
      "reason": str | None,     # short failure code, mirrors evidence.failure_reason
    }

Each value dict has the documented keys ``property``, ``stat_id``, ``stat_name``,
``unit``, ``modifier_type``, ``direction``, ``roll_min``, ``roll_max``,
``value_numeric``, ``display_value``, ``round_added_to_int``, ``derivation`` and
a few traceability extras (``tags``, ``special_tag``, ``status``, ``branch``,
``stat_resolution``, and for derived-only values ``derived_display_value``).

Conventions that the importer must respect
------------------------------------------
* ``raw_roll`` absent from the source JSON -> pass :data:`MISSING_ROLL`.
  It is a distinct sentinel, *not* ``0`` and *not* ``None``.
* ``"r": null`` -> pass ``None`` (``roll_status == "null"``).  The site formula
  throws on a null roll (``rrlZm``), so null must never be fed to the formula;
  it follows the same "no roll provided" display policy as absent, but the two
  states remain distinguishable.
* ``r == 0`` is a valid, present endpoint and must never be treated as falsy.
* ``tier_index`` is the *normalized* catalog tier (source tier - 1); the archived
  ``Ra`` function uses ``affix.tiers[tier - 1]``.
* ``ir`` is aligned with the base item's implicit definitions (0-based JSON;
  the reference Lua importer reads ``itemData["ir"][i]`` with Lua's 1-based i).
* ``ur`` is indexed by the unique mod's own ``rollId`` (0-based JSON; the
  reference Lua importer reads ``itemData["ur"][uniqueBase.rollIds[i] + 1]``).
  Several mods may share one ``rollId``; they all read the same array slot.
"""

from __future__ import annotations

import json
import math

CODE_RULE_VERSION = "letools-roll-rules-v1"

#: Sentinel for "the source JSON object had no roll key at all".
#: Never compare it by truthiness against ``0`` or ``None``.
class _MissingRoll:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - repr only
        return "MISSING_ROLL"


MISSING_ROLL = _MissingRoll()

#: Internal sentinel used to distinguish "4th positional argument not supplied"
#: (caller used the 3-argument catalog-first order) from "explicitly supplied None"
#: (caller passed a source null as the 4th argument in catalog-first order).
_UNSET = object()

#: Affix instance channels in the LE Tools save payload.  All four are created by
#: the same archived ``Ra``/``Cc`` path and carry ``{id, tier, r?}``.
AFFIX_ROLL_CHANNELS = ("affixes", "sealedAffix", "primordialAffix", "corruptedAffix")

#: Source field names for the other two roll arrays.
IMPLICIT_ROLLS_FIELD = "ir"
UNIQUE_ROLLS_FIELD = "ur"

#: Symbols ported from the archived runtime.  ``offset`` is the character offset of
#: ``"function <name>("`` in planner-runtime.js; re-verified by the test suite.
EVIDENCE = {
    "code_rule_version": CODE_RULE_VERSION,
    "runtime": {
        "file": "archive://planner-runtime.js",
        "url": "https://www.lastepochtools.com/planner/js/planner.js?1793429712",
        "sha256": "3b962b5d3281e4ddd1651cb3f3b82bcf8569b50164777d0f9061541c93dd0b86",
        "capture_meta": "archive://capture-meta.json",
        "roll_rule_block": {"start": 433367, "end": 435028},
    },
    "symbols": {
        "rrl5o": {"offset": 433367, "role": "percent display: Math.round(100*v)+'%'"},
        "rrlM": {
            "offset": 433414,
            "role": "quality-mode dispatch: Min/Max/Average/Roll; missing roll -> rrl6o",
        },
        "rrlZm": {
            "offset": 433668,
            "role": "per-mod roll entry: canRoll gate, null roll throws, g=property.roundAddedToInt",
        },
        "rrl7o": {
            "offset": 433894,
            "role": "roll -> value formula: roll/255, ascending/inverted, g 0/1/2/3 branches",
        },
        "rrl6o": {
            "offset": 434760,
            "role": "average branch when no roll: rrlb((value+maxValue)/2) with g rounding",
        },
        "rrlb": {
            "offset": 3025,
            "role": "round-half-to-even helper (NOT Math.round); used for every endpoint/display rounding",
        },
        "rrlFj": {
            "offset": 384272,
            "role": "item-card rolled-values path: implicits ir[i], uniques ur[rollId], affix mods via rrlZm",
        },
        "rrl3o": {
            "offset": 431340,
            "role": "stat-building path: base implicits value/maxValue with ir[index]; unique mods maxValue clamp + ur[rollId]",
        },
        "Ra": {
            "offset": 100401,
            "role": "affix instance -> mods: tiers[tier-1], rolls paired with affixProperties by index, one r for the affix",
        },
        "$d": {"offset": 52541, "role": "affix tier range lookup + display endpoint normalization"},
        "nb": {"offset": 54082, "role": "endpoint display normalization (only for the Ff integer properties)"},
        "$a": {"offset": 46472, "role": "rounding-rule selector: type 0 -> roundAddedToInt, type 1 -> 0, else roundingForMore"},
        "ce": {"offset": 57123, "role": "property resolver: AbilityProperty=58, PlayerProperty=98, else coreDB.propertyList"},
    },
    "reference_importer": {
        "file": "archive://Build.lua",
        "line_ur": 1206,
        "line_ir": 1221,
        "note": "ur indexed via rollIds[i]+1 (Lua 1-based => Python 0-based ur[rollId]); ir indexed 1-based by implicit position",
    },
    "provenance_notes": [
        "capture-meta.json records the planner-runtime sha256 and only the codec expression offsets (145539..149610).",
        "The roll-rule offsets above were located locally in the same archived file; the test suite recomputes sha256 and every offset.",
        "The archived script is protocol evidence, not a dependency: no part of it is downloaded or executed by this module.",
    ],
}

# Status vocabulary (kept explicit so a consumer never has to guess).
STATUS_RESOLVED = "resolved"
STATUS_PARTIAL = "partial"
STATUS_UNRESOLVED = "unresolved"

ROLL_PRESENT = "present"
ROLL_ABSENT = "absent"
ROLL_NULL = "null"

DIRECTION_ASCENDING = "ascending"
DIRECTION_INVERTED = "inverted"
DIRECTION_CONSTANT = "constant"

#: Stat display-unit values observed in the catalog.
_UNIT_FLAT = "flat"
_UNIT_PERCENT = "percent"


class RollValueError(ValueError):
    """Raised for a malformed roll value (non-integer, outside 0..255, ...)."""


# ---------------------------------------------------------------------------
# JavaScript number helpers
# ---------------------------------------------------------------------------
def _js_round(value: float) -> float:
    """``Math.round``: ties round toward +Infinity."""
    return math.floor(value + 0.5)


def rrlb(value: float) -> float:
    """Port of archived ``rrlb`` (planner-runtime.js offset 3025).

    The archived helper is **round-half-to-even** (banker's rounding), not
    ``Math.round``::

        function rrlb(a){var b=b||0;a*=Math.pow(10,b);const c=Math.round(a);
          return(.5===Math.abs(a)%1?0===c%2?c:c-1:c)/Math.pow(10,b)}

    ``b`` is a hoisted, never-passed local (always 0), so the shift is 0.  This
    difference matters for exactly representable ties, e.g. 12.5 -> 12,
    37.5 -> 38, 62.5 -> 62 (while ``Math.round(62.5) == 63``).
    """
    numeric = value * 1.0
    rounded = _js_round(numeric)
    if math.fmod(abs(numeric), 1.0) == 0.5:
        return rounded if math.fmod(rounded, 2.0) == 0 else rounded - 1
    return rounded


def _na_round(value: float, rounding_rule: int) -> float:
    """Port of the archived display helper ``na`` (rounding rule 0/1/2/3).

    Rule 0 -> two decimals, 1 -> integer, 2 -> one decimal, 3 -> three decimals.
    (``rrl7o``'s ``g`` values use the same numbering for its endpoint rounding.)
    """
    rule = int(rounding_rule or 0)
    if rule == 1:
        return rrlb(value)
    if rule == 2:
        return rrlb(10 * value) / 10
    if rule == 3:
        return rrlb(1000 * value) / 1000
    return rrlb(100 * value) / 100


def _round0(value: float) -> float:
    return rrlb(value)


def _round1(value: float) -> float:
    return rrlb(10 * value) / 10


def _round2(value: float) -> float:
    return rrlb(100 * value) / 100


def _round3(value: float) -> float:
    return rrlb(1000 * value) / 1000


def _fmt_number(value) -> str | None:
    """Format a restored/derived number without float noise, never using exponent."""
    if value is None:
        return None
    text = f"{float(value):.12f}".rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


# ---------------------------------------------------------------------------
# Formula port
# ---------------------------------------------------------------------------
def rrl7o(
    round_added_to_int: int,
    modifier_type: int,
    value: float,
    max_value: float | None,
    roll: int,
) -> float:
    """Port of archived ``rrl7o`` (offset 433894): roll -> restored number.

    ``value``/``max_value`` are the *definition order* endpoints (source
    ``rolls[i].min``/``rolls[i].max``); ``max_value <= value`` means the declared
    range is inverted and the site takes its inverted branch.  ``roll`` is the
    0..255 byte; the archived code divides it by 255 *before* choosing a branch.
    """
    if max_value is None:
        max_value = value
    r = roll / 255.0
    if value <= max_value:
        if modifier_type != 0:
            c = _round2(value)
            d = _round2(max_value)
            return min(math.floor(100 * ((d + 0.01 - c) * r + c + 0.001)) / 100, d)
        if round_added_to_int == 1:
            c = _round0(value)
            d = _round0(max_value)
            return min(math.floor((d + 1 - c) * r + c), d)
        if round_added_to_int == 2:
            c = _round1(value)
            d = _round1(max_value)
            return min(math.floor(10 * ((d + 0.1 - c) * r + c)) / 10, d)
        if round_added_to_int == 3:
            c = _round3(value)
            d = _round3(max_value)
            return min(math.floor(1000 * ((d + 0.001 - c) * r + c)) / 1000, d)
        c = _round2(value)
        d = _round2(max_value)
        return min(math.floor(100 * ((d + 0.01 - c) * r + c + 0.001)) / 100, d)
    # Inverted range (definition min > definition max).
    if modifier_type == 0:
        if round_added_to_int == 1:
            d = _round0(max_value)
            c = _round0(value)
            return max(math.ceil((c - d - 1) * r + d), c)
        if round_added_to_int == 2:
            c = _round1(value)
            d = _round1(max_value)
            return max(math.ceil(10 * ((d - 0.1 - c) * r + c)) / 10, d)
        if round_added_to_int == 3:
            c = _round3(value)
            d = _round3(max_value)
            return max(math.ceil(1000 * ((d - 0.001 - c) * r + c)) / 1000, d)
    c = _round2(value)
    d = _round2(max_value)
    return max(math.ceil(100 * ((d - 0.01 - c) * r + c)) / 100, d)


def rrl6o(
    round_added_to_int: int,
    modifier_type: int,
    value: float,
    max_value: float | None,
) -> float:
    """Port of archived ``rrl6o`` (offset 434760): the *average* display branch.

    Used by the planner when no roll is available (``rrlM`` Roll mode + null roll,
    or Average quality mode).  It is a display derivation, not a source roll.
    """
    if max_value is None:
        max_value = value
    average = (value + max_value) / 2
    if modifier_type == 0:
        if round_added_to_int == 1:
            return _round0(average)
        if round_added_to_int == 2:
            return _round1(average)
        if round_added_to_int == 3:
            return _round3(average)
    return _round2(average)


def _direction(value: float, max_value: float) -> str:
    if value == max_value:
        return DIRECTION_CONSTANT
    return DIRECTION_ASCENDING if value <= max_value else DIRECTION_INVERTED


def _branch_name(round_added_to_int: int, modifier_type: int, direction: str) -> str:
    if direction == DIRECTION_CONSTANT:
        return "constant_range"
    if direction == DIRECTION_ASCENDING:
        if modifier_type != 0:
            return "ascending.type_ne_0.round2"
        return f"ascending.round_added_to_int_{int(round_added_to_int)}"
    if modifier_type == 0 and int(round_added_to_int) in (1, 2, 3):
        return f"inverted.round_added_to_int_{int(round_added_to_int)}"
    return "inverted.general.round2"


def _as_roll(raw_roll) -> tuple[str, int | None]:
    """Classify a raw roll: ('present', int) / ('absent', None) / ('null', None)."""
    if raw_roll is MISSING_ROLL:
        return ROLL_ABSENT, None
    if raw_roll is None:
        return ROLL_NULL, None
    if isinstance(raw_roll, bool) or not isinstance(raw_roll, int):
        raise RollValueError(f"roll must be an integer 0..255, got {type(raw_roll).__name__}: {raw_roll!r}")
    if not 0 <= raw_roll <= 255:
        raise RollValueError(f"roll outside the source byte range 0..255: {raw_roll!r}")
    return ROLL_PRESENT, raw_roll


# ---------------------------------------------------------------------------
# Small JSON / catalog helpers
# ---------------------------------------------------------------------------
def _json_obj(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if math.isnan(numeric) or math.isinf(numeric):
            return None
        return numeric
    return None


def _int_flag(mapping: dict, key: str, fallback=None) -> int:
    value = mapping.get(key)
    if value is None:
        value = fallback
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _stat_extensions(stat: dict | None) -> dict:
    if not stat:
        return {}
    return _json_obj(stat.get("extensions"))


def round_added_to_int(stat: dict | None) -> int:
    """``stats.extensions.roundAddedToInt`` for a resolved stat; 0 when unresolved.

    The archived ``rrlZm`` defaults ``g`` to 0 when ``getProperty`` returns null,
    so an unresolved property is not an error for the numeric formula: it selects
    the two-decimal fallback branch.  Callers still mark such a value ``partial``
    because the unit/rounding association itself is not proven.
    """
    extensions = _stat_extensions(stat)
    value = extensions.get("roundAddedToInt")
    if value is None and stat:
        value = stat.get("round_added_to_int")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _display_flags(stat: dict | None) -> dict:
    extensions = _stat_extensions(stat)
    if stat:
        for key, ext_key, parsed_key in (
            ("display_added_as_percentage", "displayAddedAsPercentage", "display_added_as_percentage"),
            ("display_as_percentage_of", "displayAsPercentageOf", "display_as_percentage_of"),
            ("display_added_as_tenth", "displayAddedAsTenthOfValue", "display_added_as_tenth"),
            ("rounding_for_more", "roundingForMore", None),
            ("dont_display_plus", "dontDisplayPlus", "dont_display_plus"),
            ("display_as_added_to", "displayAsAddedTo", "display_as_added_to"),
            ("name_includes_modifier", "nameIncludesModifier", "name_includes_modifier"),
            ("hide_modifier_value", "hideModifierValue", "hide_modifier_value"),
        ):
            # The catalog adapter applies archived getProperty display overrides onto the
            # parsed stat dict; those parsed values must win over the raw extension copy.
            if parsed_key and stat.get(parsed_key) is not None:
                extensions[key] = stat.get(parsed_key)
            elif extensions.get(ext_key) is not None:
                extensions.setdefault(key, extensions.get(ext_key))
    return {
        "display_added_as_percentage": _int_flag(extensions, "display_added_as_percentage"),
        "display_as_percentage_of": _int_flag(extensions, "display_as_percentage_of"),
        "display_added_as_tenth": _int_flag(extensions, "display_added_as_tenth"),
        "rounding_for_more": _int_flag(extensions, "rounding_for_more"),
        "dont_display_plus": _int_flag(extensions, "dont_display_plus"),
        "display_as_added_to": _int_flag(extensions, "display_as_added_to"),
        "name_includes_modifier": _int_flag(extensions, "name_includes_modifier"),
        "hide_modifier_value": _int_flag(extensions, "hide_modifier_value"),
    }


def _display_rounding_rule(stat: dict | None, modifier_type: int) -> int:
    """Port of archived ``$a``: which precision the display helper rounds to.

    ``type 0 -> stats.roundAddedToInt``; ``type 1 -> 0`` (two decimals);
    otherwise ``stats.roundingForMore``.
    """
    flags = _display_flags(stat)
    if int(modifier_type or 0) == 0:
        return round_added_to_int(stat)
    if int(modifier_type or 0) == 1:
        return 0
    return flags["rounding_for_more"]


def _is_percentage_stat(stat: dict | None, modifier_type: int) -> bool:
    """The archived ``kb`` percentage test: ``0 != modifier_type`` OR percent flags.

    Fixed from the 2026-11-03 review (P1-1): the planner formats every non-Added
    modifier (increased/more) as a percentage even when the property itself carries no
    ``displayAddedAsPercentage`` flag, e.g. Mana ``0.11`` -> ``11%`` for type 1.
    """
    if int(modifier_type or 0) != 0:
        return True
    if stat is None:
        return False
    flags = _display_flags(stat)
    return bool(flags["display_added_as_percentage"] or flags["display_as_percentage_of"])


def _display_decimals(value, stat: dict | None, modifier_type: int) -> bool:
    """Archived ``kb``/``Za`` decimal flag: tiny fractions or ``$a == 3`` show 2 decimals."""
    try:
        magnitude = abs(100 * float(value))
    except (TypeError, ValueError):
        magnitude = 0.0
    if magnitude < 1:
        return True
    return _display_rounding_rule(stat, modifier_type) == 3


def _display_endpoint_number(value, stat: dict | None, modifier_type: int) -> str:
    """One endpoint of a range/value, without the percent sign (``Za``/``na`` port)."""
    if _is_percentage_stat(stat, modifier_type):
        decimals = _display_decimals(value, stat, modifier_type)
        number = _round2(100 * float(value)) if decimals else rrlb(100 * float(value))
        return _fmt_number(number)
    flags = _display_flags(stat)
    if flags["display_added_as_tenth"]:
        decimals = _display_decimals(value, stat, modifier_type)
        number = _round2(10 * float(value)) if decimals else rrlb(100 * float(value)) / 10
        return _fmt_number(number)
    return _fmt_number(_na_round(float(value), round_added_to_int(stat)))


def display_value(value, stat: dict | None, modifier_type: int) -> str | None:
    """Reconstruct the planner's item-card display string for one restored value.

    Rules (all evidenced in the archived script, offsets listed in :data:`EVIDENCE`):

    * the percentage branch is taken when ``modifier_type != 0`` (increased/more) **or**
      ``displayAddedAsPercentage`` / ``displayAsPercentageOf`` is set (review P1-1 fix);
      the number is ``Za(100*value, t)`` where ``t`` forces two decimals for tiny
      fractions or ``$a==3``;
    * ``displayAddedAsTenthOfValue`` (Added only) uses the ``tb`` helper (``<n>%`` with
      one/two decimals);
    * everything else uses ``na(value, roundAddedToInt)``.

    This is display formatting only.  It does not assert how stats aggregate.  If the
    stat association is unresolved, a percent guess is still only made when the modifier
    type itself proves it is a percentage; otherwise the display string is unknown and
    ``None`` is returned instead of guessing a unit.
    """
    if value is None:
        return None
    if stat is None and int(modifier_type or 0) == 0:
        return None
    flags = _display_flags(stat)
    if _is_percentage_stat(stat, modifier_type):
        if flags["display_added_as_tenth"] and int(modifier_type or 0) == 0:
            decimals = _display_decimals(value, stat, modifier_type)
            number = _round2(10 * float(value)) if decimals else rrlb(100 * float(value)) / 10
            return f"{_fmt_number(number)}%"
        return f"{_display_endpoint_number(value, stat, modifier_type)}%"
    return _display_endpoint_number(value, stat, modifier_type)


def display_range(value, max_value, stat: dict | None, modifier_type: int) -> str | None:
    """Range display for a declared roll range, e.g. ``61%–75%`` or ``39–91``.

    Both endpoints use the same percentage decision as :func:`display_value`; a
    constant range collapses to a single value.  ``None`` when either endpoint is
    missing.
    """
    if value is None or max_value is None:
        return None
    try:
        low = float(value)
        high = float(max_value)
    except (TypeError, ValueError):
        return None
    first = display_value(low, stat, modifier_type)
    second = display_value(high, stat, modifier_type)
    if first is None or second is None:
        return None
    if first == second:
        return first
    return f"{first}–{second}"


def display_semantic(modifier_type: int, value, max_value, stat: dict | None) -> dict:
    """Archived ``Ea`` template selection (base/of/to/increased/reduced/more/less).

    Returns ``{"template", "plus", "value", "max_value"}`` where the numeric endpoints
    may be sign-flipped for reduced/less exactly as the planner does before formatting.
    """
    modifier_type = int(modifier_type or 0)
    try:
        numeric_value = float(value) if value is not None else None
    except (TypeError, ValueError):
        numeric_value = None
    try:
        numeric_max = float(max_value) if max_value is not None else None
    except (TypeError, ValueError):
        numeric_max = None
    flags = _display_flags(stat)
    result = {
        "template": "base",
        "plus": False,
        "value": numeric_value,
        "max_value": numeric_max,
    }
    if numeric_value is None:
        return result
    if modifier_type == 0:
        if flags["display_as_percentage_of"]:
            result["template"] = "of"
        elif numeric_value > 0 and not flags["dont_display_plus"]:
            result["plus"] = True
            if flags["display_as_added_to"]:
                result["template"] = "to"
        elif flags["display_as_added_to"]:
            result["template"] = "to"
    elif modifier_type == 1:
        negative = numeric_value < 0 and (numeric_max is None or numeric_max < 0)
        if negative:
            result["value"] = -numeric_value
            result["max_value"] = -numeric_max if numeric_max is not None else None
            if not flags["name_includes_modifier"]:
                result["template"] = "reduced"
        elif not flags["name_includes_modifier"]:
            result["template"] = "increased"
    else:
        negative = numeric_value < 0 and (numeric_max is None or numeric_max < 0)
        if negative:
            result["value"] = -numeric_value
            result["max_value"] = -numeric_max if numeric_max is not None else None
            result["template"] = "less"
        else:
            result["template"] = "more"
    return result


def _stat_for(catalog, property_id, tags: int, special_tag: int) -> dict | None:
    lookup = getattr(catalog, "stat_by_property", None)
    if lookup is None:  # pragma: no cover - defensive, contract violation
        return None
    try:
        return lookup(property_id, tags=tags, special_tag=special_tag)
    except TypeError:
        return lookup(property_id, tags, special_tag)


def _stat_trace(stat: dict | None, special_tag: int = 0) -> dict:
    if not stat:
        return {"stat_id": None, "stat_name": None, "unit": None, "stat_resolution": "unresolved"}
    display_names = stat.get("special_tag_display") or {}
    display_name = display_names.get(str(int(special_tag or 0)))
    return {
        "stat_id": stat.get("stat_id"),
        "stat_name": display_name or stat.get("name"),
        "unit": stat.get("unit"),
        "stat_resolution": stat.get("property_resolution") or "catalog.stat_by_property",
    }


def _evidence(catalog, extra: dict) -> dict:
    evidence = {
        "code_rule_version": CODE_RULE_VERSION,
        "runtime_sha256": EVIDENCE["runtime"]["sha256"],
        "runtime_file": EVIDENCE["runtime"]["file"],
        "roll_rule_block": dict(EVIDENCE["runtime"]["roll_rule_block"]),
        "symbols": {
            name: EVIDENCE["symbols"][name]["offset"]
            for name in ("rrlb", "rrlM", "rrlZm", "rrl7o", "rrl6o", "rrl5o")
        },
        "dataset_id": getattr(catalog, "dataset_id", None),
    }
    evidence.update(extra)
    return evidence


# ---------------------------------------------------------------------------
# One range -> one value
# ---------------------------------------------------------------------------
def _restore_range(
    *,
    value,
    max_value,
    raw_roll,
    modifier_type: int,
    stat: dict | None,
    special_tag: int = 0,
) -> dict:
    """Restore one declared range against one raw roll (or absent/null)."""
    numeric_value = _number(value)
    numeric_max = _number(max_value) if max_value is not None else None
    if numeric_max is None:
        numeric_max = numeric_value
    if numeric_value is None or numeric_max is None:
        raise RollValueError(f"range endpoints must be finite numbers, got min={value!r} max={max_value!r}")
    g = round_added_to_int(stat)
    modifier_type = int(modifier_type or 0)
    direction = _direction(numeric_value, numeric_max)
    branch = _branch_name(g, modifier_type, direction)
    roll_status, roll = _as_roll(raw_roll)

    policy = None
    derived_value = None
    if roll_status in (ROLL_ABSENT, ROLL_NULL):
        if direction == DIRECTION_CONSTANT:
            restored = rrl6o(g, modifier_type, numeric_value, numeric_max)
            status = STATUS_RESOLVED
            derivation = (
                "no source roll: declared range is constant, so every roll yields "
                "the same number; derived via rrl6o (constant average)"
            )
        else:
            restored = None
            derived_value = rrl6o(g, modifier_type, numeric_value, numeric_max)
            status = STATUS_PARTIAL
            derivation = (
                "no source roll: actual value is NOT restored; declared range is kept "
                "and the planner's average branch (rrl6o) is recorded as a derived "
                "display value only"
            )
            policy = {
                "policy_id": "letools-average-branch-v1",
                "labelled": "derived_display_value_not_source_roll",
                "source_roll": roll_status,
                "basis": (
                    "archived rrlM: quality==Roll and roll==null -> rrl6o(mod, value, maxValue); "
                    "rrl6o = rrlb((value+maxValue)/2) rounded by roundAddedToInt"
                ),
                "derived_values": [
                    {
                        "roll_min": numeric_value,
                        "roll_max": numeric_max,
                        "rrl6o_derived_value": derived_value,
                        "stat_rounding_resolved": stat is not None,
                    }
                ],
            }
    else:
        restored = rrl7o(g, modifier_type, numeric_value, numeric_max, roll)
        status = STATUS_RESOLVED
        derivation = (
            f"rrl7o(g={g}, type={modifier_type}, value={_fmt_number(numeric_value)}, "
            f"maxValue={_fmt_number(numeric_max)}, roll={roll}/255) branch {branch}"
        )

    if stat is None and status == STATUS_RESOLVED:
        status = STATUS_PARTIAL
        derivation += (
            "; stat property unresolved: unit and roundAddedToInt association unproven, "
            "g defaulted to 0 exactly as the archived getProperty miss"
        )

    entry = {
        "property": None,
        "stat_id": None,
        "stat_name": None,
        "unit": None,
        "modifier_type": modifier_type,
        "direction": direction,
        "roll_min": numeric_value,
        "roll_max": numeric_max,
        "value_numeric": restored,
        "display_value": display_value(restored, stat, modifier_type),
        "round_added_to_int": g,
        "derivation": derivation,
        "status": status,
        "branch": branch,
        "tags": None,
        "special_tag": None,
        **_stat_trace(stat, special_tag),
    }
    if derived_value is not None:
        entry["derived_display_value"] = derived_value
    return entry


# ---------------------------------------------------------------------------
# Public: pure formula
# ---------------------------------------------------------------------------
def restore_value_from_range(
    minv,
    maxv,
    roll,
    modifier_type,
    round_added_to_int,
) -> dict:
    """Restore one value from a declared range with the archived formula.

    ``minv``/``maxv`` are the *definition order* endpoints, i.e. the source's
    ``rolls[i].min`` and ``rolls[i].max`` (or ``implicitValue``/``implicitMaxValue``
    or unique ``value``/``maxValue``).  They are not necessarily sorted:
    ``minv > maxv`` is the planner's "inverted range" branch.

    ``roll`` may be an integer 0..255, ``None`` (source null), or
    :data:`MISSING_ROLL` (source key absent).  ``round_added_to_int`` is the stat
    rounding flag ``g``.  Returns a dict with ``direction``, ``roll_status``,
    ``raw_roll``, ``value_numeric``, ``roll_min``, ``roll_max``,
    ``derived_display_value`` (absent/null non-constant only),
    ``effective_value_policy``, ``branch``, ``status`` and ``derivation``.

    Raises :class:`RollValueError` for a malformed range or roll.
    """
    numeric_value = _number(minv)
    numeric_max = _number(maxv) if maxv is not None else None
    if numeric_max is None:
        numeric_max = numeric_value
    if numeric_value is None or numeric_max is None:
        raise RollValueError(f"range endpoints must be finite numbers, got min={minv!r} max={maxv!r}")
    g = int(round_added_to_int or 0)
    modifier_type = int(modifier_type or 0)
    direction = _direction(numeric_value, numeric_max)
    branch = _branch_name(g, modifier_type, direction)
    roll_status, roll = _as_roll(roll)

    if roll_status in (ROLL_ABSENT, ROLL_NULL):
        if direction == DIRECTION_CONSTANT:
            value_numeric = rrl6o(g, modifier_type, numeric_value, numeric_max)
            return {
                "status": STATUS_RESOLVED,
                "roll_status": roll_status,
                "raw_roll": None,
                "direction": direction,
                "roll_min": numeric_value,
                "roll_max": numeric_max,
                "modifier_type": modifier_type,
                "round_added_to_int": g,
                "value_numeric": value_numeric,
                "derived_display_value": None,
                "branch": branch,
                "derivation": "constant range; value is invariant for every roll (rrl6o == any rrl7o roll)",
                "effective_value_policy": None,
            }
        derived = rrl6o(g, modifier_type, numeric_value, numeric_max)
        return {
            "status": STATUS_PARTIAL,
            "roll_status": roll_status,
            "raw_roll": None,
            "direction": direction,
            "roll_min": numeric_value,
            "roll_max": numeric_max,
            "modifier_type": modifier_type,
            "round_added_to_int": g,
            "value_numeric": None,
            "derived_display_value": derived,
            "branch": branch,
            "derivation": "no source roll for a non-constant range; value unresolved, average branch recorded as derived display",
            "effective_value_policy": {
                "policy_id": "letools-average-branch-v1",
                "labelled": "derived_display_value_not_source_roll",
                "source_roll": roll_status,
                "basis": "archived rrlM/rrl6o average branch",
                "derived_values": [
                    {
                        "roll_min": numeric_value,
                        "roll_max": numeric_max,
                        "rrl6o_derived_value": derived,
                        "stat_rounding_resolved": False,
                    }
                ],
            },
        }
    value_numeric = rrl7o(g, modifier_type, numeric_value, numeric_max, roll)
    return {
        "status": STATUS_RESOLVED,
        "roll_status": roll_status,
        "raw_roll": roll,
        "direction": direction,
        "roll_min": numeric_value,
        "roll_max": numeric_max,
        "modifier_type": modifier_type,
        "round_added_to_int": g,
        "value_numeric": value_numeric,
        "derived_display_value": None,
        "branch": branch,
        "derivation": f"rrl7o(g={g}, type={modifier_type}, roll={roll}/255)",
        "effective_value_policy": None,
    }


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------
def _aggregate_status(values: list[dict]) -> str:
    if not values:
        return STATUS_UNRESOLVED
    statuses = [entry["status"] for entry in values]
    if all(status == STATUS_RESOLVED for status in statuses):
        return STATUS_RESOLVED
    if all(status == STATUS_UNRESOLVED for status in statuses):
        return STATUS_UNRESOLVED
    return STATUS_PARTIAL


def _result(status: str, roll_status: str, raw_roll, values: list[dict], policy, evidence: dict) -> dict:
    return {
        "status": status,
        "roll_status": roll_status,
        "raw_roll": raw_roll,
        "values": values,
        "effective_value_policy": policy,
        "evidence": evidence,
        "reason": None,
    }


def _derived_policy(roll_status: str | None, values: list[dict]) -> dict | None:
    """Merge per-value derived (average-branch) policies into one top-level policy."""
    derived = []
    for entry in values:
        if entry.get("derived_display_value") is None:
            continue
        derived.append(
            {
                "property": entry.get("property"),
                "roll_min": entry.get("roll_min"),
                "roll_max": entry.get("roll_max"),
                "rrl6o_derived_value": entry.get("derived_display_value"),
                "stat_rounding_resolved": entry.get("stat_resolution") != "unresolved",
            }
        )
    if not derived:
        return None
    return {
        "policy_id": "letools-average-branch-v1",
        "labelled": "derived_display_value_not_source_roll",
        "source_roll": roll_status,
        "basis": (
            "archived rrlM: quality==Roll and roll==null -> rrl6o(mod, value, maxValue); "
            "average branch, display only"
        ),
        "derived_values": derived,
    }


def _failure(reason: str, roll_status: str, raw_roll, evidence: dict, values=None) -> dict:
    result = _result(
        STATUS_UNRESOLVED,
        roll_status,
        raw_roll,
        values or [],
        None,
        {**evidence, "failure_reason": reason},
    )
    result["reason"] = reason
    return result


def _read_roll(raw_roll) -> tuple[str, int | None, str | None]:
    """Return (roll_status, raw_roll_for_output, invalid_reason)."""
    try:
        roll_status, roll = _as_roll(raw_roll)
    except RollValueError as exc:
        return ROLL_ABSENT, None, str(exc)
    return roll_status, roll, None


# ---------------------------------------------------------------------------
# Public: affixes
# ---------------------------------------------------------------------------
def restore_affix_value(affix_entity_id, tier_index, raw_roll=MISSING_ROLL, catalog=_UNSET) -> dict:
    """Restore one affix *instance* value for its declared tier.

    ``tier_index`` is the normalized catalog index (source tier - 1).  ``raw_roll``
    is the instance's ``r`` byte, :data:`MISSING_ROLL` when the source object has no
    ``r`` key, or ``None`` when the source stores explicit null.  Both argument
    orders ``(affix_entity_id, tier_index, raw_roll, catalog)`` and the task-board
    order ``(catalog, affix_entity_id, tier_index, raw_roll)`` are accepted, so a
    caller cannot silently swap the catalog and the roll.  The catalog-first order
    also accepts the explicit keyword form
    ``restore_affix_value(catalog=..., affix_entity_id=..., tier_index=..., raw_roll=None)``.

    Multi-value affix tiers (``extensions.tier.rolls`` with more than one entry /
    non-null ``extra_rolls``) are paired by index with ``affixProperties`` because
    the archived ``Ra`` iterates ``tiers[tier-1].rolls`` and looks up
    ``affixProperties[same index]``; one instance ``r`` applies to every value.
    If the two lists do not have the same length nothing is guessed: every value is
    returned unresolved with the observed counts in the evidence.
    """
    if not isinstance(affix_entity_id, str):
        # task-board order: (catalog, affix_entity_id, tier_index, raw_roll)
        if catalog is _UNSET:
            # 3-argument form: no roll was supplied -> source absence.
            catalog, affix_entity_id, tier_index, raw_roll = (
                affix_entity_id,
                tier_index,
                raw_roll,
                MISSING_ROLL,
            )
        else:
            # 4-argument form: raw_roll may legitimately be None (source null).
            catalog, affix_entity_id, tier_index, raw_roll = (
                affix_entity_id,
                tier_index,
                raw_roll,
                catalog,
            )
    if catalog is _UNSET or catalog is None:
        raise TypeError("restore_affix_value requires a catalog")
    base_evidence = _evidence(
        catalog,
        {
            "kind": "affix_instance",
            "affix_entity_id": affix_entity_id,
            "tier_index": tier_index,
        },
    )
    roll_status, raw_out, invalid = _read_roll(raw_roll)
    if invalid:
        return _failure("invalid_raw_roll", roll_status, raw_out, base_evidence)
    if isinstance(tier_index, bool) or not isinstance(tier_index, int) or tier_index < 0:
        return _failure("invalid_tier_index", roll_status, raw_out, base_evidence)

    affix = catalog.affix(affix_entity_id)
    tiers = catalog.affix_tiers(affix_entity_id) or []
    tier = next((row for row in tiers if row.get("tier_index") == tier_index), None)
    if affix is None:
        return _failure("affix_not_found", roll_status, raw_out, base_evidence)
    if tier is None:
        return _failure(
            "affix_tier_not_found", roll_status, raw_out, {**base_evidence, "tier_count": len(tiers)}
        )

    tier_extensions = _json_obj(tier.get("extensions"))
    source_inverted = bool(tier_extensions.get("source_inverted_range"))
    tier_body = tier_extensions.get("tier")
    rolls = tier_body.get("rolls") if isinstance(tier_body, dict) else None
    if not isinstance(rolls, list) or not rolls:
        return _failure(
            "tier_roll_definition_missing",
            roll_status,
            raw_out,
            {
                **base_evidence,
                "tier_id": tier.get("tier_id"),
                "note": "extensions.tier.rolls is the only place that keeps range direction; "
                "sorted roll_min/roll_max must not be used to guess it",
            },
        )

    affix_extensions = _json_obj(affix.get("extensions"))
    properties = affix_extensions.get("affixProperties")
    if not isinstance(properties, list) or not properties:
        return _failure(
            "affix_properties_missing",
            roll_status,
            raw_out,
            {**base_evidence, "tier_id": tier.get("tier_id")},
        )
    if len(properties) != len(rolls):
        return _failure(
            "affix_property_roll_count_mismatch",
            roll_status,
            raw_out,
            {
                **base_evidence,
                "tier_id": tier.get("tier_id"),
                "affix_properties": len(properties),
                "tier_rolls": len(rolls),
                "note": "values were not paired by guess or truncated",
            },
        )

    extra_rolls = _json_list(tier.get("extra_rolls"))
    crosscheck_notes = []
    definition_endpoints = []
    for roll_definition in rolls:
        if not isinstance(roll_definition, dict):
            return _failure(
                "tier_roll_definition_malformed",
                roll_status,
                raw_out,
                {**base_evidence, "tier_id": tier.get("tier_id")},
            )
        value = _number(roll_definition.get("min"))
        maximum = _number(roll_definition.get("max"))
        if value is None or maximum is None:
            return _failure(
                "tier_roll_endpoint_missing",
                roll_status,
                raw_out,
                {**base_evidence, "tier_id": tier.get("tier_id")},
            )
        definition_endpoints.append((value, maximum))
    if len(extra_rolls) != max(0, len(rolls) - 1):
        crosscheck_notes.append(
            f"extra_rolls length {len(extra_rolls)} does not match tier rolls length {len(rolls)}"
        )
    if not _endpoints_crosscheck(tier, definition_endpoints):
        crosscheck_notes.append(
            "normalized roll_min/roll_max/extra_rolls disagree with extensions.tier.rolls; "
            "extensions.tier.rolls was used as the authoritative directed range"
        )
    if source_inverted and all(value == maximum for value, maximum in definition_endpoints):
        crosscheck_notes.append("source_inverted_range=true but every declared range is constant")

    values = []
    for index, (property_definition, (value, maximum)) in enumerate(zip(properties, definition_endpoints)):
        if not isinstance(property_definition, dict):
            values.append(
                _unresolved_value_entry(
                    index, None, "affix_property_definition_malformed", roll_status, raw_out
                )
            )
            continue
        property_id = property_definition.get("property")
        tags = _int_flag(property_definition, "tags")
        special_tag = _int_flag(property_definition, "specialTag")
        modifier_type = int(property_definition.get("modifierType") or 0)
        stat = _stat_for(catalog, property_id, tags, special_tag)
        entry = _restore_range(
            value=value,
            max_value=maximum,
            raw_roll=raw_roll,
            modifier_type=modifier_type,
            stat=stat,
            special_tag=special_tag,
        )
        entry["property"] = property_id
        entry["tags"] = tags
        entry["special_tag"] = special_tag
        entry["display_value"] = display_value(entry["value_numeric"], stat, modifier_type)
        values.append(entry)

    status = _aggregate_status(values)
    if crosscheck_notes and status == STATUS_RESOLVED:
        status = STATUS_PARTIAL
    policy = _derived_policy(roll_status, values)
    evidence = {
        **base_evidence,
        "tier_id": tier.get("tier_id"),
        "source_inverted_range": source_inverted,
        "affix_properties": len(properties),
        "tier_rolls": len(rolls),
        "extra_rolls": extra_rolls,
        "crosscheck_notes": crosscheck_notes,
        "affix_display_name": affix.get("affix_display_name") or affix.get("canonical_name"),
        "affix_slot": affix.get("affix_slot"),
        "tier_index_basis": "source tier - 1 (archived Ra: tiers[tier-1])",
    }
    return _result(status, roll_status, raw_out, values, policy, evidence)


def _endpoints_crosscheck(tier: dict, definition_endpoints: list[tuple[float, float]]) -> bool:
    """Cross-check sorted normalized endpoints against the directed definitions."""
    actual = []
    for value in (tier.get("roll_min"), tier.get("roll_max")):
        numeric = _number(value)
        if numeric is not None:
            actual.append(numeric)
    for roll_definition in _json_list(tier.get("extra_rolls")):
        if isinstance(roll_definition, dict):
            for key in ("min", "max"):
                numeric = _number(roll_definition.get(key))
                if numeric is not None:
                    actual.append(numeric)
    expected = sorted([endpoint for pair in definition_endpoints for endpoint in pair])
    return _sorted_endpoints_equal(sorted(actual), expected)


def _sorted_endpoints_equal(left: list[float], right: list[float]) -> bool:
    if len(left) != len(right):
        return False
    return all(math.isclose(a, b, rel_tol=0.0, abs_tol=1e-9) for a, b in zip(left, right))


def _unresolved_value_entry(index, property_id, reason: str, roll_status: str, raw_roll) -> dict:
    return {
        "property": property_id,
        "stat_id": None,
        "stat_name": None,
        "unit": None,
        "modifier_type": None,
        "direction": None,
        "roll_min": None,
        "roll_max": None,
        "value_numeric": None,
        "display_value": None,
        "round_added_to_int": 0,
        "derivation": reason,
        "status": STATUS_UNRESOLVED,
        "branch": None,
        "tags": None,
        "special_tag": None,
        "index": index,
        "stat_resolution": "unresolved",
    }


# ---------------------------------------------------------------------------
# Public: base item implicits
# ---------------------------------------------------------------------------
def restore_implicit_values(base_entity_id, ir_list=None, catalog=None) -> list[dict]:
    """Restore a base item's implicit values from its ``ir`` array.

    ``ir`` is aligned with the base item's implicit definitions (0-based).  A
    shorter ``ir`` array means the later implicits have no source roll (absent);
    a longer one is *not* truncated: surplus entries are returned as unresolved so
    no source content is silently dropped.  Accepts both argument orders
    ``(base_entity_id, ir_list, catalog)`` and ``(catalog, base_entity_id, ir_list)``.
    """
    if not isinstance(base_entity_id, str):
        catalog, base_entity_id, ir_list = base_entity_id, ir_list, catalog
    if catalog is None:
        raise TypeError("restore_implicit_values requires a catalog")
    if ir_list is None:
        ir_list = []
    if not isinstance(ir_list, (list, tuple)):
        return [
            _failure(
                "ir_not_an_array",
                ROLL_ABSENT,
                None,
                _evidence(catalog, {"kind": "implicit", "base_entity_id": base_entity_id}),
            )
        ]
    definition_reader = getattr(catalog, "item_definition", None)
    item_definition = definition_reader(base_entity_id) if definition_reader else None
    evidence = _evidence(
        catalog,
        {
            "kind": "implicit",
            "base_entity_id": base_entity_id,
            "ir_length": len(ir_list),
            "ir_index_basis": "0-based JSON ir[], aligned with implicit definitions; reference Build.lua line 1221 uses ir[i] with Lua 1-based i",
            "definition_provenance": (item_definition or {}).get("implicits_provenance"),
        },
    )
    if item_definition is None:
        return [_failure("item_not_found", ROLL_ABSENT, None, evidence)]
    implicit_definitions = item_definition.get("implicits") or []
    evidence["item_implicit_count"] = len(implicit_definitions)
    results = []
    for index in range(max(len(implicit_definitions), len(ir_list))):
        if index >= len(implicit_definitions):
            roll_status, raw_out, invalid = _read_roll(ir_list[index])
            evidence_extra = {
                **evidence,
                "implicit_index": index,
                "failure_reason": "implicit_definition_missing_for_ir_index"
                if not invalid
                else invalid,
            }
            result = _failure(
                "implicit_definition_missing_for_ir_index" if not invalid else "invalid_raw_roll",
                roll_status,
                raw_out,
                evidence_extra,
                values=[_unresolved_value_entry(index, None, "no implicit definition at this ir index", roll_status, raw_out)],
            )
            results.append(result)
            continue
        definition = implicit_definitions[index]
        implicit_evidence = {**evidence, "implicit_index": index}
        if not isinstance(definition, dict):
            roll_status, raw_out, _ = _read_roll(ir_list[index] if index < len(ir_list) else MISSING_ROLL)
            results.append(
                _failure(
                    "implicit_definition_malformed",
                    roll_status,
                    raw_out,
                    implicit_evidence,
                    values=[_unresolved_value_entry(index, None, "implicit definition is not an object", roll_status, raw_out)],
                )
            )
            continue
        property_id = definition.get("property")
        tags = _int_flag(definition, "tags")
        special_tag = _int_flag(definition, "specialTag")
        modifier_type = int(definition.get("type") or 0)
        value = definition.get("implicitValue")
        maximum = definition.get("implicitMaxValue")
        raw_roll = ir_list[index] if index < len(ir_list) else MISSING_ROLL
        roll_status, raw_out, invalid = _read_roll(raw_roll)
        if invalid:
            results.append(
                _failure(
                    "invalid_raw_roll",
                    roll_status,
                    raw_out,
                    implicit_evidence,
                    values=[_unresolved_value_entry(index, property_id, invalid, roll_status, raw_out)],
                )
            )
            continue
        numeric_value = _number(value)
        numeric_max = _number(maximum) if maximum is not None else numeric_value
        if numeric_value is None or numeric_max is None:
            results.append(
                _failure(
                    "implicit_definition_endpoint_missing",
                    roll_status,
                    raw_out,
                    implicit_evidence,
                    values=[_unresolved_value_entry(index, property_id, "implicit endpoints missing", roll_status, raw_out)],
                )
            )
            continue
        stat = _stat_for(catalog, property_id, tags, special_tag)
        entry = _restore_range(
            value=numeric_value,
            max_value=numeric_max,
            raw_roll=raw_roll,
            modifier_type=modifier_type,
            stat=stat,
            special_tag=special_tag,
        )
        entry["property"] = property_id
        entry["tags"] = tags
        entry["special_tag"] = special_tag
        entry["display_value"] = display_value(entry["value_numeric"], stat, modifier_type)
        policy = _derived_policy(roll_status, [entry])
        results.append(
            _result(
                entry["status"],
                roll_status,
                raw_out,
                [entry],
                policy,
                {**implicit_evidence, "property": property_id},
            )
        )
    return results


# ---------------------------------------------------------------------------
# Public: unique mods
# ---------------------------------------------------------------------------
def restore_unique_values(unique_entity_id, ur_list=None, catalog=None) -> list[dict]:
    """Restore a unique item's mod values from its ``ur`` array.

    ``ur`` is indexed by each mod's own ``rollId`` (0-based).  Duplicate
    ``rollId`` values share one array slot; the archived ``rrl3o`` path reads
    ``r[C.rollId]`` and the reference importer reads ``ur[rollIds[i]+1]`` (Lua
    1-based), both equivalent to ``ur[rollId]`` in Python.  Non-rollable mods
    (``canRoll == false``) are returned as their definitional constant without
    consuming a roll.  Accepts both argument orders ``(unique_entity_id, ur_list,
    catalog)`` and ``(catalog, unique_entity_id, ur_list)``.
    """
    if not isinstance(unique_entity_id, str):
        catalog, unique_entity_id, ur_list = unique_entity_id, ur_list, catalog
    if catalog is None:
        raise TypeError("restore_unique_values requires a catalog")
    if ur_list is None:
        ur_list = []
    if not isinstance(ur_list, (list, tuple)):
        return [
            _failure(
                "ur_not_an_array",
                ROLL_ABSENT,
                None,
                _evidence(catalog, {"kind": "unique_mod", "unique_entity_id": unique_entity_id}),
            )
        ]
    item = catalog.item(unique_entity_id)
    mods = catalog.unique_mods(unique_entity_id) or []
    evidence = _evidence(
        catalog,
        {
            "kind": "unique_mod",
            "unique_entity_id": unique_entity_id,
            "ur_length": len(ur_list),
            "ur_index_basis": "0-based ur[rollId]; archived rrl3o reads r[mod.rollId]; reference Build.lua line 1206 reads ur[rollIds[i]+1] (Lua 1-based)",
        },
    )
    if item is None:
        return [_failure("item_not_found", ROLL_ABSENT, None, evidence)]
    evidence["unique_mod_count"] = len(mods)
    roll_id_counts: dict = {}
    for mod in mods:
        if isinstance(mod, dict) and mod.get("canRoll") and mod.get("rollId") is not None:
            roll_id_counts[mod.get("rollId")] = roll_id_counts.get(mod.get("rollId"), 0) + 1

    results = []
    for index, mod in enumerate(mods):
        mod_evidence = {**evidence, "mod_index": index}
        if not isinstance(mod, dict):
            results.append(
                _failure(
                    "unique_mod_malformed",
                    ROLL_ABSENT,
                    None,
                    mod_evidence,
                    values=[_unresolved_value_entry(index, None, "unique mod is not an object", ROLL_ABSENT, None)],
                )
            )
            continue
        property_id = mod.get("property")
        tags = _int_flag(mod, "tags")
        special_tag = _int_flag(mod, "specialTag")
        modifier_type = int(mod.get("type") or 0)
        hide_in_tooltip = _int_flag(mod, "hideInTooltip")
        can_roll = bool(mod.get("canRoll"))
        roll_id = mod.get("rollId")
        stat = _stat_for(catalog, property_id, tags, special_tag)
        mod_evidence = {
            **mod_evidence,
            "property": property_id,
            "can_roll": can_roll,
            "roll_id": roll_id,
            "hide_in_tooltip": bool(hide_in_tooltip),
            "shared_roll_id_mods": roll_id_counts.get(roll_id, 0),
            "stat_id": stat.get("stat_id") if stat else None,
        }

        raw_value = mod.get("value")
        raw_max = mod.get("maxValue")
        numeric_value = _number(raw_value)
        numeric_max = _number(raw_max) if raw_max is not None else None
        if not can_roll:
            if numeric_value is None:
                results.append(
                    _failure(
                        "unique_mod_value_missing",
                        ROLL_ABSENT,
                        None,
                        mod_evidence,
                        values=[_unresolved_value_entry(index, property_id, "non-rollable mod has no numeric value", ROLL_ABSENT, None)],
                    )
                )
                continue
            entry = {
                "property": property_id,
                "modifier_type": modifier_type,
                "direction": DIRECTION_CONSTANT,
                "roll_min": numeric_value,
                "roll_max": numeric_value,
                "value_numeric": numeric_value,
                "display_value": display_value(numeric_value, stat, modifier_type),
                "round_added_to_int": round_added_to_int(stat),
                "derivation": "canRoll=false: definitional constant returned by rrlM without a roll",
                "status": STATUS_RESOLVED if stat is not None else STATUS_PARTIAL,
                "branch": "non_rollable_constant",
                "tags": tags,
                "special_tag": special_tag,
                "index": index,
                **_stat_trace(stat, special_tag),
            }
            results.append(
                _result(
                    entry["status"],
                    ROLL_ABSENT,
                    None,
                    [entry],
                    None,
                    {**mod_evidence, "note": "non-rollable unique mod: no ur slot is consumed"},
                )
            )
            continue

        # The archived item-card path clamps a rollable unique range when the
        # declared maxValue is non-zero and <= value (rrl3o / offset 433376).
        effective_max = numeric_max
        clamp_note = None
        if numeric_max is not None and numeric_max != 0 and numeric_max <= numeric_value:
            effective_max = numeric_value
            clamp_note = "unique range maxValue <= value: site sets maxValue=value (constant)"

        if roll_id is None:
            roll_status, raw_out, invalid = ROLL_ABSENT, None, "unique_mod_roll_id_missing"
        elif isinstance(roll_id, bool) or not isinstance(roll_id, int):
            roll_status, raw_out, invalid = ROLL_ABSENT, None, "unique_mod_roll_id_malformed"
        elif roll_id < 0:
            roll_status, raw_out, invalid = ROLL_ABSENT, None, "unique_mod_roll_id_negative"
        elif roll_id >= len(ur_list):
            roll_status, raw_out, invalid = ROLL_ABSENT, None, "unique_roll_array_too_short"
        else:
            raw_roll = ur_list[roll_id]
            roll_status, raw_out, invalid = _read_roll(raw_roll)

        if invalid:
            derived = (
                rrl6o(round_added_to_int(stat), modifier_type, numeric_value, effective_max)
                if numeric_value is not None and effective_max is not None
                else None
            )
            entry = _unresolved_value_entry(index, property_id, invalid, roll_status, raw_out)
            entry["value_numeric"] = None
            entry["derived_display_value"] = derived
            # The declared range is known even when the ur slot is missing/malformed,
            # so the average branch stays available as a labelled derived display value.
            policy = _derived_policy(roll_status, [entry])
            result = _result(
                STATUS_PARTIAL if derived is not None else STATUS_UNRESOLVED,
                roll_status,
                raw_out,
                [entry],
                policy,
                {
                    **mod_evidence,
                    "clamp_note": clamp_note,
                    "failure_reason": invalid,
                },
            )
            result["reason"] = invalid
            results.append(result)
            continue
        if numeric_value is None or effective_max is None:
            results.append(
                _failure(
                    "unique_mod_range_missing",
                    roll_status,
                    raw_out,
                    mod_evidence,
                    values=[_unresolved_value_entry(index, property_id, "unique mod range missing", roll_status, raw_out)],
                )
            )
            continue

        entry = _restore_range(
            value=numeric_value,
            max_value=effective_max,
            raw_roll=raw_roll,
            modifier_type=modifier_type,
            stat=stat,
            special_tag=special_tag,
        )
        entry["property"] = property_id
        entry["tags"] = tags
        entry["special_tag"] = special_tag
        entry["index"] = index
        entry["display_value"] = display_value(entry["value_numeric"], stat, modifier_type)
        policy = _derived_policy(roll_status, [entry])
        results.append(
            _result(
                entry["status"],
                roll_status,
                raw_out,
                [entry],
                policy,
                {**mod_evidence, "clamp_note": clamp_note, "effective_max_value": effective_max},
            )
        )
    return results


__all__ = [
    "CODE_RULE_VERSION",
    "EVIDENCE",
    "MISSING_ROLL",
    "AFFIX_ROLL_CHANNELS",
    "IMPLICIT_ROLLS_FIELD",
    "UNIQUE_ROLLS_FIELD",
    "STATUS_RESOLVED",
    "STATUS_PARTIAL",
    "STATUS_UNRESOLVED",
    "ROLL_PRESENT",
    "ROLL_ABSENT",
    "ROLL_NULL",
    "DIRECTION_ASCENDING",
    "DIRECTION_INVERTED",
    "DIRECTION_CONSTANT",
    "RollValueError",
    "rrlb",
    "rrl7o",
    "rrl6o",
    "round_added_to_int",
    "display_value",
    "restore_value_from_range",
    "restore_affix_value",
    "restore_implicit_values",
    "restore_unique_values",
]
