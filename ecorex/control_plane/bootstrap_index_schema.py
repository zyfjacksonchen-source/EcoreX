"""Independent schema authority for the co-located public Bootstrap pointer.

The tables live in the Control Plane SQLite WAL database, but use their own
``bootstrap_`` namespace and migration history.  This keeps release/rollout
schema fingerprints stable while still making pointer stage, activation,
readback proof, audit and outbox one durable local authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


BOOTSTRAP_INDEX_SCHEMA_VERSION = 1

BOOTSTRAP_INDEX_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bootstrap_schema_migrations (
    version INTEGER PRIMARY KEY CHECK(version = 1),
    migration_name TEXT NOT NULL UNIQUE,
    migration_checksum TEXT NOT NULL,
    installed_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS bootstrap_schema_migrations_no_update
BEFORE UPDATE ON bootstrap_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'bootstrap schema history is immutable');
END;
CREATE TRIGGER IF NOT EXISTS bootstrap_schema_migrations_no_delete
BEFORE DELETE ON bootstrap_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'bootstrap schema history is immutable');
END;

CREATE TABLE IF NOT EXISTS bootstrap_index_stages (
    record_id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL,
    version TEXT NOT NULL,
    build_digest TEXT NOT NULL,
    authority_sequence INTEGER NOT NULL CHECK(authority_sequence > 0),
    authority_revision TEXT NOT NULL,
    authority_issued_at TEXT NOT NULL,
    authority_expires_at TEXT NOT NULL,
    authority_target_json TEXT NOT NULL,
    authority_json TEXT NOT NULL,
    index_sha256 TEXT NOT NULL,
    index_size_bytes INTEGER NOT NULL CHECK(index_size_bytes BETWEEN 1 AND 262144),
    index_bytes BLOB NOT NULL,
    public_url TEXT NOT NULL,
    previous_activation_record_id TEXT,
    previous_sequence INTEGER,
    previous_revision TEXT,
    previous_index_sha256 TEXT,
    previous_target_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(index_sha256, public_url),
    CHECK(length(index_bytes) = index_size_bytes),
    CHECK(
        (previous_activation_record_id IS NULL AND previous_sequence IS NULL
            AND previous_revision IS NULL AND previous_index_sha256 IS NULL
            AND previous_target_json IS NULL)
        OR
        (previous_activation_record_id IS NOT NULL AND previous_sequence IS NOT NULL
            AND previous_revision IS NOT NULL AND previous_index_sha256 IS NOT NULL
            AND previous_target_json IS NOT NULL)
    )
);
CREATE TRIGGER IF NOT EXISTS bootstrap_index_stages_no_update
BEFORE UPDATE ON bootstrap_index_stages BEGIN
    SELECT RAISE(ABORT, 'bootstrap index stages are append-only');
END;
CREATE TRIGGER IF NOT EXISTS bootstrap_index_stages_no_delete
BEFORE DELETE ON bootstrap_index_stages BEGIN
    SELECT RAISE(ABORT, 'bootstrap index stages are append-only');
END;

CREATE TABLE IF NOT EXISTS bootstrap_index_publication_intents (
    record_id TEXT PRIMARY KEY,
    stage_record_id TEXT NOT NULL UNIQUE
        REFERENCES bootstrap_index_stages(record_id),
    previous_activation_record_id TEXT,
    previous_sequence INTEGER,
    previous_revision TEXT,
    previous_index_sha256 TEXT,
    previous_target_json TEXT,
    candidate_index_sha256 TEXT NOT NULL,
    candidate_size_bytes INTEGER NOT NULL CHECK(candidate_size_bytes BETWEEN 1 AND 262144),
    public_url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(
        (previous_activation_record_id IS NULL AND previous_sequence IS NULL
            AND previous_revision IS NULL AND previous_index_sha256 IS NULL
            AND previous_target_json IS NULL)
        OR
        (previous_activation_record_id IS NOT NULL AND previous_sequence IS NOT NULL
            AND previous_revision IS NOT NULL AND previous_index_sha256 IS NOT NULL
            AND previous_target_json IS NOT NULL)
    )
);
CREATE TRIGGER IF NOT EXISTS bootstrap_index_publication_intents_no_update
BEFORE UPDATE ON bootstrap_index_publication_intents BEGIN
    SELECT RAISE(ABORT, 'bootstrap index publication intents are append-only');
END;
CREATE TRIGGER IF NOT EXISTS bootstrap_index_publication_intents_no_delete
BEFORE DELETE ON bootstrap_index_publication_intents BEGIN
    SELECT RAISE(ABORT, 'bootstrap index publication intents are append-only');
END;

CREATE TABLE IF NOT EXISTS bootstrap_index_publication_lease (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    intent_record_id TEXT NOT NULL UNIQUE
        REFERENCES bootstrap_index_publication_intents(record_id),
    acquired_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS bootstrap_index_publication_lease_no_update
BEFORE UPDATE ON bootstrap_index_publication_lease BEGIN
    SELECT RAISE(ABORT, 'bootstrap index publication lease cannot be reassigned');
END;

CREATE TABLE IF NOT EXISTS bootstrap_index_activations (
    record_id TEXT PRIMARY KEY,
    publication_intent_record_id TEXT NOT NULL UNIQUE
        REFERENCES bootstrap_index_publication_intents(record_id),
    stage_record_id TEXT NOT NULL UNIQUE
        REFERENCES bootstrap_index_stages(record_id),
    previous_activation_record_id TEXT
        REFERENCES bootstrap_index_activations(record_id),
    authority_sequence INTEGER NOT NULL CHECK(authority_sequence > 0),
    authority_revision TEXT NOT NULL,
    authority_issued_at TEXT NOT NULL,
    authority_expires_at TEXT NOT NULL,
    authority_target_json TEXT NOT NULL,
    index_sha256 TEXT NOT NULL,
    index_size_bytes INTEGER NOT NULL CHECK(index_size_bytes BETWEEN 1 AND 262144),
    public_url TEXT NOT NULL,
    public_object_revision_id TEXT NOT NULL,
    activated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS bootstrap_index_activations_no_update
BEFORE UPDATE ON bootstrap_index_activations BEGIN
    SELECT RAISE(ABORT, 'bootstrap index activations are append-only');
END;
CREATE TRIGGER IF NOT EXISTS bootstrap_index_activations_no_delete
BEFORE DELETE ON bootstrap_index_activations BEGIN
    SELECT RAISE(ABORT, 'bootstrap index activations are append-only');
END;

CREATE TABLE IF NOT EXISTS bootstrap_index_active_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    activation_record_id TEXT NOT NULL UNIQUE
        REFERENCES bootstrap_index_activations(record_id),
    authority_sequence INTEGER NOT NULL CHECK(authority_sequence > 0),
    authority_revision TEXT NOT NULL,
    authority_issued_at TEXT NOT NULL,
    authority_expires_at TEXT NOT NULL,
    authority_target_json TEXT NOT NULL,
    index_sha256 TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bootstrap_index_readbacks (
    record_id TEXT PRIMARY KEY,
    activation_record_id TEXT NOT NULL UNIQUE
        REFERENCES bootstrap_index_activations(record_id),
    index_sha256 TEXT NOT NULL,
    index_size_bytes INTEGER NOT NULL CHECK(index_size_bytes BETWEEN 1 AND 262144),
    public_url TEXT NOT NULL,
    read_back_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS bootstrap_index_readbacks_no_update
BEFORE UPDATE ON bootstrap_index_readbacks BEGIN
    SELECT RAISE(ABORT, 'bootstrap index readbacks are append-only');
END;

CREATE TABLE IF NOT EXISTS bootstrap_freshness_refresh_attempts (
    record_id TEXT PRIMARY KEY,
    source_activation_record_id TEXT NOT NULL
        REFERENCES bootstrap_index_activations(record_id),
    source_index_sha256 TEXT NOT NULL,
    authority_sequence INTEGER NOT NULL CHECK(authority_sequence > 0),
    authority_revision TEXT NOT NULL,
    authority_target_json TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    forced INTEGER NOT NULL CHECK(forced IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS bootstrap_freshness_refresh_attempts_no_update
BEFORE UPDATE ON bootstrap_freshness_refresh_attempts BEGIN
    SELECT RAISE(ABORT, 'Bootstrap freshness refresh attempts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS bootstrap_freshness_refresh_attempts_no_delete
BEFORE DELETE ON bootstrap_freshness_refresh_attempts BEGIN
    SELECT RAISE(ABORT, 'Bootstrap freshness refresh attempts are append-only');
END;

CREATE TABLE IF NOT EXISTS bootstrap_freshness_refresh_preparations (
    attempt_record_id TEXT PRIMARY KEY
        REFERENCES bootstrap_freshness_refresh_attempts(record_id),
    index_sha256 TEXT NOT NULL,
    index_size_bytes INTEGER NOT NULL CHECK(index_size_bytes BETWEEN 1 AND 262144),
    index_bytes BLOB NOT NULL,
    signer_key_id TEXT NOT NULL,
    prepared_at TEXT NOT NULL,
    CHECK(length(index_bytes) = index_size_bytes)
);
CREATE TRIGGER IF NOT EXISTS bootstrap_freshness_refresh_preparations_no_update
BEFORE UPDATE ON bootstrap_freshness_refresh_preparations BEGIN
    SELECT RAISE(ABORT, 'Bootstrap freshness refresh preparations are append-only');
END;
CREATE TRIGGER IF NOT EXISTS bootstrap_freshness_refresh_preparations_no_delete
BEFORE DELETE ON bootstrap_freshness_refresh_preparations BEGIN
    SELECT RAISE(ABORT, 'Bootstrap freshness refresh preparations are append-only');
END;

CREATE TABLE IF NOT EXISTS bootstrap_freshness_refresh_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    attempt_record_id TEXT
        REFERENCES bootstrap_freshness_refresh_attempts(record_id),
    status TEXT NOT NULL CHECK(status IN (
        'started','prepared','succeeded','failed','not-due','no-active','unconfigured'
    )),
    error_code TEXT,
    activation_record_id TEXT,
    proof_record_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS bootstrap_freshness_refresh_events_no_update
BEFORE UPDATE ON bootstrap_freshness_refresh_events BEGIN
    SELECT RAISE(ABORT, 'Bootstrap freshness refresh events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS bootstrap_freshness_refresh_events_no_delete
BEFORE DELETE ON bootstrap_freshness_refresh_events BEGIN
    SELECT RAISE(ABORT, 'Bootstrap freshness refresh events are append-only');
END;

CREATE TABLE IF NOT EXISTS bootstrap_freshness_refresh_lease (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    attempt_record_id TEXT NOT NULL
        REFERENCES bootstrap_freshness_refresh_attempts(record_id),
    owner_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bootstrap_freshness_refresh_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    status TEXT NOT NULL CHECK(status IN (
        'idle','healthy','refreshing','degraded','unconfigured'
    )),
    active_expires_at TEXT,
    last_checked_at TEXT,
    next_check_at TEXT,
    last_attempt_record_id TEXT,
    last_success_at TEXT,
    last_failure_at TEXT,
    last_error_code TEXT,
    updated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS bootstrap_index_readbacks_no_delete
BEFORE DELETE ON bootstrap_index_readbacks BEGIN
    SELECT RAISE(ABORT, 'bootstrap index readbacks are append-only');
END;

CREATE TABLE IF NOT EXISTS bootstrap_index_outbox (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'bootstrap-index.staged','bootstrap-index.activated',
        'bootstrap-index.publication-requested','bootstrap-index.read-back',
        'bootstrap-freshness.refresh-started',
        'bootstrap-freshness.refresh-succeeded',
        'bootstrap-freshness.refresh-failed'
    )),
    record_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS bootstrap_index_outbox_no_update
BEFORE UPDATE ON bootstrap_index_outbox BEGIN
    SELECT RAISE(ABORT, 'bootstrap index outbox is append-only');
END;
CREATE TRIGGER IF NOT EXISTS bootstrap_index_outbox_no_delete
BEFORE DELETE ON bootstrap_index_outbox BEGIN
    SELECT RAISE(ABORT, 'bootstrap index outbox is append-only');
END;
CREATE INDEX IF NOT EXISTS bootstrap_index_stage_release
    ON bootstrap_index_stages(release_id, authority_sequence, created_at);
CREATE INDEX IF NOT EXISTS bootstrap_index_intent_created
    ON bootstrap_index_publication_intents(created_at, record_id);
CREATE INDEX IF NOT EXISTS bootstrap_index_outbox_sequence
    ON bootstrap_index_outbox(sequence, created_at);
CREATE INDEX IF NOT EXISTS bootstrap_freshness_refresh_attempt_source
    ON bootstrap_freshness_refresh_attempts(source_activation_record_id, created_at);
CREATE INDEX IF NOT EXISTS bootstrap_freshness_refresh_event_attempt
    ON bootstrap_freshness_refresh_events(attempt_record_id, sequence);
"""

_MIGRATION_NAME = "durable-monotonic-public-bootstrap-index"
_MIGRATION_CHECKSUM = hashlib.sha256(
    b"ecorex-bootstrap-index-schema-v1\0" + BOOTSTRAP_INDEX_SCHEMA_SQL.encode("utf-8")
).hexdigest()


class BootstrapIndexSchemaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BootstrapIndexSchemaReceipt:
    version: int
    migration_name: str
    migration_checksum: str
    installed_at: str


class BootstrapIndexSchemaManager:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def migrate(self) -> BootstrapIndexSchemaReceipt:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            connection.execute("BEGIN EXCLUSIVE")
            _execute_sql(connection, BOOTSTRAP_INDEX_SCHEMA_SQL)
            row = connection.execute(
                "SELECT * FROM bootstrap_schema_migrations WHERE version=1"
            ).fetchone()
            if row is None:
                installed_at = datetime.now(UTC).isoformat()
                connection.execute(
                    "INSERT INTO bootstrap_schema_migrations VALUES(1,?,?,?)",
                    (_MIGRATION_NAME, _MIGRATION_CHECKSUM, installed_at),
                )
            receipt = self._validate_connection(connection)
            connection.commit()
            return receipt
        except Exception as error:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(error, BootstrapIndexSchemaError):
                raise
            raise BootstrapIndexSchemaError(
                "Bootstrap index schema migration failed"
            ) from None
        finally:
            connection.close()

    def validate(self) -> BootstrapIndexSchemaReceipt:
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro",
            uri=True,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA query_only=ON")
        try:
            connection.execute("BEGIN")
            receipt = self._validate_connection(connection)
            connection.commit()
            return receipt
        except Exception as error:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(error, BootstrapIndexSchemaError):
                raise
            raise BootstrapIndexSchemaError(
                "Bootstrap index schema validation failed"
            ) from None
        finally:
            connection.close()

    @staticmethod
    def _validate_connection(
        connection: sqlite3.Connection,
    ) -> BootstrapIndexSchemaReceipt:
        expected = _compiled_records()
        actual = _records(connection)
        if actual != expected:
            raise BootstrapIndexSchemaError(
                "Bootstrap index schema object fingerprint is incompatible"
            )
        row = connection.execute(
            "SELECT * FROM bootstrap_schema_migrations WHERE version=1"
        ).fetchone()
        if (
            row is None
            or row["migration_name"] != _MIGRATION_NAME
            or row["migration_checksum"] != _MIGRATION_CHECKSUM
        ):
            raise BootstrapIndexSchemaError(
                "Bootstrap index schema history is invalid"
            )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise BootstrapIndexSchemaError(
                "Bootstrap index schema foreign keys are invalid"
            )
        return BootstrapIndexSchemaReceipt(
            version=1,
            migration_name=str(row["migration_name"]),
            migration_checksum=str(row["migration_checksum"]),
            installed_at=str(row["installed_at"]),
        )


def _records(connection: sqlite3.Connection) -> tuple[dict[str, str], ...]:
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE name LIKE 'bootstrap_%' AND sql IS NOT NULL ORDER BY type,name"
    ).fetchall()
    return tuple(
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": " ".join(str(row[3]).split()),
        }
        for row in rows
    )


def _compiled_records() -> tuple[dict[str, str], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(BOOTSTRAP_INDEX_SCHEMA_SQL)
        connection.execute(
            "INSERT INTO bootstrap_schema_migrations VALUES(1,?,?,?)",
            (_MIGRATION_NAME, _MIGRATION_CHECKSUM, "1970-01-01T00:00:00+00:00"),
        )
        return _records(connection)
    finally:
        connection.close()


def _execute_sql(connection: sqlite3.Connection, sql: str) -> None:
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            pending = ""
            if statement:
                connection.execute(statement)
    if pending.strip():
        raise BootstrapIndexSchemaError("Bootstrap index schema SQL is incomplete")


__all__ = [
    "BOOTSTRAP_INDEX_SCHEMA_VERSION",
    "BootstrapIndexSchemaError",
    "BootstrapIndexSchemaManager",
    "BootstrapIndexSchemaReceipt",
]
