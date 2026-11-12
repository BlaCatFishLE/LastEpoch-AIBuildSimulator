"""CLI smoke tests (subprocess, using the run.py launcher from the repo root)."""

from __future__ import annotations

import json
import subprocess
import sys
import pathlib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _support  # noqa: E402

RUN_PY = _support.AFFIX_DB_DIR / "run.py"


def run_cli(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RUN_PY), *args],
        capture_output=True,
        timeout=timeout,
    )


class CliTests(unittest.TestCase):
    def test_help(self) -> None:
        result = run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"query", result.stdout)

    @unittest.skipUnless(_support.DB_PATH.is_file(), "built affix DB not present")
    def test_query_fire_resistance_body_armor(self) -> None:
        result = run_cli("query", "--affix", "fire_resistance", "--base", "Body Armor", "--tier", "7")
        self.assertEqual(result.returncode, 0, msg=result.stderr.decode("utf-8", "replace"))
        stdout = result.stdout.decode("utf-8", "replace")
        self.assertIn("92-112%", stdout)
        self.assertIn("0.915..1.125", stdout)
        self.assertIn("eligible=True", stdout)

    @unittest.skipUnless(_support.DB_PATH.is_file(), "built affix DB not present")
    def test_query_tunklab_rounding(self) -> None:
        result = run_cli("query", "--affix", "13", "--base", "1", "--tier", "7", "--rounding", "tunklab")
        self.assertEqual(result.returncode, 0)
        self.assertIn("92-113%", result.stdout.decode("utf-8", "replace"))

    @unittest.skipUnless(_support.DB_PATH.is_file(), "built affix DB not present")
    def test_query_json(self) -> None:
        result = run_cli("query", "--affix", "13", "--base", "1", "--tier", "7", "--json")
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(payload["affix"]["slug"], "fire_resistance")
        self.assertEqual(payload["base_type"]["base_type_id"], 1)
        self.assertEqual(len(payload["ranges"]), 1)
        self.assertEqual(payload["ranges"][0]["display_max"], 112)
        self.assertEqual(payload["ranges"][0]["tunklab_display_max"], 113)

    @unittest.skipUnless(_support.DB_PATH.is_file(), "built affix DB not present")
    def test_query_ineligible_base(self) -> None:
        result = run_cli("query", "--affix", "fire_resistance", "--base", "9", "--tier", "7")
        self.assertEqual(result.returncode, 3)
        self.assertIn("eligible=False", result.stdout.decode("utf-8", "replace"))

    @unittest.skipUnless(_support.DB_PATH.is_file(), "built affix DB not present")
    def test_query_throne_of_ambition(self) -> None:
        result = run_cli(
            "query", "--affix", "idol_all_resistances_for_you_and_your_minions", "--base", "Adorned Idol", "--tier", "1"
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr.decode("utf-8", "replace"))
        stdout = result.stdout.decode("utf-8", "replace")
        self.assertIn("0.04470588235294118", stdout)
        self.assertIn("4%", stdout)

    @unittest.skipUnless(_support.DB_PATH.is_file(), "built affix DB not present")
    def test_query_omen_special_path(self) -> None:
        result = run_cli("query", "--affix", "111", "--omen-sub-base", "grand_arcane_omen_idol")
        self.assertEqual(result.returncode, 0, msg=result.stderr.decode("utf-8", "replace"))
        stdout = result.stdout.decode("utf-8", "replace")
        self.assertIn("20-45%", stdout)
        self.assertIn("[DERIVED", stdout)
        self.assertIn("unresolved", stdout)

    @unittest.skipUnless(_support.DB_PATH.is_file(), "built affix DB not present")
    def test_validate(self) -> None:
        result = run_cli("validate")
        self.assertEqual(result.returncode, 0, msg=result.stdout.decode("utf-8", "replace"))
        self.assertIn("RESULT: PASS", result.stdout.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
