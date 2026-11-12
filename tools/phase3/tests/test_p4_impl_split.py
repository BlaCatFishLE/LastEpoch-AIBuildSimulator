# -*- coding: utf-8 -*-
"""P4-R4: implementation fingerprints split by numeric/validation/render groups."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRATCH = ROOT / ".scratch"
SCRATCH.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "tools" / "phase3" / "src"))

from le_calc.workflow import stage  # noqa: E402
from le_calc.workflow import stage_cache  # noqa: E402


class ImplementationSplitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="p4-r4-", dir=SCRATCH)
        cls.root = Path(cls._tmp.name)
        for rel in ("tools/phase3/src/le_calc", "tools/phase3/rules"):
            source = ROOT / rel
            target = cls.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _fingerprint(self, stage_id: str) -> str:
        handler = stage.STAGES[stage_id]["handler"]
        return stage_cache.implementation_fingerprint(
            stage_id, self.root, handler=handler
        )["fingerprint"]

    def _assert_change(
        self, relative: str, *, changed: tuple[str, ...], unchanged: tuple[str, ...], suffix: str = "\n# r4 probe\n"
    ) -> None:
        before = {stage_id: self._fingerprint(stage_id) for stage_id in changed + unchanged}
        path = self.root / relative
        original = path.read_bytes()
        path.write_bytes(original + suffix.encode("utf-8"))
        try:
            after = {stage_id: self._fingerprint(stage_id) for stage_id in changed + unchanged}
        finally:
            path.write_bytes(original)
        for stage_id in changed:
            self.assertNotEqual(before[stage_id], after[stage_id], f"{relative} 应改变 {stage_id} 指纹")
        for stage_id in unchanged:
            self.assertEqual(before[stage_id], after[stage_id], f"{relative} 不应改变 {stage_id} 指纹")

    def test_ui_asset_change_only_invalidates_render(self) -> None:
        self._assert_change(
            "tools/phase3/src/le_calc/report_ui/assets/app.js",
            changed=("render_report",),
            unchanged=("simulate", "validate_result"),
        )
        manifest = stage_cache.implementation_manifest(
            "render_report", self.root, handler=stage.STAGES["render_report"]["handler"]
        )
        paths = [row["path"] for row in manifest["files"]]
        self.assertTrue(any("report_ui" in path for path in paths))
        numeric = stage_cache.implementation_manifest(
            "simulate", self.root, handler=stage.STAGES["simulate"]["handler"]
        )
        self.assertFalse(any("report_ui" in row["path"] for row in numeric["files"]))

    def test_engine_change_invalidates_simulate(self) -> None:
        self._assert_change(
            "tools/phase3/src/le_calc/damage.py",
            changed=("simulate",),
            unchanged=(),
        )

    def test_validation_impl_change_invalidates_validate_only(self) -> None:
        self._assert_change(
            "tools/phase3/src/le_calc/workflow/expected_check.py",
            changed=("validate_result",),
            unchanged=("simulate",),
        )

    def test_renderer_change_invalidates_render_only(self) -> None:
        self._assert_change(
            "tools/phase3/src/le_calc/html.py",
            changed=("render_report",),
            unchanged=("simulate", "validate_result"),
        )


if __name__ == "__main__":
    unittest.main()
