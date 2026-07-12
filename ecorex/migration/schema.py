"""Canonical v1 tables owned by the copy-on-write migration boundary."""

from __future__ import annotations

from pathlib import Path

from ecorex.runtime import SQLiteDatabase
from ecorex.runtime.schema_fragments.legacy_import import LEGACY_IMPORT_SCHEMA_SQL


# Version of the one-time v0.3 import layout, not the Runtime storage schema.
# Layout 4 adds the immutable ordinal-zero Turn input fact for every directly
# imported historical Turn and verifies that no synthetic execution snapshot
# is attached to that terminal history.
IMPORT_LAYOUT_VERSION = 4


def initialize_target_database(path: str | Path) -> SQLiteDatabase:
    """Initialize the signed Runtime schema and its canonical import tables."""

    database = SQLiteDatabase(path)
    with database.transaction() as connection:
        # The tables are already part of the compiled Runtime schema.  Keeping
        # the idempotent statement here makes this boundary self-checking and
        # prevents a standalone importer from drifting from product authority.
        connection.executescript(LEGACY_IMPORT_SCHEMA_SQL)
        connection.execute(
            "INSERT OR REPLACE INTO migration_meta(key, value) VALUES (?, ?)",
            ("import_layout_version", str(IMPORT_LAYOUT_VERSION)),
        )
    return database
