"""Static parser for a safe subset of JavaScript object/array literals.

This module exists because tunklab's affix/equipment database ships inside a
minified Turbopack client chunk as a plain JS object literal.  We must extract
it *without executing any downloaded JavaScript*, so instead of ``eval`` /
``node`` we implement a small recursive-descent parser that accepts exactly the
literal-value grammar the data uses:

    value      := object | array | string | number | boolean | null | undefined
    object     := '{' (key ':' value (',' key ':' value)* ','?)? '}'
    key        := identifier | string | number
    array      := '[' (value (',' value)* ','?)? ']'
    string     := '"' ... '"' | "'" ... "'"      (with JS escapes)
    number     := [-+]? (decimal | hex | '.5' | '1e3')
    boolean    := 'true' | 'false' | '!0' | '!1'
    undefined  := 'undefined' | 'void' '0'

Anything outside this grammar raises :class:`JSLiteralError`; in particular
function expressions, template literals, spreads, identifiers used as values and
method calls are rejected instead of being silently skipped.

The parser keeps exact source spans so callers can record a reproducible
``source locator`` (character/byte offsets and a snippet hash) for every
extracted literal.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Any


class JSLiteralError(ValueError):
    """Raised when the text is not a value of the accepted literal grammar."""


_WS = " \t\r\n\v\f\u00a0\ufeff"
_IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUMBER_RE = re.compile(
    r"""[-+]?(?:
        0[xX][0-9a-fA-F]+
      | (?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?
    )""",
    re.VERBOSE,
)

_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
    "'": "'",
    '"': '"',
    "\\": "\\",
    "/": "/",
    "\n": "",
    "\r": "",
    "\u2028": "",
    "\u2029": "",
}


@dataclass
class Node:
    """Parsed value plus its exact span inside the source text."""

    value: Any
    start: int
    end: int
    kind: str  # 'object' | 'array' | 'scalar'
    children: list["Node"] = field(default_factory=list)


class JSLiteralParser:
    def __init__(self, text: str, pos: int = 0):
        self.text = text
        self.pos = pos
        self.n = len(text)

    # ------------------------------------------------------------------ utils
    def error(self, message: str) -> JSLiteralError:
        line = self.text.count("\n", 0, self.pos) + 1
        col = self.pos - (self.text.rfind("\n", 0, self.pos) + 1) + 1
        return JSLiteralError(f"{message} at line {line} col {col} (offset {self.pos})")

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < self.n else ""

    def skip_ws(self) -> None:
        text, n = self.text, self.n
        while self.pos < n:
            c = text[self.pos]
            if c in _WS:
                self.pos += 1
            elif c == "/" and self.pos + 1 < n and text[self.pos + 1] == "/":
                nl = text.find("\n", self.pos + 2)
                self.pos = n if nl < 0 else nl + 1
            elif c == "/" and self.pos + 1 < n and text[self.pos + 1] == "*":
                end = text.find("*/", self.pos + 2)
                if end < 0:
                    raise self.error("unterminated block comment")
                self.pos = end + 2
            else:
                return

    # ---------------------------------------------------------------- parsing
    def parse(self) -> Node:
        self.skip_ws()
        node = self.parse_value()
        return node

    def parse_value(self) -> Node:
        self.skip_ws()
        if self.pos >= self.n:
            raise self.error("unexpected end of input")
        start = self.pos
        c = self.text[self.pos]
        if c == "{":
            value, children = self.parse_object()
            return Node(value, start, self.pos, "object", children)
        if c == "[":
            value, children = self.parse_array()
            return Node(value, start, self.pos, "array", children)
        if c in "\"'":
            value = self.parse_string()
            return Node(value, start, self.pos, "scalar")
        if c == "!":
            if self.text.startswith("!0", self.pos):
                self.pos += 2
                return Node(True, start, self.pos, "scalar")
            if self.text.startswith("!1", self.pos):
                self.pos += 2
                return Node(False, start, self.pos, "scalar")
            raise self.error("unsupported '!' expression")
        if c == "-" or c == "+" or c.isdigit() or c == ".":
            value = self.parse_number()
            return Node(value, start, self.pos, "scalar")
        m = _IDENT_RE.match(self.text, self.pos)
        if m:
            word = m.group(0)
            if word == "true":
                self.pos = m.end()
                return Node(True, start, self.pos, "scalar")
            if word == "false":
                self.pos = m.end()
                return Node(False, start, self.pos, "scalar")
            if word == "null":
                self.pos = m.end()
                return Node(None, start, self.pos, "scalar")
            if word == "undefined":
                self.pos = m.end()
                return Node(None, start, self.pos, "scalar")
            if word == "void":
                self.pos = m.end()
                self.skip_ws()
                if self.peek() != "0":
                    raise self.error("void expression must be 'void 0'")
                self.pos += 1
                return Node(None, start, self.pos, "scalar")
            raise self.error(f"unsupported identifier/value {word!r}")
        raise self.error(f"unsupported character {c!r}")

    def parse_object(self) -> tuple[dict[str, Any], list[Node]]:
        assert self.text[self.pos] == "{"
        self.pos += 1
        result: dict[str, Any] = {}
        children: list[Node] = []
        self.skip_ws()
        if self.peek() == "}":
            self.pos += 1
            return result, children
        while True:
            self.skip_ws()
            key = self.parse_key()
            self.skip_ws()
            if self.peek() != ":":
                raise self.error(f"expected ':' after object key {key!r}")
            self.pos += 1
            node = self.parse_value()
            children.append(node)
            result[key] = node.value
            self.skip_ws()
            c = self.peek()
            if c == ",":
                self.pos += 1
                self.skip_ws()
                if self.peek() == "}":
                    self.pos += 1
                    return result, children
                continue
            if c == "}":
                self.pos += 1
                return result, children
            raise self.error("expected ',' or '}' in object literal")

    def parse_key(self) -> str:
        c = self.peek()
        if c in "\"'":
            return self.parse_string()
        if c.isdigit():
            value = self.parse_number()
            return str(value)
        m = _IDENT_RE.match(self.text, self.pos)
        if not m:
            raise self.error(f"unsupported object key starting with {c!r}")
        self.pos = m.end()
        return m.group(0)

    def parse_array(self) -> tuple[list[Any], list[Node]]:
        assert self.text[self.pos] == "["
        self.pos += 1
        result: list[Any] = []
        children: list[Node] = []
        self.skip_ws()
        if self.peek() == "]":
            self.pos += 1
            return result, children
        while True:
            node = self.parse_value()
            children.append(node)
            result.append(node.value)
            self.skip_ws()
            c = self.peek()
            if c == ",":
                self.pos += 1
                self.skip_ws()
                if self.peek() == "]":
                    self.pos += 1
                    return result, children
                continue
            if c == "]":
                self.pos += 1
                return result, children
            raise self.error("expected ',' or ']' in array literal")

    def parse_number(self) -> int | float:
        m = _NUMBER_RE.match(self.text, self.pos)
        if not m:
            raise self.error("invalid number literal")
        raw = m.group(0)
        self.pos = m.end()
        sign = 1
        body = raw
        if body[0] in "+-":
            if body[0] == "-":
                sign = -1
            body = body[1:]
        if body[:2].lower() == "0x":
            return sign * int(body, 16)
        if "." in body or "e" in body.lower():
            value = sign * float(body)
            if not math.isfinite(value):
                raise self.error("non-finite number literal")
            return value
        return sign * int(body)

    def parse_string(self) -> str:
        quote = self.text[self.pos]
        self.pos += 1
        out: list[str] = []
        text, n = self.text, self.n
        while self.pos < n:
            c = text[self.pos]
            if c == quote:
                self.pos += 1
                return "".join(out)
            if c == "\\":
                self.pos += 1
                if self.pos >= n:
                    raise self.error("unterminated string escape")
                esc = text[self.pos]
                if esc in _ESCAPES:
                    out.append(_ESCAPES[esc])
                    self.pos += 1
                elif esc == "u":
                    out.append(self._parse_unicode_escape())
                elif esc == "x":
                    self.pos += 1
                    hex_digits = text[self.pos : self.pos + 2]
                    if len(hex_digits) != 2 or not all(ch in "0123456789abcdefABCDEF" for ch in hex_digits):
                        raise self.error("invalid \\x escape")
                    out.append(chr(int(hex_digits, 16)))
                    self.pos += 2
                else:
                    # JS treats unknown escapes as the escaped character itself.
                    out.append(esc)
                    self.pos += 1
                continue
            if c == "\n" or c == "\r":
                raise self.error("unescaped newline in string literal")
            out.append(c)
            self.pos += 1
        raise self.error("unterminated string literal")

    def _parse_unicode_escape(self) -> str:
        text = self.text
        self.pos += 1  # consume 'u'
        if self.pos < self.n and text[self.pos] == "{":
            end = text.find("}", self.pos)
            if end < 0:
                raise self.error("unterminated \\u{...} escape")
            digits = text[self.pos + 1 : end]
            self.pos = end + 1
        else:
            digits = text[self.pos : self.pos + 4]
            if len(digits) != 4:
                raise self.error("invalid \\u escape")
            self.pos += 4
        if not digits or not all(ch in "0123456789abcdefABCDEF" for ch in digits):
            raise self.error("invalid unicode escape digits")
        return chr(int(digits, 16))


# --------------------------------------------------------------------- helpers


def parse_literal_at(text: str, start: int) -> Node:
    """Parse one literal value whose first character is ``text[start]``."""
    parser = JSLiteralParser(text, start)
    node = parser.parse()
    return node


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snippet_hash(text: str, start: int, end: int, limit: int = 4096) -> str:
    """Deterministic hash of an extracted literal (truncated for huge values)."""
    chunk = text[start : min(end, start + limit)]
    return hashlib.sha256(chunk.encode("utf-8")).hexdigest()
