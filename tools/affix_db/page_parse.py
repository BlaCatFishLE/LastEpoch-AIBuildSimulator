"""Parser for the rendered tunklab affix page ("Scaled Values" table).

The *server-rendered* HTML only contains five tier columns; the full eight-column
table is produced after client hydration (columns T6+ are ``responsive: ["md"]``).
``render_pages`` therefore captures the post-hydration DOM with headless Chrome
and this module turns that DOM into comparable structured rows.

Input is untrusted page content; only text/tables are read, nothing is executed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

#: tunklab's page-side display names for weapon base types (helper ``E``/``i7``
#: in the client code overrides the generic ``displayName`` for these ids).
_PAGE_DISPLAY_NAME_OVERRIDES = {
    5: "1H Axe",
    7: "1H Mace",
    9: "1H Sword",
    12: "2H Axe",
    13: "2H Mace",
    16: "2H Sword",
    14: "Spear",
    15: "Staff",
    8: "Scepter",
    19: "Catalyst",
}

_RANGE_RE = re.compile(
    r"(?P<low>-?\d+(?:\.\d+)?)\s*(?P<unit1>%?)"
    r"(?:\s*to\s*(?P<high>-?\d+(?:\.\d+)?)\s*(?P<unit2>%?))?"
)


def page_display_name(base_type_id: int, display_name: str | None) -> str:
    return _PAGE_DISPLAY_NAME_OVERRIDES.get(int(base_type_id), display_name or f"Unknown ({base_type_id})")


@dataclass
class PageCell:
    text: str = ""
    div_texts: list[str] = field(default_factory=list)
    span_texts: list[str] = field(default_factory=list)


@dataclass
class PageRow:
    row_key: str | None
    cells: list[PageCell]

    @property
    def item_names(self) -> list[str]:
        return self.cells[0].span_texts if self.cells else []


@dataclass
class ScaledValuesTable:
    headers: list[str] = field(default_factory=list)
    rows: list[PageRow] = field(default_factory=list)

    @property
    def tier_headers(self) -> list[str]:
        return [header for header in self.headers if header.lower().startswith("tier")]


class _ScaledValuesParser(HTMLParser):
    """Collect the first table that follows an ``<h2>Scaled Values</h2>``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[ScaledValuesTable] = []
        self._heading_text: list[str] = []
        self._in_heading = False
        self._awaiting_table = False
        self._table: ScaledValuesTable | None = None
        self._in_table = False
        self._in_thead = False
        self._in_th = False
        self._header_buffer: list[str] = []
        self._row: PageRow | None = None
        self._cell: PageCell | None = None
        self._div_depth = 0
        self._div_text: list[str] = []
        self._in_span = False
        self._span_text: list[str] = []

    # ------------------------------------------------------------------ tags
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "h2":
            self._in_heading = True
            self._heading_text = []
            return
        if tag == "table" and self._awaiting_table:
            self._table = ScaledValuesTable()
            self._in_table = True
            self._awaiting_table = False
            return
        if not self._in_table or self._table is None:
            return
        if tag == "thead":
            self._in_thead = True
        elif tag == "tr":
            self._row = PageRow(row_key=attributes.get("data-row-key"), cells=[])
        elif tag in ("td", "th"):
            self._cell = PageCell()
            self._in_th = tag == "th"
            self._header_buffer = []
            self._div_depth = 0
            self._div_text = []
        elif tag == "div" and self._cell is not None:
            if self._div_depth == 0:
                self._div_text = []
            self._div_depth += 1
        elif tag == "span" and self._cell is not None:
            self._in_span = True
            self._span_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2":
            if "".join(self._heading_text).strip() == "Scaled Values":
                self._awaiting_table = True
            self._in_heading = False
            self._heading_text = []
            return
        if tag == "table" and self._in_table:
            if self._table is not None:
                self.tables.append(self._table)
            self._table = None
            self._in_table = False
            self._awaiting_table = False
            return
        if not self._in_table or self._table is None:
            return
        if tag == "thead":
            self._in_thead = False
        elif tag == "tr":
            if self._row is not None and self._row.cells:
                self._table.rows.append(self._row)
            self._row = None
            self._cell = None
        elif tag in ("td", "th"):
            if self._cell is not None:
                if self._in_th:
                    self._table.headers.append(" ".join(self._header_buffer).strip())
                elif self._row is not None:
                    self._cell.text = re.sub(r"\s+", " ", self._cell.text).strip()
                    self._row.cells.append(self._cell)
            self._cell = None
            self._in_th = False
        elif tag == "div" and self._cell is not None:
            if self._div_depth == 1:
                text = re.sub(r"\s+", " ", "".join(self._div_text)).strip()
                if text:
                    self._cell.div_texts.append(text)
            self._div_depth = max(0, self._div_depth - 1)
        elif tag == "span" and self._cell is not None:
            text = "".join(self._span_text).strip()
            if text:
                self._cell.span_texts.append(text)
            self._in_span = False
            self._span_text = []

    def handle_data(self, data: str) -> None:
        if self._in_heading:
            self._heading_text.append(data)
            return
        if not self._in_table:
            return
        if self._in_th:
            self._header_buffer.append(data)
        if self._cell is not None:
            self._cell.text += data
            if self._in_span:
                self._span_text.append(data)
            if self._div_depth > 0:
                self._div_text.append(data)


def parse_scaled_values_table(html: str) -> ScaledValuesTable | None:
    """Parse the first "Scaled Values" table in a rendered affix page DOM."""
    parser = _ScaledValuesParser()
    parser.feed(html)
    return parser.tables[0] if parser.tables else None


_APPLIES_TO_RE = re.compile(
    r">\s*Applies To\s*</th>\s*<td[^>]*>(?P<td>.*?)</td>", re.S | re.I
)
_TEXT_CHUNK_RE = re.compile(r">([^<>]+)<")


def parse_applies_to(html: str) -> list[str]:
    """Extract the display names listed in an affix page's "Applies To" cell.

    The cell is a flex container of badge ``<span>`` elements (plain item names)
    and, for idol bases, extra grid-size text nodes ("1×1").  We return every
    non-empty text chunk; callers intersect with known base display names.
    """
    match = _APPLIES_TO_RE.search(html)
    if not match:
        return []
    cell_html = match.group("td")
    chunks: list[str] = []
    for chunk in _TEXT_CHUNK_RE.findall(cell_html):
        text = re.sub(r"\s+", " ", chunk).strip()
        if text:
            chunks.append(text)
    # Text directly inside the td (no nested tags) is only needed when the cell
    # has no child elements at all.
    if not chunks:
        plain = re.sub(r"<[^>]+>", " ", cell_html)
        plain = re.sub(r"\s+", " ", plain).strip()
        if plain:
            chunks.append(plain)
    seen: set[str] = set()
    unique: list[str] = []
    for chunk in chunks:
        if chunk not in seen:
            seen.add(chunk)
            unique.append(chunk)
    return unique


def parse_cell_ranges(text: str) -> list[dict[str, Any]]:
    """Parse one rendered value cell into ``[{low, high, unit}, ...]``.

    A cell may contain several lines for multi-property hybrid affixes.
    Returns an empty list for "-" placeholders.
    """
    cleaned = text.replace("+", " ").replace("(", " ").replace(")", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or cleaned == "-":
        return []
    results: list[dict[str, Any]] = []
    for match in _RANGE_RE.finditer(cleaned):
        low = float(match.group("low"))
        high_text = match.group("high")
        high = float(high_text) if high_text is not None else low
        unit = match.group("unit1") or match.group("unit2") or ""
        results.append({"low": low, "high": high, "unit": unit, "raw": match.group(0).strip()})
    return results
