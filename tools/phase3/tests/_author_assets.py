"""Skip guard for tests that need the author's frozen case/report/build assets.

The public release intentionally ships **no** real case, save, build revision or
historical report: ``examples/`` only contains ``_template/`` and ``reports/`` /
``data/builds/`` are local-only working directories.  Several acceptance test
modules still assert numbers recorded on the author's full working tree; on a
clean public clone they cannot run.

Call :func:`require_author_assets` from a module's ``setUpModule()`` so the
whole module is reported as a clear skip instead of erroring on a missing file.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

#: Markers that only exist in the author's complete working tree (never in the
#: public package): real case directories and the published build revisions.
AUTHOR_CASE_DIRS = ("black-hole", "flame-reave", "void-cleave-20261110")
AUTHOR_BUILD_MARKERS = ("data/builds",)


def author_assets_missing() -> list[str]:
    missing: list[str] = []
    for name in AUTHOR_CASE_DIRS:
        if not (REPO / "examples" / name).is_dir():
            missing.append(f"examples/{name}/")
            break
    for relative in AUTHOR_BUILD_MARKERS:
        path = REPO / relative
        if not path.is_dir() or not any(path.iterdir()):
            missing.append(relative + "/")
    return missing


def require_author_assets() -> None:
    """Skip the calling module when the author-local frozen assets are absent."""
    missing = author_assets_missing()
    if missing:
        raise unittest.SkipTest(
            "作者本地冻结验收资产不随公开版分发（缺少 " + "、".join(missing) + "）："
            "本模块只适用于作者完整工作树；公开版以合成 fixture 测试为准"
        )
