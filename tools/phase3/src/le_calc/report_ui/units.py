"""Display-unit helpers for the offline HTML damage report.

Every number that leaves the presentation layer goes through this module so the
whole report uses one vocabulary::

    fraction   0.4      -> 40%          (a share of 100%)
    pp         948.94   -> 948.94%      (already percentage points)
    factor     1.6      -> ×1.6         (a multiplier)
    damage     decimal  -> 1,066.22 万  (compact) / 1,066,227.47 (full)
    dps        decimal  -> compact damage per second (unit added by the caller)
    seconds    7.15     -> 7.15 秒
    stacks     711.705  -> 711.705 层
    stacks_rate 94.894  -> 94.894 层/秒
    count      24       -> 24 次
    points     5        -> 5 点
    integer    3        -> 3
    number     73       -> 73  (plain fallback for flat values)

Rules of thumb (from the UI redesign brief §9):

* damage numbers default to 2 decimals and support a global ``万 / 亿`` toggle;
* factors keep 3-4 decimals and always carry a leading ``×``;
* percentages keep up to 2 decimals, trailing zeros trimmed;
* every rendered number keeps an exact full-precision tooltip and both a
  compact and a full text so the client only swaps strings (no math in JS);
* unknown values are rendered as ``待确认`` / ``未提供`` - never ``0`` or ``NaN``.
"""

from __future__ import annotations

import math
from html import escape as _escape

__all__ = [
    "UNKNOWN",
    "MISSING",
    "UNIT_LABELS",
    "clean_number",
    "is_number",
    "fmt_plain",
    "fmt_compact",
    "fmt_full",
    "fmt",
    "unit_of",
    "fmt_with_unit",
    "num",
    "pct",
    "factor",
    "seconds",
    "stacks",
    "signed_pct",
    "unknown",
    "missing",
]

UNKNOWN = "待确认"
MISSING = "未提供"

UNIT_LABELS = {
    "seconds": "秒",
    "stacks": "层",
    "stacks_rate": "层/秒",
    "count": "次",
    "points": "点",
    "dps": "DPS",
    "damage": "伤害",
}


def is_number(value) -> bool:
    """True for a finite int/float-like value (strings are accepted)."""

    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, str):
        try:
            return math.isfinite(float(value))
        except ValueError:
            return False
    return False


def clean_number(value) -> float:
    """Coerce a JSON scalar to float, returning ``nan`` for anything unusable."""

    if isinstance(value, bool) or value is None:
        return math.nan
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _trim(text: str) -> str:
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _group(value: float, digits: int) -> str:
    if digits <= 0:
        return f"{round(value):,d}"
    return _trim(f"{value:,.{digits}f}")


def _percent_text(percent: float, digits: int = 2) -> str:
    return f"{_group(percent, digits)}%"


def _compact_damage(value: float, digits: int = 2) -> str:
    magnitude = abs(value)
    if magnitude >= 1e8:
        return f"{_group(value / 1e8, digits)} 亿"
    if magnitude >= 1e4:
        return f"{_group(value / 1e4, digits)} 万"
    return _group(value, digits)


#: kind -> (compact formatter, full formatter)
_FORMATTERS = {
    "damage": (lambda v: _compact_damage(v, 2), lambda v: _group(v, 2)),
    "dps": (lambda v: _compact_damage(v, 2), lambda v: _group(v, 2)),
    "fraction": (lambda v: _percent_text(v * 100.0), lambda v: _percent_text(v * 100.0)),
    "pp": (lambda v: _percent_text(v), lambda v: _percent_text(v)),
    "factor": (lambda v: "×" + _group(v, 4), lambda v: "×" + _group(v, 6)),
    "seconds": (lambda v: _trim(f"{v:.2f}"), lambda v: _trim(f"{v:.5f}")),
    "stacks": (lambda v: _trim(f"{v:.3f}"), lambda v: _trim(f"{v:.5f}")),
    "stacks_rate": (lambda v: _trim(f"{v:.3f}"), lambda v: _trim(f"{v:.5f}")),
    "count": (lambda v: _group(v, 0), lambda v: _group(v, 0)),
    "points": (lambda v: _group(v, 0), lambda v: _group(v, 0)),
    "integer": (lambda v: _group(v, 0), lambda v: _group(v, 0)),
    "number": (lambda v: _group(v, 2), lambda v: _trim(f"{v:.6f}")),
}

#: exact-value tooltip digits per kind
_EXACT_DIGITS = {
    "damage": 6,
    "dps": 6,
    "fraction": 6,
    "pp": 4,
    "factor": 8,
    "seconds": 6,
    "stacks": 6,
    "stacks_rate": 6,
    "count": 0,
    "points": 0,
    "integer": 0,
    "number": 8,
}


def fmt_plain(value, digits: int = 2) -> str:
    """Plain grouped number without unit semantics (legacy-safe fallback)."""

    number = clean_number(value)
    if math.isnan(number):
        return MISSING
    return _trim(f"{number:,.{digits}f}") if digits > 0 else f"{round(number):,d}"


def fmt_compact(value, kind: str = "number") -> str:
    """Compact display text (万/亿 for damage, % for fractions) - no unit suffix."""

    number = clean_number(value)
    if math.isnan(number):
        return MISSING
    compact, _full = _FORMATTERS.get(kind, _FORMATTERS["number"])
    return compact(number)


def fmt_full(value, kind: str = "number", *, digits: int | None = None) -> str:
    """Full-precision-ish display text used by the ``显示完整数字`` toggle."""

    number = clean_number(value)
    if math.isnan(number):
        return MISSING
    _compact, full = _FORMATTERS.get(kind, _FORMATTERS["number"])
    if digits is not None:
        if kind == "fraction":
            return _percent_text(number * 100.0, digits)
        if kind == "pp":
            return _percent_text(number, digits)
        if kind == "factor":
            return "×" + _group(number, digits)
        return _group(number, digits) if kind not in ("seconds", "stacks", "stacks_rate") else _trim(f"{number:.{digits}f}")
    return full(number)


def fmt(value, kind: str = "number", *, full: bool = False) -> str:
    return fmt_full(value, kind) if full else fmt_compact(value, kind)


def unit_of(kind: str) -> str:
    return UNIT_LABELS.get(kind, "")


def fmt_with_unit(value, kind: str = "number") -> str:
    text = fmt(value, kind)
    unit = unit_of(kind)
    return f"{text} {unit}" if unit else text


def _exact_title(value, kind: str) -> str:
    number = clean_number(value)
    if math.isnan(number):
        return ""
    digits = _EXACT_DIGITS.get(kind, 6)
    if kind == "fraction":
        return f"精确值 {number:.8f}（{fmt_full(number, 'fraction')}）"
    if kind == "pp":
        return f"精确值 {number:.6f}%"
    if kind == "factor":
        return f"精确值 ×{number:.10f}"
    if kind in ("count", "points", "integer"):
        return f"精确值 {number:,.0f}"
    if kind in ("seconds", "stacks", "stacks_rate", "number"):
        return f"精确值 {number:,.{digits}f}".rstrip("0").rstrip(".")
    return f"精确值 {number:,.{digits}f}"


def num(
    value,
    kind: str = "number",
    *,
    cls: str = "",
    title: str | None = None,
    unit: bool = False,
    compact_text: str | None = None,
    sc: str | None = None,
) -> str:
    """Render a value as a toggleable ``.num`` span (compact text by default).

    ``sc`` tags the number for the dynamic-scenario engine:

    * ``group:fire`` / ``group:cold`` / ``group:ignite`` / ``group:body`` /
      ``group:total`` / ``group:loop`` - the value scales with the target-side
      multiplier group when the reader changes monster level / target fire
      resistance;
    * ``stat:level`` / ``stat:dr`` / ``stat:drFactor`` / ``stat:targetRes`` /
      ``stat:effFire`` / ``stat:multFire`` / ``stat:effIgnite`` /
      ``stat:multIgnite`` / ``stat:effCold`` / ``stat:multCold`` - the value is
      recomputed from the selected scenario.
    """

    if not is_number(value):
        return unknown()
    number = clean_number(value)
    text = compact_text if compact_text is not None else fmt_compact(number, kind)
    full_text = fmt_full(number, kind)
    if unit:
        suffix = unit_of(kind)
        if suffix:
            text = f"{text} {suffix}"
            full_text = f"{full_text} {suffix}"
    classes = "num"
    if kind:
        classes += f" n-{kind}"
    if compact_text is not None:
        classes += " n-custom"
    if cls:
        classes += " " + cls
    tooltip = title if title is not None else _exact_title(number, kind)
    attrs = [
        f'class="{classes}"',
        f'data-value="{_escape(f"{number:.12g}", quote=True)}"',
        f'data-kind="{_escape(kind, quote=True)}"',
        f'data-compact="{_escape(text, quote=True)}"',
        f'data-full="{_escape(full_text, quote=True)}"',
    ]
    if tooltip:
        attrs.append(f'title="{_escape(tooltip, quote=True)}"')
    if sc:
        attrs.append(f'data-sc="{_escape(sc, quote=True)}"')
    return f"<span {' '.join(attrs)}>{_escape(text)}</span>"


def _signed(value, kind: str) -> str:
    number = clean_number(value)
    if math.isnan(number):
        return MISSING
    if kind == "fraction":
        shown = number * 100.0
    elif kind == "pp":
        shown = number
    else:
        shown = number
    sign = "+" if shown > 0 else ""
    if kind in ("fraction", "pp"):
        return f"{sign}{_group(shown, 2)}%"
    return f"{sign}{_group(shown, 2)}"


def pct(value) -> str:
    """Fraction -> ``40%`` / ``948.94%`` (2 decimals, trimmed)."""

    return fmt_compact(value, "fraction")


def signed_pct(value) -> str:
    """Fraction -> ``+15%`` / ``-3%``."""

    return _signed(value, "fraction")


def factor(value) -> str:
    return fmt_compact(value, "factor")


def seconds(value) -> str:
    return fmt_with_unit(value, "seconds")


def stacks(value) -> str:
    return fmt_with_unit(value, "stacks")


def unknown(text: str = UNKNOWN) -> str:
    return f'<span class="num unknown" title="{_escape("该数值在当前结果中没有可靠来源", quote=True)}">{_escape(text)}</span>'


def missing(text: str = MISSING) -> str:
    return f'<span class="num missing">{_escape(text)}</span>'
