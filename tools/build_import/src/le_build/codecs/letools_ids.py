"""LE Tools planner ID codec (marker + LZ-String base64 variant payload).

The archived planner runtime (``archive://planner-runtime.js``,
sha256 ``3b962b5d3281e4ddd1651cb3f3b82bcf8569b50164777d0f9061541c93dd0b86``) serialises
item/affix ids as ``<marker><payload>`` where ``payload`` is LZ-String's
``compressToBase64`` output with the custom alphabet
``ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-$`` and where the
decompressor is the reviewed ``rrlrb.dk`` expression.

Observed markers (this dataset / protocol version):

* ``A`` -> decimal affix id, zero padded to a *minimum* width of 3.
* ``U`` -> ``0`` + 2-digit base subtype id + 3-digit unique id.
* ``I`` -> ``1`` + 3-digit item-type list index + 3-digit base id + tail digits.

Anything else is reported as ``unsupported-codec`` and kept verbatim by the caller.
"""

from __future__ import annotations

from .. import PROTOCOL_VERSION

CODE_VERSION = "letools-id-codec-v1"

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-$"
_ALPHABET_INDEX = {char: index for index, char in enumerate(ALPHABET)}

SUPPORTED_MARKERS = ("A", "U", "I")

EVIDENCE = {
    "codec_version": CODE_VERSION,
    "protocol_version": PROTOCOL_VERSION,
    "runtime_script": "archive://planner-runtime.js",
    "runtime_script_sha256": "3b962b5d3281e4ddd1651cb3f3b82bcf8569b50164777d0f9061541c93dd0b86",
    "codec_expression_chars": [145539, 149610],
    "decompressor_symbol": "rrlrb.dk",
    "compressor_symbol": "rrlrb.Ve",
    "alphabet": ALPHABET,
    "rules": {
        "A": "marker A + decompressed decimal affix id (min width 3, may be 4+ digits)",
        "U": "marker U + 3-digit base subtype id + 3-digit unique id; base item group comes from the unique definition baseTypeId",
        "I": "marker I + '1' + 3-digit item-type list index + 3-digit base id + 3-digit tail (tail is preserved raw)",
    },
}


class CodecError(ValueError):
    """Raised for malformed / unsupported encoded ids."""


# ---------------------------------------------------------------------------
# LZ-String ports (only the pure algorithms from the archived expression)
# ---------------------------------------------------------------------------
def lz_decompress(payload: str, bits_per_char: int = 6) -> str | None:
    """Port of the archived ``rrlrb.Ne`` (decompressFromBase64 with 6 bits/char)."""
    if payload is None:
        return None
    if payload == "":
        return ""
    length = len(payload)
    # The archived ``rrlrb.Ne`` callers pass 32 for 6-bit payloads: the initial data
    # position is 2^(bitsPerChar-1), matching LZ-String's decompressFromBase64.
    reset_value = 1 << (bits_per_char - 1)
    result: list[str] = []
    dictionary: list[str | None] = [None, None, None]
    enlarge_in = 4
    dict_size = 4
    num_bits = 3
    data_val = _ALPHABET_INDEX.get(payload[0], -1)
    data_position = reset_value
    data_index = 1

    def read_bits(count: int) -> int:
        nonlocal data_val, data_position, data_index
        bits = 0
        power = 1
        max_power = 1 << count
        while power != max_power:
            bit = data_val & data_position
            data_position >>= 1
            if data_position == 0:
                data_position = reset_value
                data_val = _ALPHABET_INDEX.get(payload[data_index], -1) if data_index < length else 0
                data_index += 1
            if bit > 0:
                bits |= power
            power <<= 1
        return bits

    code = read_bits(2)
    if code == 0:
        character = chr(read_bits(8))
    elif code == 1:
        character = chr(read_bits(16))
    elif code == 2:
        return ""
    else:
        return None
    dictionary.append(character)
    w = character
    result.append(character)
    while True:
        if data_index - 1 > length:
            return ""
        code = read_bits(num_bits)
        if code == 0:
            dictionary.append(chr(read_bits(8)))
            code = dict_size
            dict_size += 1
            enlarge_in -= 1
        elif code == 1:
            dictionary.append(chr(read_bits(16)))
            code = dict_size
            dict_size += 1
            enlarge_in -= 1
        elif code == 2:
            return "".join(result)
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1
        if code < len(dictionary) and dictionary[code] is not None:
            entry = dictionary[code]
        elif code == dict_size:
            entry = w + w[0]
        else:
            return None
        result.append(entry)
        dictionary.append(w + entry[0])
        dict_size += 1
        enlarge_in -= 1
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1
        w = entry


def decode_payload(marker: str, payload: str) -> dict:
    """Interpret the decompressed payload for one marker.

    Returns a dict with ``status`` in ``decoded`` / ``malformed`` and the parsed
    fields.  Fields are always emitted as strings where padding matters.
    """
    if marker not in SUPPORTED_MARKERS:
        return {
            "marker": marker,
            "payload": payload,
            "status": "unsupported",
            "reason": f"unsupported marker {marker!r}",
        }
    if not payload or not payload.isdigit():
        return {
            "marker": marker,
            "payload": payload,
            "status": "malformed",
            "reason": "payload is not a decimal digit string",
        }
    fields: dict = {"marker": marker, "payload": payload}
    if marker == "A":
        fields["affix_id"] = int(payload)
        fields["pad_width"] = len(payload)
        fields["status"] = "decoded"
    elif marker == "U":
        if len(payload) < 6:
            fields.update(status="malformed", reason="expected subtype (min 3 digits) + 3-digit unique id")
            return fields
        fields["format_marker"] = payload[:-3]
        fields["unique_id"] = int(payload[-3:])
        fields["base_subtype_id"] = int(payload[:-3])
        fields["unique_id_pad"] = payload[-3:]
        fields["status"] = "decoded"
    else:  # marker == "I"
        if len(payload) < 10 or payload[0] != "1":
            fields.update(
                status="malformed",
                reason="expected 1 + 3-digit item-type index + 3-digit base id + 3-digit tail",
            )
            return fields
        fields["format_marker"] = payload[0]
        fields["item_type_index"] = int(payload[1:4])
        fields["base_id"] = int(payload[4:7])
        fields["tail"] = payload[7:]
        fields["status"] = "decoded"
    return fields


def decode_encoded_id(encoded_id: str, *, expected_marker: str | None = None) -> dict:
    """Decode one planner id. Never raises for unsupported/malformed input."""
    result: dict = {
        "codec_version": CODE_VERSION,
        "encoded_id": encoded_id,
        "status": "unsupported",
    }
    if not isinstance(encoded_id, str) or len(encoded_id) < 2:
        result.update(status="malformed", reason="id too short")
        return result
    marker, payload_b64 = encoded_id[0], encoded_id[1:]
    result["marker"] = marker
    if expected_marker and marker != expected_marker:
        result.update(status="malformed", reason=f"expected marker {expected_marker}")
        return result
    payload = lz_decompress(payload_b64, bits_per_char=6)
    if payload is None:
        result.update(status="malformed", reason="LZ decompression failed")
        return result
    parsed = decode_payload(marker, payload)
    result.update(parsed)
    result["compressed_payload"] = payload_b64
    return result


def expected_payload_for_affix(affix_source_id: int) -> str:
    return f"{int(affix_source_id):03d}"


def expected_payload_for_unique(base_subtype_id: int, unique_id: int) -> str:
    """Expected U payload: 3-digit base subtype id + 3-digit unique id (protocol note P2-0)."""
    return f"{int(base_subtype_id):03d}{int(unique_id):03d}"


def expected_payload_for_base(item_type_index: int, base_id: int, tail: str = "000") -> str:
    return f"1{int(item_type_index):03d}{int(base_id):03d}{tail}"
