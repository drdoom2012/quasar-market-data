"""Schema identity shared by the Collector (which migrates) and the API (which only verifies).

This module must not import ``storage.migrate``: the import-linter contract ``only the collector
migrates`` forbids the API from reaching the runner, directly or through other modules.
"""

from typing import Final

APPLICATION_ID: Final = 0x514D4431  # "QMD1": refuse to touch a SQLite file that is not ours
SCHEMA_VERSION: Final = 1  # the highest migration shipped in ``storage.migrations``
