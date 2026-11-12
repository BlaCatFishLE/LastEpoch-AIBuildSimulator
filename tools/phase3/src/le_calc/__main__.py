"""Allow ``python -m le_calc ...`` (argument parser lives in cli.py)."""
from .cli import main

raise SystemExit(main())
