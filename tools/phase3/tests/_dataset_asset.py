"""Guard for tests that depend on the externally distributed dataset file.

``data/releases/phase1-dataset-003/data.sqlite`` (251 MB) is deliberately not
versioned: it exceeds GitHub's 100 MB per-file Git limit and is published as a
GitHub Release asset instead. Call :func:`require_dataset_db` at the top of a
test class' ``setUpClass`` so a fresh clone reports a clear skip with recovery
instructions instead of erroring out.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DATASET_DB = REPO / "data" / "releases" / "phase1-dataset-003" / "data.sqlite"
DATASET_DOWNLOAD_URL = (
    "https://github.com/BlaCatFishLE/LastEpoch-AIBuildSimulator"
    "/releases/download/v0.3.0/data.sqlite"
)


def require_dataset_db() -> None:
    """Skip the calling test class when the published dataset file is absent."""
    if not DATASET_DB.is_file():
        raise unittest.SkipTest(
            "历史发布数据集 phase1-dataset-003 未随仓库分发（外部不可变资产）：缺少 "
            f"{DATASET_DB.relative_to(REPO)}；从 {DATASET_DOWNLOAD_URL} 下载后"
            "放回原位即可（详见 README「恢复大文件」）"
        )
