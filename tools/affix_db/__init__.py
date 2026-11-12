"""affix_db -- local normalized Last Epoch affix roll-range database builder.

The package turns tunklab's static client-side data chunk into a normalized
SQLite + JSON database keyed by affix, item base type and tier.  All parsing is
static: downloaded JavaScript is never executed.

Runtime (build/query/validate) uses only the Python standard library and never
touches the network.  Network fetching lives in :mod:`affix_db.fetch`.
"""

__version__ = "1.0.0"
SCHEMA_VERSION = 1
