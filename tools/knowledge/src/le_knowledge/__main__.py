# -*- coding: utf-8 -*-
"""``python -m le_knowledge`` — 绕过 le.ps1 编排直接调用知识查询接口。

用法（仓库根，``PYTHONPATH=tools/knowledge/src``）::

    python -m le_knowledge search --query "Flame Reave"
    python -m le_knowledge show --id fr-rhythm-of-fire
    python -m le_knowledge rebuild
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
