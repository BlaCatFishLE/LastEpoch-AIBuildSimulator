"""Skip guard for build-import tests that need author-local frozen assets.

The public release ships no case, save, build revision or archived discovery
payload.  Several acceptance / import tests operate on the author's delivered
``data/builds/`` tree or archived planner captures; on a clean public clone call
:func:`require_author_assets` from ``setUpModule()`` so they skip cleanly.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def require_author_assets() -> None:
    """Skip the calling module when the author-local build assets are absent."""
    missing: list[str] = []
    builds = REPO / "data" / "builds"
    if not builds.is_dir() or not any(builds.iterdir()):
        missing.append("data/builds/")
    discovery = REPO / "reports" / "discovery"
    if not discovery.is_dir() or not any(discovery.iterdir()):
        missing.append("reports/discovery/")
    if missing:
        raise unittest.SkipTest(
            "作者本地冻结验收资产不随公开版分发（缺少 " + "、".join(missing) + "）："
            "本模块只适用于作者完整工作树；公开版以合成 fixture 测试为准"
        )
