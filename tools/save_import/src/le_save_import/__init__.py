# -*- coding: utf-8 -*-
"""Last Epoch offline-save (``EPOCH`` + JSON) build (BD) extraction.

Public API::

    from le_save_import import extract_build, compare_to_planner
    from le_save_import.items import decode_item, decode_save_items, load_save
    from le_save_import.names import Resolver

Command line::

    python -m le_save_import inspect  --save <1CHARACTERSLOT_BETA_n>
    python -m le_save_import build    --save <path> --out reports/<case>/save-import
    python -m le_save_import build    --save <path> --planner <build-response.json>
"""

from .pipeline import (  # noqa: F401
    SCHEMA,
    compare_to_planner,
    describe_item,
    extract_build,
    render_tables_markdown,
    run_pipeline,
)

__version__ = "1.0.0"
