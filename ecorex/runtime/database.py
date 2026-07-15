"""SQLite setup shared by all durable v1 runtime repositories."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .commit_guard import transaction_commit_guard
from .errors import SchemaVersionError
from .schema_catalog import (
    compiled_product_schema_digest,
    product_schema_sql,
    validate_product_schema,
)
from .sqlite_connection import TransactionSafeConnection


SCHEMA_VERSION = 1


_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "runtime_meta": frozenset({"key", "value"}),
    "events": frozenset(
        {"event_id", "thread_id", "seq", "idempotency_key", "extension_snapshot_id"}
    ),
    "job_runtime_contexts": frozenset(
        {
            "job_id",
            "config_snapshot_id",
            "capability_snapshot_id",
            "permission_snapshot_id",
            "model_catalog_snapshot_id",
            "extension_snapshot_id",
        }
    ),
    "thread_heads": frozenset({"thread_id", "last_seq"}),
    "threads": frozenset(
        {
            "thread_id",
            "status",
            "metadata_json",
            "client_request_id",
            "request_fingerprint",
        }
    ),
    "turns": frozenset(
        {
            "turn_id",
            "thread_id",
            "agent_model_id",
            "image_model_id",
            "client_message_id",
        }
    ),
    "items": frozenset(
        {"item_id", "thread_id", "turn_id", "client_message_id", "content_json"}
    ),
    "jobs": frozenset(
        {"job_id", "status", "attempt", "lease_owner", "lease_token"}
    ),
    "interactions": frozenset(
        {
            "interaction_id",
            "status",
            "thread_id",
            "job_id",
            "contract_version",
            "contract_json",
            "response_client_request_id",
            "response_fingerprint",
        }
    ),
    "scheduler_threads": frozenset(
        {"scheduling_key", "last_leased_at", "last_job_id"}
    ),
}

_CORE_INDEXES = frozenset(
    {
        "idx_events_thread_seq",
        "idx_events_turn",
        "idx_interactions_expiry",
        "idx_interactions_pending",
        "idx_interactions_response_client_request",
        "idx_items_client_message",
        "idx_items_thread_turn",
        "idx_jobs_schedulable",
        "idx_jobs_thread",
        "idx_threads_client_request",
        "idx_turns_client_message",
        "idx_turns_thread_created",
    }
)
_CORE_TRIGGERS = frozenset(
    {
        "events_are_append_only_delete",
        "events_are_append_only_update",
        "events_reject_replace",
        "job_runtime_contexts_no_delete",
        "job_runtime_contexts_no_update",
        "jobs_reject_invalid_lease_insert",
        "jobs_reject_invalid_lease_update",
    }
)
_CORE_SCHEMA_OBJECTS = frozenset(_REQUIRED_COLUMNS) | _CORE_INDEXES | _CORE_TRIGGERS
# Canonical digest of the current core table/index/trigger definitions.  Domain
# repositories may add their own objects, but changing a core object requires a
# new storage schema and a signed declarative migration.
_CORE_SCHEMA_SHA256 = "6864caeeaee00b6a190aa175bd3c1f3006c0030dda502c1d4c9e30e34bd8f975"


def json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def json_loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


class SQLiteDatabase:
    """Owns schema initialization and short, process-safe transactions."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._initialized = False
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
            factory=TransactionSafeConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        # SQLite REPLACE implements delete+insert.  DELETE triggers only fire for
        # REPLACE when recursive triggers are enabled, so this is part of the
        # append-only event-store boundary rather than an optional tuning flag.
        connection.execute("PRAGMA recursive_triggers = ON")
        return connection

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            is not None
        )

    def _core_schema_digest(self, connection: sqlite3.Connection) -> str:
        placeholders = ",".join("?" for _ in _CORE_SCHEMA_OBJECTS)
        rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            f"WHERE name IN ({placeholders}) ORDER BY type, name",
            tuple(sorted(_CORE_SCHEMA_OBJECTS)),
        ).fetchall()
        observed = {str(row["name"]) for row in rows}
        missing = _CORE_SCHEMA_OBJECTS - observed
        if missing:
            raise SchemaVersionError(
                "storage schema core objects are missing: " + ", ".join(sorted(missing))
            )
        records: list[dict[str, str]] = []
        for row in rows:
            sql = row["sql"]
            if not isinstance(sql, str) or not sql.strip():
                raise SchemaVersionError("storage schema core object definition is invalid")
            records.append(
                {
                    "type": str(row["type"]),
                    "name": str(row["name"]),
                    "table": str(row["tbl_name"]),
                    "sql": " ".join(sql.split()),
                }
            )
        return hashlib.sha256(json_dumps(records).encode("utf-8")).hexdigest()

    def _validate_existing_schema(self, connection: sqlite3.Connection) -> bool:
        existing_tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if not existing_tables:
            return False
        if "runtime_meta" not in existing_tables:
            raise SchemaVersionError("storage schema version table is missing")
        row = connection.execute(
            "SELECT value FROM runtime_meta WHERE key = 'storage_schema_version'"
        ).fetchone()
        if row is None:
            raise SchemaVersionError("storage schema version is missing")
        try:
            existing = int(row["value"])
        except (TypeError, ValueError) as error:
            raise SchemaVersionError("storage schema version is invalid") from error
        if existing != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"storage schema version {existing} is incompatible with {SCHEMA_VERSION}"
            )
        for table, required in _REQUIRED_COLUMNS.items():
            if not self._table_exists(connection, table):
                raise SchemaVersionError(
                    f"storage schema version {existing} is missing table {table}"
                )
            actual = {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            missing = required - actual
            if missing:
                raise SchemaVersionError(
                    f"storage schema version {existing} is missing {table} columns: "
                    + ", ".join(sorted(missing))
                )
        if self._core_schema_digest(connection) != _CORE_SCHEMA_SHA256:
            raise SchemaVersionError(
                f"storage schema definition is incompatible with version {existing}"
            )
        return True

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            connection = self.connect()
            try:
                # A running Runtime is never a migration authority.  Validate an
                # existing database before any persistent PRAGMA or DDL; only the
                # signed InstallCoordinator migration path may evolve storage.
                existing_schema = self._validate_existing_schema(connection)
                connection.execute("PRAGMA journal_mode = WAL")
                if not existing_schema:
                    product_digest = compiled_product_schema_digest()
                    connection.executescript(
                        """
                    BEGIN IMMEDIATE;

                    CREATE TABLE IF NOT EXISTS runtime_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS thread_heads (
                        thread_id TEXT PRIMARY KEY,
                        last_seq INTEGER NOT NULL CHECK (last_seq >= 0)
                    );

                    CREATE TABLE IF NOT EXISTS events (
                        event_id TEXT PRIMARY KEY,
                        schema_version INTEGER NOT NULL,
                        thread_id TEXT NOT NULL,
                        seq INTEGER NOT NULL CHECK (seq > 0),
                        turn_id TEXT,
                        item_id TEXT,
                        job_id TEXT,
                        tool_call_id TEXT,
                        client_message_id TEXT,
                        causation_id TEXT,
                        correlation_id TEXT,
                        trace_id TEXT,
                        config_snapshot_id TEXT,
                        capability_snapshot_id TEXT,
                        permission_snapshot_id TEXT,
                        extension_snapshot_id TEXT,
                        event_type TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        idempotency_key TEXT,
                        UNIQUE (thread_id, seq),
                        UNIQUE (thread_id, idempotency_key)
                    );

                    CREATE INDEX IF NOT EXISTS idx_events_thread_seq
                        ON events(thread_id, seq);
                    CREATE INDEX IF NOT EXISTS idx_events_turn
                        ON events(turn_id, seq);

                    CREATE TRIGGER IF NOT EXISTS events_reject_replace
                    BEFORE INSERT ON events
                    WHEN EXISTS (
                        SELECT 1 FROM events AS existing
                        WHERE existing.event_id = NEW.event_id
                           OR (existing.thread_id = NEW.thread_id AND existing.seq = NEW.seq)
                           OR (
                               NEW.idempotency_key IS NOT NULL
                               AND existing.thread_id = NEW.thread_id
                               AND existing.idempotency_key = NEW.idempotency_key
                           )
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'events are append-only');
                    END;

                    CREATE TRIGGER IF NOT EXISTS events_are_append_only_update
                    BEFORE UPDATE ON events
                    BEGIN
                        SELECT RAISE(ABORT, 'events are append-only');
                    END;

                    CREATE TRIGGER IF NOT EXISTS events_are_append_only_delete
                    BEFORE DELETE ON events
                    BEGIN
                        SELECT RAISE(ABORT, 'events are append-only');
                    END;

                    CREATE TABLE IF NOT EXISTS threads (
                        thread_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        title TEXT,
                        metadata_json TEXT NOT NULL,
                        client_request_id TEXT,
                        request_fingerprint TEXT,
                        forked_from_thread_id TEXT,
                        forked_from_turn_id TEXT,
                        forked_from_seq INTEGER,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_threads_client_request
                        ON threads(client_request_id)
                        WHERE client_request_id IS NOT NULL;

                    CREATE TABLE IF NOT EXISTS turns (
                        turn_id TEXT PRIMARY KEY,
                        thread_id TEXT NOT NULL REFERENCES threads(thread_id),
                        status TEXT NOT NULL,
                        input_text TEXT NOT NULL,
                        agent_model_id TEXT NOT NULL,
                        image_model_id TEXT,
                        client_message_id TEXT,
                        metadata_json TEXT NOT NULL,
                        terminal_reason TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_turns_thread_created
                        ON turns(thread_id, created_at, turn_id);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_client_message
                        ON turns(thread_id, client_message_id)
                        WHERE client_message_id IS NOT NULL;

                    CREATE TABLE IF NOT EXISTS items (
                        item_id TEXT PRIMARY KEY,
                        thread_id TEXT NOT NULL REFERENCES threads(thread_id),
                        turn_id TEXT NOT NULL REFERENCES turns(turn_id),
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        content_json TEXT NOT NULL,
                        client_message_id TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_items_thread_turn
                        ON items(thread_id, turn_id, created_at, item_id);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_items_client_message
                        ON items(thread_id, client_message_id)
                        WHERE client_message_id IS NOT NULL;

                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        priority INTEGER NOT NULL,
                        attempt INTEGER NOT NULL CHECK (attempt >= 0),
                        max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
                        thread_id TEXT,
                        turn_id TEXT,
                        lease_owner TEXT,
                        lease_token TEXT,
                        lease_expires_at TEXT,
                        heartbeat_at TEXT,
                        available_at TEXT NOT NULL,
                        deadline TEXT,
                        checkpoint_json TEXT,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        request_fingerprint TEXT NOT NULL,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_jobs_schedulable
                        ON jobs(status, available_at, priority, created_at);
                    CREATE INDEX IF NOT EXISTS idx_jobs_thread
                        ON jobs(thread_id, created_at, job_id);

                    CREATE TRIGGER IF NOT EXISTS jobs_reject_invalid_lease_insert
                    BEFORE INSERT ON jobs
                    WHEN (
                        NEW.status NOT IN (
                            'queued', 'leased', 'running', 'waiting_human',
                            'retry_scheduled', 'completed', 'failed',
                            'cancelled', 'dead_letter'
                        )
                        OR NEW.attempt > NEW.max_attempts
                        OR (
                            NEW.status IN ('leased', 'running')
                            AND (
                                NEW.lease_owner IS NULL OR NEW.lease_owner = ''
                                OR NEW.lease_token IS NULL OR NEW.lease_token = ''
                                OR NEW.lease_expires_at IS NULL
                                OR NEW.heartbeat_at IS NULL
                            )
                        )
                        OR (
                            NEW.status NOT IN ('leased', 'running')
                            AND (
                                NEW.lease_owner IS NOT NULL
                                OR NEW.lease_token IS NOT NULL
                                OR NEW.lease_expires_at IS NOT NULL
                                OR NEW.heartbeat_at IS NOT NULL
                            )
                        )
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'invalid durable job lease state');
                    END;

                    CREATE TRIGGER IF NOT EXISTS jobs_reject_invalid_lease_update
                    BEFORE UPDATE OF status, attempt, max_attempts, lease_owner,
                        lease_token, lease_expires_at, heartbeat_at
                    ON jobs
                    WHEN (
                        NEW.status NOT IN (
                            'queued', 'leased', 'running', 'waiting_human',
                            'retry_scheduled', 'completed', 'failed',
                            'cancelled', 'dead_letter'
                        )
                        OR NEW.attempt > NEW.max_attempts
                        OR (
                            NEW.status IN ('leased', 'running')
                            AND (
                                NEW.lease_owner IS NULL OR NEW.lease_owner = ''
                                OR NEW.lease_token IS NULL OR NEW.lease_token = ''
                                OR NEW.lease_expires_at IS NULL
                                OR NEW.heartbeat_at IS NULL
                            )
                        )
                        OR (
                            NEW.status NOT IN ('leased', 'running')
                            AND (
                                NEW.lease_owner IS NOT NULL
                                OR NEW.lease_token IS NOT NULL
                                OR NEW.lease_expires_at IS NOT NULL
                                OR NEW.heartbeat_at IS NOT NULL
                            )
                        )
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'invalid durable job lease state');
                    END;

                    CREATE TABLE IF NOT EXISTS job_runtime_contexts (
                        job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
                        config_snapshot_id TEXT NOT NULL,
                        capability_snapshot_id TEXT NOT NULL,
                        permission_snapshot_id TEXT NOT NULL,
                        model_catalog_snapshot_id TEXT NOT NULL,
                        extension_snapshot_id TEXT NOT NULL
                    );

                    CREATE TRIGGER IF NOT EXISTS job_runtime_contexts_no_update
                    BEFORE UPDATE ON job_runtime_contexts
                    BEGIN
                        SELECT RAISE(ABORT, 'job runtime contexts are immutable');
                    END;

                    CREATE TRIGGER IF NOT EXISTS job_runtime_contexts_no_delete
                    BEFORE DELETE ON job_runtime_contexts
                    BEGIN
                        SELECT RAISE(ABORT, 'job runtime contexts are immutable');
                    END;

                    CREATE TABLE IF NOT EXISTS scheduler_threads (
                        scheduling_key TEXT PRIMARY KEY,
                        last_leased_at TEXT NOT NULL,
                        last_job_id TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS interactions (
                        interaction_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        prompt TEXT NOT NULL,
                        contract_version INTEGER NOT NULL CHECK (contract_version = 1),
                        contract_json TEXT NOT NULL,
                        options_json TEXT NOT NULL,
                        response_json TEXT,
                        response_client_request_id TEXT,
                        response_fingerprint TEXT,
                        thread_id TEXT NOT NULL,
                        turn_id TEXT,
                        job_id TEXT,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        request_fingerprint TEXT NOT NULL,
                        expires_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_interactions_pending
                        ON interactions(status, thread_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_interactions_expiry
                        ON interactions(status, expires_at)
                        WHERE expires_at IS NOT NULL;
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_interactions_response_client_request
                        ON interactions(response_client_request_id)
                        WHERE response_client_request_id IS NOT NULL;

                    INSERT INTO runtime_meta(key, value)
                    VALUES ('storage_schema_version', '1')
                    ON CONFLICT(key) DO NOTHING;
                        """
                        + product_schema_sql()
                        + f"""
                    INSERT INTO runtime_meta(key, value)
                    VALUES ('product_schema_sha256', '{product_digest}');
                    COMMIT;
                        """
                    )
                    if not self._validate_existing_schema(connection):
                        raise SchemaVersionError("fresh storage schema was not created")
                    observed_product_digest = validate_product_schema(connection)
                else:
                    observed_product_digest = validate_product_schema(connection)
                row = connection.execute(
                    "SELECT value FROM runtime_meta WHERE key = 'product_schema_sha256'"
                ).fetchone()
                if row is None or row["value"] != observed_product_digest:
                    raise SchemaVersionError(
                        "product schema catalog fingerprint metadata is incompatible"
                    )
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()
            self._initialized = True

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            # Autocommit mode otherwise gives every SELECT a different snapshot.
            # Projection rows and their watermark must be observed atomically.
            connection.execute("BEGIN")
            yield connection
            if connection.in_transaction:
                connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
