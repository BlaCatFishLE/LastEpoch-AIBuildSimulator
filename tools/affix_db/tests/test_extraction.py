"""Extraction tests against the frozen source snapshot (offline)."""

from __future__ import annotations

import hashlib
import sys
import pathlib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _support  # noqa: E402

from affix_db.extract import extract_snapshot  # noqa: E402


@unittest.skipUnless(_support.SNAPSHOT_DIR.is_dir(), "frozen source snapshot not present")
class ExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.extraction = extract_snapshot(_support.SNAPSHOT_DIR / "chunks")

    def test_required_datasets_found(self) -> None:
        kinds = set(self.extraction.datasets)
        self.assertEqual(
            kinds,
            {"affixes", "equipment", "player_properties", "player_stat_properties", "ability_properties"},
        )
        self.assertEqual(len(self.extraction.datasets["affixes"].value), 1156)
        self.assertEqual(len(self.extraction.datasets["equipment"].value["equippableItems"]), 39)
        self.assertEqual(len(self.extraction.datasets["player_properties"].value), 113)
        self.assertEqual(len(self.extraction.datasets["player_stat_properties"].value), 713)
        self.assertEqual(len(self.extraction.datasets["ability_properties"].value), 177)

    def test_source_version_is_1_5_2(self) -> None:
        for kind, dataset in self.extraction.datasets.items():
            self.assertEqual(dataset.version, "1.5.2", msg=kind)

    def test_data_chunk_is_the_expected_sha256(self) -> None:
        data_chunk = self.extraction.data_chunk
        self.assertTrue(data_chunk.file_name.endswith(".js"))
        self.assertEqual(len(data_chunk.sha256), 64)
        self.assertGreater(data_chunk.size_bytes, 7_000_000)

    def test_locators_point_at_the_literal(self) -> None:
        chunk_path = _support.SNAPSHOT_DIR / self.extraction.data_chunk.file_name
        raw = chunk_path.read_bytes()
        for kind, dataset in self.extraction.datasets.items():
            with self.subTest(kind=kind):
                self.assertEqual(dataset.chunk_sha256, hashlib.sha256(raw).hexdigest())
                prefix = raw[dataset.byte_start : dataset.byte_start + 40].decode("utf-8", "replace")
                self.assertIn("version", prefix)
                snippet = raw[dataset.byte_start : dataset.byte_end].decode("utf-8", "replace")
                self.assertIn('"1.5.2"', snippet[:200])

    def test_enum_anchors_verified(self) -> None:
        self.assertTrue(all(self.extraction.enum_anchors.values()), msg=self.extraction.enum_anchors)
        self.assertGreaterEqual(len(self.extraction.stat_property_enum), 130)
        self.assertEqual(self.extraction.stat_property_enum[13], "FireResistance")
        self.assertEqual(self.extraction.stat_property_enum[98], "PlayerProperty")
        self.assertEqual(self.extraction.stat_property_enum[120], "EffectOfAilmentOnYou")
        self.assertEqual(self.extraction.stat_property_enum[130], "IdolAltarProperty")

    def test_page_code_chunk_found(self) -> None:
        page_code = self.extraction.page_code_chunk
        self.assertIsNotNone(page_code)
        self.assertTrue(page_code.file_name.endswith("0-8itj8_g2l_7.js"))


if __name__ == "__main__":
    unittest.main()
