"""Independent tests for the LE Tools planner id codec (P2-5).

The fixture ``reports/discovery/phase-2-o3ZRm9my/decoded-id-probe.json`` was produced
during discovery, *before* the importer existed: it maps 55 real encoded planner ids to
the exact string the archived site decompressor returns for them.  These tests treat
that file as the authority and do not re-derive it from the importer.

The LZ-String compressor below is an independent port of the standard LZ-String
``compressToBase64`` (6 bits/char, same alphabet as the archived expression).  It is
used in both directions:

* decompression of every archived payload must reproduce the fixture text;
* re-compressing that text must reproduce the archived payload byte-for-byte, which is
  a much stronger claim than a one-way decode (it pins encoder *and* decoder).

Run from ``tools/build_import`` (no network is used anywhere in this module)::

    & $py -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import json
import socket
import unittest
from pathlib import Path
from unittest import mock

from le_build.codecs import letools_ids

REPO = Path(__file__).resolve().parents[3]
DISCOVERY = REPO / "reports" / "discovery" / "phase-2-o3ZRm9my"
PROBE_PATH = DISCOVERY / "decoded-id-probe.json"
CAPTURE_META_PATH = DISCOVERY / "capture-meta.json"
RUNTIME_PATH = DISCOVERY / "planner-runtime.js"

#: Standard LZ-String base64 alphabet (also recorded in letools_ids.ALPHABET).
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-$"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def reference_lz_compress(text: str, bits_per_char: int = 6) -> str:
    """Independent port of LZ-String's ``_compress`` + ``compressToBase64``.

    Written from the published LZ-String algorithm, not from ``letools_ids``.  No
    padding is appended because the archived planner runtime emits unpadded base64.
    """
    output: list[str] = []
    data_val = 0
    data_position = 0
    dictionary: dict[str, int] = {}
    to_create: dict[str, bool] = {}
    w = ""
    enlarge_in = 2
    dict_size = 3
    num_bits = 2

    def emit_bit(bit: int) -> None:
        nonlocal data_val, data_position
        data_val = (data_val << 1) | bit
        if data_position == bits_per_char - 1:
            output.append(ALPHABET[data_val])
            data_val = 0
            data_position = 0
        else:
            data_position += 1

    def emit_code(code: int, width: int) -> None:
        for _ in range(width):
            emit_bit(code & 1)
            code >>= 1

    def emit_literal(character: str) -> None:
        code = ord(character)
        # A literal is prefixed by ``num_bits`` zero bits (0b00 / 0b01 markers).
        for _ in range(num_bits):
            emit_bit(0)
        emit_code(code, 8 if code < 256 else 16)

    for character in text:
        if character not in dictionary:
            dictionary[character] = dict_size
            dict_size += 1
            to_create[character] = True
        wc = w + character
        if wc in dictionary:
            w = wc
            continue
        if w in to_create:
            emit_literal(w)
            enlarge_in -= 1
            if enlarge_in == 0:
                enlarge_in = 1 << num_bits
                num_bits += 1
            del to_create[w]
        else:
            emit_code(dictionary[w], num_bits)
        enlarge_in -= 1
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1
        dictionary[wc] = dict_size
        dict_size += 1
        w = character

    if w:
        if w in to_create:
            emit_literal(w)
            enlarge_in -= 1
            if enlarge_in == 0:
                enlarge_in = 1 << num_bits
                num_bits += 1
        else:
            emit_code(dictionary[w], num_bits)
        enlarge_in -= 1
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1

    # End-of-stream marker 0b10 (value 2) using the current bit width.
    emit_code(2, num_bits)
    while True:  # flush the final partial byte
        data_val <<= 1
        if data_position == bits_per_char - 1:
            output.append(ALPHABET[data_val])
            break
        data_position += 1
    return "".join(output)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class CodecFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.probe = json.loads(PROBE_PATH.read_text(encoding="utf-8"))
        cls.capture_meta = json.loads(CAPTURE_META_PATH.read_text(encoding="utf-8"))

    # ------------------------------------------------------------- fixture shape
    def test_probe_fixture_has_55_entries_with_supported_markers(self) -> None:
        self.assertEqual(len(self.probe), 55)
        markers = [encoded[0] for encoded in self.probe]
        self.assertEqual(set(markers), set(letools_ids.SUPPORTED_MARKERS))
        # Every expected value is the plain decompressed payload string.
        for encoded, expected in self.probe.items():
            self.assertIsInstance(encoded, str)
            self.assertIsInstance(expected, str)
            self.assertGreaterEqual(len(encoded), 2)
            self.assertTrue(expected.isdigit(), f"{encoded!r} -> {expected!r}")

    # ----------------------------------------------------------------- LZ layer
    def test_decompresses_every_archived_payload_to_its_fixture_value(self) -> None:
        for encoded, expected in self.probe.items():
            marker, payload = encoded[0], encoded[1:]
            self.assertEqual(
                letools_ids.lz_decompress(payload),
                expected,
                f"decompressor disagrees with the archived fixture for {encoded!r}",
            )
            self.assertEqual(marker in letools_ids.SUPPORTED_MARKERS, True)

    def test_recompressing_fixture_value_reproduces_archived_payload_exactly(self) -> None:
        """Independent encoder output must be byte-identical to the site payload."""
        mismatched = {
            encoded: reference_lz_compress(expected)
            for encoded, expected in self.probe.items()
            if reference_lz_compress(expected) != encoded[1:]
        }
        self.assertEqual(mismatched, {}, "independent LZ-String encoder disagrees with archive")

    def test_lz_round_trip_through_the_importer_decompressor(self) -> None:
        for encoded, expected in self.probe.items():
            compressed = reference_lz_compress(expected)
            self.assertEqual(
                letools_ids.lz_decompress(compressed),
                expected,
                f"round-trip failed for fixture text {expected!r} ({encoded!r})",
            )

    def test_decompressor_empty_and_none(self) -> None:
        self.assertIsNone(letools_ids.lz_decompress(None))
        self.assertEqual(letools_ids.lz_decompress(""), "")

    def test_invalid_compressed_payloads_are_rejected(self) -> None:
        self.assertIsNone(letools_ids.lz_decompress("===="))
        self.assertIsNone(letools_ids.lz_decompress("!!!!"))

    # ------------------------------------------------------- A / U / I semantics
    def test_decode_all_55_entries_exact_payload_semantics(self) -> None:
        for encoded, expected in sorted(self.probe.items()):
            decoded = letools_ids.decode_encoded_id(encoded)
            self.assertEqual(decoded["status"], "decoded", f"{encoded}: {decoded}")
            self.assertEqual(decoded["marker"], encoded[0])
            self.assertEqual(decoded["payload"], expected)
            self.assertEqual(decoded["compressed_payload"], encoded[1:])
            marker = encoded[0]
            if marker == "A":
                self.assertEqual(decoded["affix_id"], int(expected), encoded)
                self.assertEqual(decoded["pad_width"], len(expected), encoded)
                self.assertEqual(
                    letools_ids.expected_payload_for_affix(decoded["affix_id"]),
                    expected,
                    encoded,
                )
            elif marker == "U":
                # 0 + 2-digit base subtype + 3-digit unique id (payload may carry
                # a zero prefix making the subtype wider than 2 digits).
                self.assertEqual(decoded["unique_id"], int(expected[-3:]), encoded)
                self.assertEqual(decoded["base_subtype_id"], int(expected[:-3]), encoded)
                self.assertEqual(decoded["unique_id_pad"], expected[-3:], encoded)
                self.assertEqual(
                    letools_ids.expected_payload_for_unique(
                        decoded["base_subtype_id"], decoded["unique_id"]
                    ),
                    expected,
                    encoded,
                )
            else:  # marker == "I"
                self.assertEqual(decoded["format_marker"], "1", encoded)
                self.assertEqual(decoded["item_type_index"], int(expected[1:4]), encoded)
                self.assertEqual(decoded["base_id"], int(expected[4:7]), encoded)
                self.assertEqual(decoded["tail"], expected[7:], encoded)
                self.assertEqual(
                    letools_ids.expected_payload_for_base(
                        decoded["item_type_index"], decoded["base_id"], decoded["tail"]
                    ),
                    expected,
                    encoded,
                )

    def test_marker_a_parsing_examples(self) -> None:
        # (encoded id, decoded payload text, affix id, payload width)
        cases = [
            ("AAwRgzEA", "013", 13, 3),
            ("AIwBhQ", "100", 100, 3),
            ("AOwJgDEA", "720", 720, 3),
            ("AKzAsQ", "554", 554, 3),
            ("AIwBgnAzEA", "1093", 1093, 4),
            ("AIzAsHYg", "1147", 1147, 4),
        ]
        for encoded, payload, affix_id, width in cases:
            decoded = letools_ids.decode_encoded_id(encoded)
            self.assertEqual(decoded["status"], "decoded", encoded)
            self.assertEqual(decoded["marker"], "A")
            self.assertEqual(decoded["payload"], payload)
            self.assertEqual(decoded["affix_id"], affix_id)
            # Width is the observed padding, never a hardcoded three characters.
            self.assertEqual(decoded["pad_width"], width)

    def test_marker_u_parsing_examples(self) -> None:
        cases = [
            # encoded, subtype, unique id
            ("UAwJgzGCMBsQ", 23, 316),
            ("UAzDsGZ2I", 7, 330),
            ("UAzBswTgRiA", 6, 91),
            ("UAzBMEYHYFYg", 2, 175),
            ("UAzDMoFgJiA", 3, 342),
        ]
        for encoded, subtype, unique_id in cases:
            decoded = letools_ids.decode_encoded_id(encoded)
            self.assertEqual(decoded["status"], "decoded", encoded)
            self.assertEqual(decoded["marker"], "U")
            self.assertEqual(decoded["base_subtype_id"], subtype, encoded)
            self.assertEqual(decoded["unique_id"], unique_id, encoded)

    def test_marker_i_parsing_examples(self) -> None:
        cases = [
            # encoded, item-type index, base id, tail
            ("IIwBhCYwNjEg", 2, 6, "000"),
            ("IIwBgTKKSQ", 21, 1, "000"),
            ("IIwBgzALMwGwvQ", 34, 116, "000"),
            ("IIzAMHZQZlXSA", 107, 30, "000"),
            ("IIwBgTAbC00A", 26, 0, "000"),
        ]
        for encoded, type_index, base_id, tail in cases:
            decoded = letools_ids.decode_encoded_id(encoded)
            self.assertEqual(decoded["status"], "decoded", encoded)
            self.assertEqual(decoded["marker"], "I")
            self.assertEqual(decoded["item_type_index"], type_index, encoded)
            self.assertEqual(decoded["base_id"], base_id, encoded)
            self.assertEqual(decoded["tail"], tail, encoded)

    # ---------------------------------------------------- malformed / unsupported
    def test_unsupported_marker_is_reported_not_decoded(self) -> None:
        encoded = "Z" + reference_lz_compress("123")
        decoded = letools_ids.decode_encoded_id(encoded)
        self.assertEqual(decoded["status"], "unsupported")
        self.assertEqual(decoded["marker"], "Z")
        self.assertEqual(decoded["payload"], "123")
        self.assertIn("unsupported marker", decoded["reason"])

    def test_wrong_expected_marker_is_malformed(self) -> None:
        # A real affix id but the caller demands an item ("I") marker.
        decoded = letools_ids.decode_encoded_id("AAwRgzEA", expected_marker="I")
        self.assertEqual(decoded["status"], "malformed")
        self.assertEqual(decoded["marker"], "A")
        self.assertIn("expected marker I", decoded["reason"])
        # The same id with the correct expectation decodes.
        self.assertEqual(
            letools_ids.decode_encoded_id("AAwRgzEA", expected_marker="A")["status"],
            "decoded",
        )

    def test_non_decimal_payload_is_malformed(self) -> None:
        decoded = letools_ids.decode_encoded_id("A" + reference_lz_compress("12a"))
        self.assertEqual(decoded["status"], "malformed")
        self.assertEqual(decoded["payload"], "12a")
        self.assertIn("not a decimal digit string", decoded["reason"])

    def test_short_u_and_i_payloads_are_malformed(self) -> None:
        short_u = letools_ids.decode_encoded_id("U" + reference_lz_compress("12"))
        self.assertEqual(short_u["status"], "malformed")
        self.assertIn("subtype", short_u["reason"])

        short_i = letools_ids.decode_encoded_id("I" + reference_lz_compress("100200600"))
        self.assertEqual(short_i["status"], "malformed")

        wrong_prefix_i = letools_ids.decode_encoded_id("I" + reference_lz_compress("2002006000"))
        self.assertEqual(wrong_prefix_i["status"], "malformed")

    def test_too_short_and_empty_ids_are_malformed(self) -> None:
        for encoded in ("", "A", "I", "U"):
            decoded = letools_ids.decode_encoded_id(encoded)
            self.assertEqual(decoded["status"], "malformed", encoded)
            self.assertEqual(decoded["reason"], "id too short")
        self.assertEqual(letools_ids.decode_encoded_id(None)["status"], "malformed")

    def test_invalid_lz_payload_is_malformed(self) -> None:
        decoded = letools_ids.decode_encoded_id("A====")
        self.assertEqual(decoded["status"], "malformed")
        self.assertIn("LZ decompression failed", decoded["reason"])

    # ------------------------------------------------------------- evidence file
    def test_evidence_matches_archived_capture_metadata(self) -> None:
        runtime = self.capture_meta["planner_runtime"]
        self.assertEqual(letools_ids.EVIDENCE["runtime_script_sha256"], runtime["sha256"])
        self.assertEqual(
            letools_ids.EVIDENCE["codec_expression_chars"],
            [runtime["codec_expression_char_start"], runtime["codec_expression_char_end"]],
        )
        self.assertEqual(letools_ids.EVIDENCE["alphabet"], letools_ids.ALPHABET)
        self.assertEqual(letools_ids.EVIDENCE["compressor_symbol"], "rrlrb.Ve")
        self.assertEqual(letools_ids.EVIDENCE["decompressor_symbol"], "rrlrb.dk")
        # The referenced archive is actually present locally and unmodified.
        self.assertTrue(RUNTIME_PATH.is_file())
        self.assertEqual(sha256_file(RUNTIME_PATH), runtime["sha256"])

    # ----------------------------------------------------------------- no network
    def test_decoding_performs_no_network_io(self) -> None:
        def _forbidden(*args, **kwargs):
            raise AssertionError("codec decoding attempted network I/O")

        with mock.patch("socket.socket", side_effect=_forbidden), mock.patch(
            "urllib.request.urlopen", side_effect=_forbidden
        ):
            for encoded, expected in self.probe.items():
                decoded = letools_ids.decode_encoded_id(encoded)
                self.assertEqual(decoded["status"], "decoded")
                self.assertEqual(decoded["payload"], expected)


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
