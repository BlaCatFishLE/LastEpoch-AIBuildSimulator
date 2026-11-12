"""Scaling formula and display-rounding semantics for tunklab affix data.

Everything here is a static re-implementation of the *observable* client code in
the downloaded tunklab page chunk (``standardAffixEffectModifier`` /
``affixEffectModifier`` / ``getDisplayValue``).  It is data-processing, not
execution: the downloaded JavaScript is never run.

Formula (evidence: tunklab page chunk ``0-8itj8_g2l_7.js``)::

    # grouping helper
    modifier = (baseType.affixEffectModifier + 1) / (affix.standardAffixEffectModifier + 1)

    # scaled-values table / item tooltip
    effective_min = tier.minRoll * modifier
    effective_max = tier.maxRoll * modifier

Display rounding (evidence: ``getDisplayValue`` in the data chunk)::

    value = Math.round(100 * effective)        # percentage properties
    value = Math.round(effective)              # flat/integer properties
    value = Math.round(10 * effective) / 10    # one-decimal properties

``Math.round`` in JavaScript is round-half-up (ties toward +infinity).  The game
itself is Unity/C#; ``Mathf.RoundToInt`` / .NET ``Math.Round`` default to
round-half-to-even, which matters for exact ``.5`` endpoints (e.g. Body Armor
Fire Resistance T7 = 91.5..112.5 -> tunklab 92..113, Unity display 92..112).
We therefore store both display conventions explicitly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

# Enum values recovered from the tunklab data chunk (see enums.py).
MOD_ADDED = 0
MOD_INCREASED = 1
MOD_MORE = 2

ROUNDING_HUNDREDTH = 0
ROUNDING_INTEGER = 1
ROUNDING_TENTH = 2
ROUNDING_THOUSANDTH = 3

PROPERTY_PLAYER_PROPERTY = 98
PROPERTY_ABILITY_PROPERTY = 58
PROPERTY_EFFECT_OF_AILMENT_ON_YOU = 120
PROPERTY_IDOL_ALTAR = 130

IDOL_ALTAR_FRACTIONAL_TAGS = frozenset({4, 15, 16, 17, 18, 21, 29, 30})

ROUNDING_POLICY_TUNKLAB = "tunklab-js-math-round-half-up"
ROUNDING_POLICY_UNITY = "unity-dotnet-round-half-to-even"
ROUNDING_POLICY_RAW = "unresolved-property-raw-fallback"


def compute_modifier(base_affix_effect_modifier: float, standard_affix_effect_modifier: float) -> float:
    """Return the tunklab scale factor for one (base type, affix) pair.

    ``(base.affixEffectModifier + 1) / (affix.standardAffixEffectModifier + 1)``

    Raises ``ZeroDivisionError`` if ``standardAffixEffectModifier == -1`` (not
    observed in the dataset, but guarded instead of silently producing inf).
    """
    denominator = 1.0 + float(standard_affix_effect_modifier)
    if denominator == 0.0:
        raise ZeroDivisionError("standardAffixEffectModifier == -1 produces an undefined scale factor")
    return (1.0 + float(base_affix_effect_modifier)) / denominator


def js_math_round(value: float) -> int:
    """JavaScript ``Math.round`` semantics: round half toward +infinity."""
    return math.floor(value + 0.5)


def unity_round_to_int(value: float) -> int:
    """Unity/C# display rounding: round half to even (banker's rounding)."""
    return round(value)


def round_to_tenth(value: float, rounder) -> float:
    return rounder(10.0 * value) / 10.0


def fraction_from_clean(value: float | int) -> Fraction:
    """Interpret a JSON-sourced number by its shortest decimal representation.

    tunklab serialises the game values as short decimals (``0.61``, ``0.17``,
    ``5``).  Interpreting those decimal strings as exact rationals recovers the
    designer-intended value instead of the binary floating point approximation.
    """
    return Fraction(str(value))


def fraction_to_decimal_string(frac: Fraction) -> str:
    """Render a Fraction whose denominator only has factors 2 and 5 as decimal."""
    if frac.denominator == 1:
        return str(frac.numerator)
    denominator = frac.denominator
    rest = denominator
    for prime in (2, 5):
        while rest % prime == 0:
            rest //= prime
    if rest != 1:
        return repr(float(frac))
    digits = 0
    scaled = frac.numerator
    while (scaled * 10**digits) % denominator != 0:
        digits += 1
    scaled = scaled * 10**digits // denominator
    sign = "-" if scaled < 0 else ""
    digits_abs = str(abs(scaled)).rjust(digits + 1, "0")
    if digits == 0:
        return f"{sign}{digits_abs}"
    return f"{sign}{digits_abs[:-digits]}.{digits_abs[-digits:]}"


def exact_decimal_string(value: float) -> str:
    """Return a short exact decimal representation where one exists."""
    return fraction_to_decimal_string(fraction_from_clean(value))


@dataclass(frozen=True)
class PropertyDisplayInfo:
    """Display metadata for one stat property (mirrors tunklab's Property class)."""

    property_id: int
    property_name: str
    mod_type: int
    rounding_for_added: int | None
    display_added_as_percentage: bool
    display_added_as_tenth_of_value: bool
    display_as_percentage_of: bool
    display_as_added_to: bool
    dont_display_plus: bool
    less_is_better: bool
    more_rounding: int | None = None
    resolved: bool = True


@dataclass(frozen=True)
class DisplayResult:
    value: float
    unit: str
    scale: int
    policy: str
    resolved: bool


def compute_display(
    effective: float,
    info: PropertyDisplayInfo | None,
    mod_type: int,
) -> DisplayResult:
    """Re-implement tunklab ``getDisplayValue`` + ``getValueUnit``.

    ``info is None`` means tunklab had no property metadata for this property id.
    The client code then returns the raw (unscaled) value; we keep the value but
    mark it unresolved so callers do not present it as a validated display value.
    """
    if info is None:
        if mod_type == MOD_MORE:
            return DisplayResult(effective, "", 1, ROUNDING_POLICY_RAW, False)
        return DisplayResult(effective, "", 1, ROUNDING_POLICY_RAW, False)

    if not info.resolved:
        return DisplayResult(effective, "", 1, ROUNDING_POLICY_RAW, False)

    # getValueUnit
    if (
        mod_type != MOD_ADDED
        or info.display_added_as_percentage
        or (info.rounding_for_added != ROUNDING_TENTH and info.display_as_percentage_of)
    ):
        unit = "%"
    else:
        unit = ""

    x = float(effective)
    if x < 0:
        if mod_type != MOD_ADDED and not info.display_as_percentage_of:
            x = -x
        if mod_type == MOD_MORE:
            x = -x

    if mod_type == MOD_ADDED and info.rounding_for_added == ROUNDING_TENTH and not info.display_added_as_percentage:
        value = js_math_round(10.0 * x) / 10.0
        scale = 10
    elif mod_type == MOD_ADDED and not info.display_added_as_percentage and not info.display_as_percentage_of:
        value = float(js_math_round(x))
        scale = 1
    elif info.display_added_as_tenth_of_value and mod_type == MOD_ADDED:
        value = js_math_round(100.0 * x) / 10.0
        scale = 10
    elif mod_type != MOD_MORE:
        value = float(js_math_round(100.0 * x))
        scale = 100
    elif info.more_rounding == ROUNDING_THOUSANDTH:
        value = js_math_round(1000.0 * x) / 10.0
        scale = 10
    elif info.more_rounding == ROUNDING_HUNDREDTH:
        value = float(js_math_round(100.0 * x))
        scale = 100
    elif info.more_rounding == ROUNDING_TENTH:
        value = 10.0 * js_math_round(10.0 * x)
        scale = 10
    else:
        value = float(js_math_round(100.0 * x))
        scale = 100
    return DisplayResult(value, unit, scale, ROUNDING_POLICY_TUNKLAB, True)


def game_display_value(effective: float, info: PropertyDisplayInfo | None, mod_type: int) -> DisplayResult:
    """The same scale/unit decision as :func:`compute_display`, but rounded the
    way Unity/C# rounds (half-to-even).  This is the convention the in-game
    tooltip uses; for most values it agrees with tunklab and it only differs on
    exactly representable ``.5`` endpoints."""
    result = compute_display(effective, info, mod_type)
    if not result.resolved:
        return DisplayResult(effective, result.unit, result.scale, result.policy, False)
    scale = result.scale
    if scale == 1:
        value = float(unity_round_to_int(effective))
    elif scale == 10:
        value = unity_round_to_int(10.0 * effective) / 10.0
    else:
        value = float(unity_round_to_int(100.0 * effective))
    if effective < 0:
        x = float(effective)
        info_ok = info is not None
        if info_ok and mod_type != MOD_ADDED and not info.display_as_percentage_of:
            x = -x
        if mod_type == MOD_MORE:
            x = -x
        if scale == 1:
            value = float(unity_round_to_int(x))
        elif scale == 10:
            value = unity_round_to_int(10.0 * x) / 10.0
        else:
            value = float(unity_round_to_int(100.0 * x))
    return DisplayResult(value, result.unit, scale, ROUNDING_POLICY_UNITY, True)


def clean_modifier_fraction(
    base_affix_effect_modifier: float,
    standard_affix_effect_modifier: float,
) -> Fraction:
    """Exact rational scale factor from the decimal-serialised modifiers."""
    return (fraction_from_clean(base_affix_effect_modifier) + 1) / (
        fraction_from_clean(standard_affix_effect_modifier) + 1
    )


def range_summary(
    raw_min: float,
    raw_max: float,
    base_affix_effect_modifier: float,
    standard_affix_effect_modifier: float,
    info: PropertyDisplayInfo | None,
    mod_type: int,
) -> dict[str, Any]:
    """Compute the full normalized row payload for one roll slot of one tier."""
    modifier = compute_modifier(base_affix_effect_modifier, standard_affix_effect_modifier)
    modifier_fraction = clean_modifier_fraction(base_affix_effect_modifier, standard_affix_effect_modifier)
    effective_min = raw_min * modifier
    effective_max = raw_max * modifier
    exact_min = fraction_from_clean(raw_min) * modifier_fraction
    exact_max = fraction_from_clean(raw_max) * modifier_fraction
    tunk = compute_display(effective_min, info, mod_type)
    tunk_max = compute_display(effective_max, info, mod_type)
    game = game_display_value(effective_min, info, mod_type)
    game_max = game_display_value(effective_max, info, mod_type)
    return {
        "raw_min": raw_min,
        "raw_max": raw_max,
        "modifier": modifier,
        "modifier_decimal": fraction_to_decimal_string(modifier_fraction),
        "effective_min": effective_min,
        "effective_max": effective_max,
        "effective_min_decimal": fraction_to_decimal_string(exact_min),
        "effective_max_decimal": fraction_to_decimal_string(exact_max),
        "display_unit": tunk.unit,
        "display_scale": tunk.scale,
        "display_min": game.value if game.resolved else None,
        "display_max": game_max.value if game_max.resolved else None,
        "tunklab_display_min": tunk.value if tunk.resolved else None,
        "tunklab_display_max": tunk_max.value if tunk_max.resolved else None,
        "display_policy": ROUNDING_POLICY_UNITY,
        "tunklab_display_policy": ROUNDING_POLICY_TUNKLAB,
        "display_resolved": bool(tunk.resolved),
        "formula": "effective = raw * (1 + base.affixEffectModifier) / (1 + affix.standardAffixEffectModifier)",
    }
