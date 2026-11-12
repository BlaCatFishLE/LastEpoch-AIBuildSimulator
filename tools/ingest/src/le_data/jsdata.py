"""Safe extraction of JavaScript object/array literals.

The rule for this project (phase-1 workguide section 6.3) is that downloaded
JavaScript must never be executed.  Source sites (Tunklab, Last Epoch Tools)
ship their data as minified JavaScript bundles where the payload is a plain
literal expression such as ``window.coreDB={propertyList:[...]}`` or
``let a={version:"1.5.2",data:{...}};e.s([...])``.

This module implements a small, strict, non-executing parser for that subset
of JavaScript: objects, arrays, strings, numbers, booleans, ``null``,
``!0``/``!1``, ``void 0`` and the constant-only transforms ``.split(x)`` and
``.concat(x)`` (the bundles encode string lists as ``"a b c".split(" ")``).
Anything else (other function calls, identifiers that are not literals,
template strings, spreads, computed keys) raises
:class:`UnsupportedJsConstruct` instead of being silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

__all__ = [
    "JsParseError",
    "UnsupportedJsConstruct",
    "UNDEFINED",
    "Undefined",
    "parse_literal",
    "parse_assignment",
    "find_js_assignments",
    "find_assignment_sites",
    "extract_balanced_object",
    "js_to_jsonable",
]


class JsParseError(ValueError):
    """Raised when a JavaScript literal cannot be parsed."""

    def __init__(self, message: str, text: str = "", pos: int = 0) -> None:
        self.pos = pos
        if text:
            lo = max(0, pos - 60)
            hi = min(len(text), pos + 60)
            snippet = text[lo:hi].replace("\n", "\\n")
            message = f"{message} at offset {pos}: ...{snippet}..."
        super().__init__(message)


class UnsupportedJsConstruct(JsParseError):
    """Raised for syntactically valid JS that is outside the literal subset."""


class Undefined:
    """Sentinel for JavaScript ``undefined`` (kept distinct from null)."""

    __slots__ = ()
    _instance: "Undefined | None" = None

    def __new__(cls) -> "Undefined":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "UNDEFINED"

    def __bool__(self) -> bool:
        return False


UNDEFINED = Undefined()


@dataclass(frozen=True)
class _RegexLiteral:
    source: str
    flags: str = ""


_WHITESPACE = " \t\r\n\v\f\u00a0\ufeff"
_IDENT_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_$")
_IDENT_CONT = _IDENT_START | set("0123456789")
_ESCAPES = {
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "0": "\0",
    "'": "'",
    '"': '"',
    "\\": "\\",
    "/": "/",
}


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.length = len(text)

    # -- low level helpers -------------------------------------------------
    def error(self, message: str) -> None:
        raise JsParseError(message, self.text, self.pos)

    def unsupported(self, message: str) -> None:
        raise UnsupportedJsConstruct(message, self.text, self.pos)

    def skip_ws(self) -> None:
        text = self.text
        pos = self.pos
        n = self.length
        while pos < n:
            ch = text[pos]
            if ch in _WHITESPACE:
                pos += 1
            elif ch == "/" and pos + 1 < n and text[pos + 1] == "/":
                nl = text.find("\n", pos)
                pos = n if nl < 0 else nl + 1
            elif ch == "/" and pos + 1 < n and text[pos + 1] == "*":
                end = text.find("*/", pos + 2)
                if end < 0:
                    self.pos = pos
                    self.error("unterminated block comment")
                pos = end + 2
            else:
                break
        self.pos = pos

    def peek(self) -> str:
        self.skip_ws()
        if self.pos >= self.length:
            return ""
        return self.text[self.pos]

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            self.error(f"expected {ch!r}")
        self.pos += 1

    # -- values ------------------------------------------------------------
    def parse_expression(self) -> Any:
        """Parse a literal value, then allow constant-only postfix transforms."""
        value = self.parse_primary()
        while True:
            self.skip_ws()
            if self.pos >= self.length or self.text[self.pos] != ".":
                return value
            saved = self.pos
            self.pos += 1
            self.skip_ws()
            start = self.pos
            while self.pos < self.length and self.text[self.pos] in _IDENT_CONT:
                self.pos += 1
            method = self.text[start : self.pos]
            self.skip_ws()
            if self.pos >= self.length or self.text[self.pos] != "(":
                self.pos = saved
                return value
            if method == "split":
                value = self._apply_split(value)
            elif method == "concat" and isinstance(value, list):
                value = self._apply_concat(value)
            elif method in {"trim", "toLowerCase", "toUpperCase"} and isinstance(value, str):
                self._expect_empty_call()
                value = getattr(value, "strip" if method == "trim" else
                                 ("lower" if method == "toLowerCase" else "upper"))()
            else:
                self.pos = saved
                self.unsupported(f"method .{method}() is outside the supported constant subset")

    def parse_primary(self) -> Any:
        ch = self.peek()
        if ch == "{":
            return self.parse_object()
        if ch == "[":
            return self.parse_array()
        if ch in ('"', "'"):
            return self.parse_string()
        if ch == "!":
            return self.parse_bang()
        if ch == "-" or ch == "+" or ch.isdigit() or ch == ".":
            return self.parse_number()
        if ch and ch in _IDENT_START:
            return self.parse_keyword()
        if ch == "/":
            return self.parse_regex()
        self.error(f"unexpected {ch!r} while parsing a value")

    def parse_keyword(self) -> Any:
        start = self.pos
        while self.pos < self.length and self.text[self.pos] in _IDENT_CONT:
            self.pos += 1
        word = self.text[start : self.pos]
        if word == "true":
            return True
        if word == "false":
            return False
        if word == "null":
            return None
        if word == "undefined":
            return UNDEFINED
        if word == "void":
            self.skip_ws()
            if self.text.startswith("0", self.pos):
                self.pos += 1
                return UNDEFINED
            self.error("only 'void 0' is supported")
        if word == "NaN":
            return float("nan")
        if word == "Infinity":
            return float("inf")
        self.unsupported(f"identifier {word!r} is not a literal (dynamic value)")

    def parse_bang(self) -> Any:
        self.expect("!")
        self.skip_ws()
        ch = self.peek()
        if ch == "0":
            self.pos += 1
            return True
        if ch == "1":
            self.pos += 1
            return False
        self.unsupported("only '!0' and '!1' are supported")

    def parse_regex(self) -> _RegexLiteral:
        text = self.text
        start = self.pos
        pos = start + 1
        n = self.length
        in_class = False
        while pos < n:
            ch = text[pos]
            if ch == "\\":
                pos += 2
                continue
            if ch == "[":
                in_class = True
            elif ch == "]":
                in_class = False
            elif ch == "/" and not in_class:
                source = text[start + 1 : pos]
                pos += 1
                flag_start = pos
                while pos < n and text[pos].isalpha():
                    pos += 1
                flags = text[flag_start:pos]
                self.pos = pos
                return _RegexLiteral(source, flags)
            pos += 1
        self.error("unterminated regex literal")

    def parse_number(self) -> int | float:
        text = self.text
        start = self.pos
        pos = self.pos
        n = self.length
        if pos < n and text[pos] in "+-":
            pos += 1
        if pos + 1 < n and text[pos] == "0" and text[pos + 1] in "xX":
            pos += 2
            hstart = pos
            while pos < n and text[pos] in "0123456789abcdefABCDEF":
                pos += 1
            if pos == hstart:
                self.error("malformed hex literal")
            self.pos = pos
            return int(text[start:pos], 16)
        seen_dot = False
        seen_exp = False
        while pos < n:
            ch = text[pos]
            if ch.isdigit():
                pos += 1
            elif ch == "." and not seen_dot and not seen_exp:
                seen_dot = True
                pos += 1
            elif ch in "eE" and not seen_exp:
                seen_exp = True
                pos += 1
                if pos < n and text[pos] in "+-":
                    pos += 1
            else:
                break
        raw = text[start:pos]
        self.pos = pos
        try:
            if seen_dot or seen_exp:
                return float(raw)
            return int(raw, 10)
        except ValueError:
            raise JsParseError(f"malformed number {raw!r}", text, start) from None

    def parse_string(self) -> str:
        text = self.text
        quote = text[self.pos]
        pos = self.pos + 1
        n = self.length
        out: list[str] = []
        while pos < n:
            ch = text[pos]
            if ch == quote:
                self.pos = pos + 1
                return "".join(out)
            if ch == "\\":
                pos += 1
                if pos >= n:
                    break
                esc = text[pos]
                if esc in _ESCAPES:
                    out.append(_ESCAPES[esc])
                    pos += 1
                elif esc == "x":
                    if pos + 2 >= n:
                        self.error("truncated \\x escape")
                    out.append(chr(int(text[pos + 1 : pos + 3], 16)))
                    pos += 3
                elif esc == "u":
                    if pos + 1 < n and text[pos + 1] == "{":
                        end = text.find("}", pos + 2)
                        if end < 0:
                            self.error("unterminated \\u{...} escape")
                        out.append(chr(int(text[pos + 2 : end], 16)))
                        pos = end + 1
                    else:
                        if pos + 4 >= n:
                            self.error("truncated \\u escape")
                        out.append(chr(int(text[pos + 1 : pos + 5], 16)))
                        pos += 5
                elif esc == "\n":
                    pos += 1
                elif esc == "\r":
                    pos += 2 if text.startswith("\r\n", pos) else 1
                else:
                    out.append(esc)
                    pos += 1
                continue
            if ch in "\r\n":
                self.error("unterminated string literal")
            out.append(ch)
            pos += 1
        self.error("unterminated string literal")

    def parse_array(self) -> list[Any]:
        self.expect("[")
        result: list[Any] = []
        if self.peek() == "]":
            self.pos += 1
            return result
        while True:
            self.skip_ws()
            if self.text.startswith("...", self.pos):
                self.unsupported("array spread is not supported")
            if self.peek() == "]":
                self.pos += 1
                return result
            result.append(self.parse_expression())
            ch = self.peek()
            if ch == ",":
                self.pos += 1
                continue
            if ch == "]":
                self.pos += 1
                return result
            self.error("expected ',' or ']' in array")

    def parse_object(self) -> dict[str, Any]:
        self.expect("{")
        result: dict[str, Any] = {}
        if self.peek() == "}":
            self.pos += 1
            return result
        while True:
            self.skip_ws()
            if self.text.startswith("...", self.pos):
                self.unsupported("object spread is not supported")
            if self.peek() == "}":
                self.pos += 1
                return result
            key = self.parse_key()
            self.expect(":")
            result[key] = self.parse_expression()
            ch = self.peek()
            if ch == ",":
                self.pos += 1
                continue
            if ch == "}":
                self.pos += 1
                return result
            self.error("expected ',' or '}' in object")

    def parse_key(self) -> str:
        text = self.text
        pos = self.pos
        ch = text[pos] if pos < self.length else ""
        if ch in ('"', "'"):
            return self.parse_string()
        if ch.isdigit():
            num = self.parse_number()
            return str(num)
        if ch and ch in _IDENT_START:
            start = pos
            pos += 1
            while pos < self.length and text[pos] in _IDENT_CONT:
                pos += 1
            self.pos = pos
            return text[start:pos]
        if ch == "[":
            self.unsupported("computed keys are not supported")
        self.error(f"unexpected {ch!r} while parsing object key")

    # -- constant-only calls -------------------------------------------------
    def _read_call_argument(self) -> Any:
        self.expect("(")
        self.skip_ws()
        if self.pos < self.length and self.text.startswith(")", self.pos):
            self.unsupported("call without an argument is not supported")
        arg = self.parse_expression()
        self.skip_ws()
        if self.pos >= self.length or not self.text.startswith(")", self.pos):
            self.unsupported("multi-argument calls are not supported")
        self.pos += 1
        return arg

    def _expect_empty_call(self) -> None:
        self.expect("(")
        self.skip_ws()
        if self.pos >= self.length or not self.text.startswith(")", self.pos):
            self.unsupported("arguments are not supported for this method")
        self.pos += 1

    def _apply_split(self, value: Any) -> list[Any]:
        if not isinstance(value, str):
            self.unsupported("split() on a non-string constant is not supported")
        separator = self._read_call_argument()
        if isinstance(separator, str):
            return value.split(separator)
        if isinstance(separator, _RegexLiteral):
            flags = 0
            if "i" in separator.flags:
                flags |= re.IGNORECASE
            return re.split(separator.source, value, flags=flags)
        self.unsupported("split() separator must be a string or regex literal")

    def _apply_concat(self, value: list[Any]) -> list[Any]:
        arg = self._read_call_argument()
        if isinstance(arg, list):
            return value + arg
        if isinstance(arg, str):
            return value + [arg]
        self.unsupported("concat() with a non-array, non-string argument is not supported")


def parse_literal(text: str, start: int = 0) -> tuple[Any, int]:
    """Parse one JS literal starting at ``start``; return (value, end_offset)."""
    parser = _Parser(text)
    parser.pos = start
    value = parser.parse_expression()
    return value, parser.pos


def parse_assignment(text: str, name: str, start: int = 0) -> tuple[Any, int, int]:
    """Parse ``<name>=<literal>`` and return (value, value_start, value_end)."""
    needle = name + "="
    idx = text.find(needle, start)
    if idx < 0:
        raise JsParseError(f"assignment to {name!r} not found", text, start)
    value_start = idx + len(needle)
    value, end = parse_literal(text, value_start)
    return value, value_start, end


def find_assignment_sites(text: str, prefix: str = "window.") -> Iterator[tuple[str, int]]:
    """Yield (name, value_start) for every ``prefix<name>=`` site in ``text``."""
    search = 0
    length = len(text)
    while True:
        idx = text.find(prefix, search)
        if idx < 0:
            return
        name_start = idx + len(prefix)
        pos = name_start
        while pos < length and text[pos] in _IDENT_CONT:
            pos += 1
        name = text[name_start:pos]
        if not name:
            search = name_start
            continue
        eq = pos
        while eq < length and text[eq] in _WHITESPACE:
            eq += 1
        if eq >= length or text[eq] != "=":
            search = pos
            continue
        value_start = eq + 1
        yield name, value_start
        search = value_start


def find_js_assignments(
    text: str, prefix: str = "window."
) -> Iterator[tuple[str, Any, int, int]]:
    """Yield (name, value, value_start, value_end) for each parseable assignment.

    Assignments whose right-hand side is outside the supported literal subset
    are skipped (the caller can enumerate them separately with
    :func:`find_assignment_sites` to report unsupported constructs).
    """
    for name, value_start in find_assignment_sites(text, prefix):
        try:
            value, end = parse_literal(text, value_start)
        except UnsupportedJsConstruct:
            continue
        yield name, value, value_start, end


def extract_balanced_object(text: str, start: int) -> tuple[dict[str, Any], int]:
    """Parse an object literal beginning at ``start`` and return (object, end)."""
    if start >= len(text) or text[start] != "{":
        raise JsParseError("expected '{'", text, start)
    value, end = parse_literal(text, start)
    if not isinstance(value, dict):
        raise JsParseError("expected object literal", text, start)
    return value, end


def js_to_jsonable(value: Any) -> Any:
    """Convert parsed JS values to JSON-serialisable Python values.

    ``undefined`` becomes a sentinel object ``{"__js_undefined__": true}`` so
    that raw payloads keep the distinction from ``null``.
    """
    if value is UNDEFINED:
        return {"__js_undefined__": True}
    if isinstance(value, _RegexLiteral):
        return {"__js_regex__": value.source, "flags": value.flags}
    if isinstance(value, float):
        if value != value:  # NaN
            return {"__js_number__": "NaN"}
        if value in (float("inf"), float("-inf")):
            return {"__js_number__": "Infinity" if value > 0 else "-Infinity"}
        return value
    if isinstance(value, dict):
        return {key: js_to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [js_to_jsonable(item) for item in value]
    return value
