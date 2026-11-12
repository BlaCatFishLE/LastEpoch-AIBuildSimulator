"""CLI smoke tests (run/check with a pre-dumped synthetic snapshot)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support

from le_calc import cli


class CliTest(unittest.TestCase):
    def _write_snapshot(self, directory: Path) -> Path:
        path = directory / "snapshot.json"
        path.write_text(
            json.dumps(support.make_snapshot().to_dict(), ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def test_check_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._write_snapshot(Path(tmp))
            code = cli.main(["check", "--snapshot", str(snap)])
            self.assertEqual(code, 0)

    def test_run_command_writes_artefacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            snap = self._write_snapshot(Path(tmp))
            code = cli.main(
                [
                    "run",
                    "--snapshot",
                    str(snap),
                    "--out",
                    str(out),
                    "--no-html",
                    "--generated-at",
                    "2026-11-04T00:00:00+00:00",
                    "--run-id",
                    "cli-test",
                ]
            )
            self.assertEqual(code, 0)
            result = json.loads((out / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["run"]["run_id"], "cli-test")
            self.assertTrue((out / "run-manifest.json").is_file())
            self.assertTrue((out / "input-snapshot.json").is_file())
            self.assertTrue(result["consistency"]["all_pass"])


if __name__ == "__main__":
    unittest.main()
