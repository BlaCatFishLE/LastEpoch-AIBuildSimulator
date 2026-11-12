"""Tests for the safe static JS-literal parser (never executes JavaScript)."""

from __future__ import annotations

import sys
import pathlib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _support  # noqa: E402,F401

from affix_db.js_literal import (  # noqa: E402
    JSLiteralError,
    JSLiteralParser,
    parse_literal_at,
)


class LiteralParsingTests(unittest.TestCase):
    def test_minified_object_with_js_booleans(self) -> None:
        text = '{version:"1.5.2",data:{items:[{id:1,name:"a\\"b",ok:!0,no:!1,n:null,u:void 0,x:0x10,y:.5,z:-2.5e2,},],}}'
        node = parse_literal_at(text, 0)
        value = node.value
        self.assertEqual(value["version"], "1.5.2")
        item = value["data"]["items"][0]
        self.assertEqual(item["name"], 'a"b')
        self.assertIs(item["ok"], True)
        self.assertIs(item["no"], False)
        self.assertIsNone(item["n"])
        self.assertIsNone(item["u"])
        self.assertEqual(item["x"], 16)
        self.assertEqual(item["y"], 0.5)
        self.assertEqual(item["z"], -250.0)

    def test_unicode_escapes(self) -> None:
        node = parse_literal_at(r'{"a":"\u0041\x42\u{43}\n"}', 0)
        self.assertEqual(node.value["a"], "ABC\n")

    def test_comments_are_skipped(self) -> None:
        node = parse_literal_at('{/* c */a:1,// line\nb:[2,3]}', 0)
        self.assertEqual(node.value, {"a": 1, "b": [2, 3]})

    def test_span_is_exact(self) -> None:
        text = 'prefix {version:"x",data:[1,2]} suffix'
        start = text.index("{")
        node = parse_literal_at(text, start)
        self.assertEqual(node.start, start)
        self.assertEqual(text[node.start : node.end], '{version:"x",data:[1,2]}')

    def test_parse_from_offset(self) -> None:
        text = 'junk;;{"a":1};more'
        node = parse_literal_at(text, text.index("{"))
        self.assertEqual(node.value, {"a": 1})


class RejectionTests(unittest.TestCase):
    """Anything outside the literal grammar must fail closed (no eval-like behaviour)."""

    def assert_rejected(self, text: str) -> None:
        with self.assertRaises(JSLiteralError):
            parse_literal_at(text, 0)

    def test_rejects_function_call(self) -> None:
        self.assert_rejected('{version:"x",data:alert(1)}')

    def test_rejects_spread(self) -> None:
        self.assert_rejected('{a:[...other]}')

    def test_rejects_template_literal(self) -> None:
        self.assert_rejected('{a:`hello ${x}`}')

    def test_rejects_bare_identifier_value(self) -> None:
        self.assert_rejected('{a:someGlobal}')

    def test_rejects_arrow_function(self) -> None:
        self.assert_rejected('{a:()=>1}')

    def test_rejects_unterminated_object(self) -> None:
        self.assert_rejected('{version:"x",data:[1,2]')

    def test_rejects_unescaped_newline_in_string(self) -> None:
        self.assert_rejected('{a:"line1\nline2"}')


class StreamingParserTests(unittest.TestCase):
    def test_parser_position_after_value(self) -> None:
        text = '{"a":1} trailing'
        parser = JSLiteralParser(text)
        node = parser.parse()
        self.assertEqual(node.value, {"a": 1})
        self.assertEqual(parser.pos, len('{"a":1}'))


if __name__ == "__main__":
    unittest.main()
