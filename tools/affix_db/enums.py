"""Enum tables recovered (statically) from the tunklab client chunks.

The tunklab data uses small numeric enums.  We do not execute the chunk; the
mappings below are transcribed from the enum-assignment expressions that appear
verbatim in the chunk, and :func:`verify_anchors` re-checks that every anchor is
still present in the frozen snapshot so a future tunklab build cannot silently
change the meaning of a stored integer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnumTable:
    name: str
    values: dict[int, str]
    anchor: str  # literal substring in the chunk that proves the mapping
    export_marker: str  # module export id, for provenance only


AFFIX_TYPE = EnumTable(
    name="affix_type",
    values={0: "Prefix", 1: "Suffix", 2: "Special"},
    anchor='[i.Prefix=0]="Prefix"',
    export_marker="973458",
)

SPECIAL_AFFIX_TYPE = EnumTable(
    name="special_affix_type",
    values={
        0: "Standard",
        1: "Experimental",
        2: "Personal",
        3: "Set",
        4: "IdolEnchantment",
        5: "IdolWeaver",
        6: "Corrupted",
        7: "FakeUniqueMod",
    },
    anchor='[t.Standard=0]="Standard"',
    export_marker="973458",
)

MOD_TYPE = EnumTable(
    name="mod_type",
    values={0: "ADDED", 1: "INCREASED", 2: "MORE", 3: "QUOTIENT"},
    anchor='[t.ADDED=0]="ADDED"',
    export_marker="735662",
)

CLASS_SPECIFICITY = EnumTable(
    name="class_specificity",
    values={0: "None", 1: "NonSpecific", 2: "Primalist", 4: "Mage", 8: "Sentinel", 16: "Acolyte", 32: "Rogue"},
    anchor='[l.None=0]="None"',
    export_marker="973458",
)

ROUNDING_FOR_ADDED = EnumTable(
    name="rounding_for_added",
    values={0: "Hundredth", 1: "Integer", 2: "Tenth", 3: "Thousandth"},
    anchor='[m.Hundredth=0]="Hundredth"',
    export_marker="973458",
)

ROLLS_ON = EnumTable(
    name="rolls_on",
    values={0: "Equipment", 1: "Idol"},
    anchor="rollsOn:1",
    export_marker="",
)

#: Base type ids tunklab hides from "Applies To" / scaled-value grouping.
#: Evidence: ``f=[11,24,105,40]`` exported as ``bH`` by the equipment module and
#: used as ``e.canRollOn.filter(e => -1 === n.bH.indexOf(e))``.
TUNKLAB_EXCLUDED_BASE_TYPES = (11, 24, 105, 40)

#: Base type ids referenced by tunklab that are *not* present in
#: ``equippableItems`` / ``nonEquippableItems`` (verified at build time).
#: 11 = Crossbow, 24 = One-Handed Fist in the game's ItemType enum; they have no
#: item base in the tunklab dataset, so no roll range can be scaled for them.
UNRESOLVED_BASE_TYPE_HINTS = {
    11: "Crossbow (game ItemType enum; no tunklab item base)",
    24: "One-Handed Fist (game ItemType enum; no tunklab item base)",
    105: "pseudo base type (no tunklab item base)",
    40: "Unused (game ItemType enum)",
}

ENUM_TABLES = (
    AFFIX_TYPE,
    SPECIAL_AFFIX_TYPE,
    MOD_TYPE,
    CLASS_SPECIFICITY,
    ROUNDING_FOR_ADDED,
)


def name_of(table: EnumTable, value: int | None) -> str | None:
    if value is None:
        return None
    return table.values.get(int(value))


def class_specificity_names(value: int) -> list[str]:
    """Decode the affix ``classSpecificity`` bit field (tunklab helper ``k``)."""
    if value is None:
        return []
    out: list[str] = []
    if 16 & value:
        out.append("Acolyte")
    if 4 & value:
        out.append("Mage")
    if 2 & value:
        out.append("Primalist")
    if 32 & value:
        out.append("Rogue")
    if 8 & value:
        out.append("Sentinel")
    if 1 & value:
        out.append("Non-Specific")
    return out


def verify_anchors(chunk_text: str) -> dict[str, bool]:
    """Check every enum anchor literal is present in the frozen chunk."""
    return {table.name: table.anchor in chunk_text for table in ENUM_TABLES}
