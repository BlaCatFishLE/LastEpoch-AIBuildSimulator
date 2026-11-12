"""Phase-2 build importer for Last Epoch Tools builds.

Reads a public LE Tools build_data payload (live or archived) and resolves it against
the fixed phase-1 catalog into an offline BuildSpec v1.  Network access is confined to
``le_build.sources.letools``; every other module must work fully offline.
"""

__version__ = "0.1.0"

BUILD_SCHEMA_VERSION = "build-spec-v1"
PARSER_VERSION = "le-build-parser-v1"
MAPPING_VERSION = "letools-catalog-map-v1"
PROTOCOL_VERSION = "letools-build-data-v1"
