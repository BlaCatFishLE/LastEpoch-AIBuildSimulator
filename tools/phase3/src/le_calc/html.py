"""Offline single-file HTML report renderer (public entry point).

The implementation lives in :mod:`le_calc.report_ui`; this module keeps the
stable ``render`` / ``write_html`` / ``main`` API used by ``le_calc.report``
and the CLI::

    python -m le_calc.html --result <result.json> --out <report.html>
"""

from __future__ import annotations

import sys

from .report_ui import render, write_html
from .report_ui.shell import main

__all__ = ["render", "write_html", "main"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
