"""Tests for the non-executing JavaScript literal parser."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from le_data.jsdata import (  # noqa: E402
    JsParseError,
    UNDEFINED,
    UnsupportedJsConstruct,
    extract_balanced_object,
    find_assignment_sites,
    find_js_assignments,
    js_to_jsonable,
    parse_literal,
)


class LiteralParserTests(unittest.TestCase):
    def parse(self, text: str):
        value, end = parse_literal(text, 0)
        self.assertEqual(end, len(text))
        return value

    def test_object_with_unquoted_keys(self):
        self.assertEqual(
            self.parse('{name:"Fireball",id:7,ok:!0,no:!1,nothing:null}'),
            {"name": "Fireball", "id": 7, "ok": True, "no": False, "nothing": None},
        )

    def test_nested_structures(self):
        value = self.parse('{a:[{b:1},{c:"x"}],d:{e:.5,f:-2.5e3}}')
        self.assertEqual(value["a"], [{"b": 1}, {"c": "x"}])
        self.assertEqual(value["d"], {"e": 0.5, "f": -2500.0})

    def test_string_escapes(self):
        self.assertEqual(self.parse(r'"a\"b\\c\n"'), 'a"b\\c\n')

    def test_undefined_is_distinct_from_null(self):
        self.assertIs(self.parse("[void 0, undefined, null]")[0], UNDEFINED)
        self.assertIsNone(self.parse("[void 0, undefined, null]")[2])
        self.assertEqual(js_to_jsonable(UNDEFINED), {"__js_undefined__": True})

    def test_split_constant_transform(self):
        self.assertEqual(self.parse('"a b c".split(" ")'), ["a", "b", "c"])
        self.assertEqual(self.parse('"a;b".split(";")'), ["a", "b"])

    def test_regex_split(self):
        self.assertEqual(self.parse(r'"a1b22c".split(/\d+/)'), ["a", "b", "c"])

    def test_trailing_commas(self):
        self.assertEqual(self.parse("[1,2,]"), [1, 2])
        self.assertEqual(self.parse('{a:1,}'), {"a": 1})

    def test_comments_are_skipped(self):
        self.assertEqual(self.parse("/* c */ [1, // line\n 2]"), [1, 2])

    def test_unsupported_identifier_raises(self):
        with self.assertRaises(UnsupportedJsConstruct):
            parse_literal("someVariable", 0)

    def test_unsupported_method_raises(self):
        with self.assertRaises(UnsupportedJsConstruct):
            parse_literal('"x".toFixed(2)', 0)

    def test_object_spread_raises(self):
        with self.assertRaises(UnsupportedJsConstruct):
            parse_literal("{...other}", 0)

    def test_truncated_input_raises(self):
        with self.assertRaises(JsParseError):
            parse_literal('{a:[1,2}', 0)

    def test_extract_balanced_object(self):
        text = 'noise {version:"1.5.2",data:{a:[1]}} tail'
        obj, end = extract_balanced_object(text, text.find("{"))
        self.assertEqual(obj["data"], {"a": [1]})
        self.assertEqual(text[end:].strip(), "tail")

    def test_find_assignments(self):
        text = 'window.a={x:1};window.b=[1,2];foo=3;window.c="z"'
        names = {name for name, _value, _start, _end in find_js_assignments(text)}
        self.assertEqual(names, {"a", "b", "c"})
        sites = [name for name, _start in find_assignment_sites(text)]
        self.assertEqual(sites, ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
