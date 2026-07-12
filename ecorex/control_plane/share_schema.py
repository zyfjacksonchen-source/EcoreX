"""Explicit, fingerprinted SQLite authority for public Cloud Share state."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Any, Mapping, Sequence


CURRENT_CLOUD_SHARE_SCHEMA_VERSION = 1
CLOUD_SHARE_SCHEMA_RECEIPT_VERSION = 1
_HEX_DIGEST = frozenset("0123456789abcdef")
_WAL_ACTIVATION_TIMEOUT_SECONDS = 5.0
_WAL_ACTIVATION_INITIAL_RETRY_SECONDS = 0.005
_WAL_ACTIVATION_MAX_RETRY_SECONDS = 0.05


class CloudShareSchemaError(RuntimeError):
    """Cloud Share schema is absent, unknown, drifted, corrupt, or too new."""


CLOUD_SHARE_SCHEMA_SQL = """
CREATE TABLE cloud_share_schema_migrations (
    version INTEGER PRIMARY KEY CHECK(version > 0),
    migration_name TEXT NOT NULL UNIQUE,
    migration_checksum TEXT NOT NULL,
    source_schema_sha256 TEXT NOT NULL,
    target_schema_sha256 TEXT NOT NULL,
    transformed_rows INTEGER NOT NULL CHECK(transformed_rows >= 0),
    receipt_json TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    installed_at TEXT NOT NULL
);
CREATE TRIGGER cloud_share_schema_migrations_no_update
BEFORE UPDATE ON cloud_share_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'cloud share schema history is immutable');
END;
CREATE TRIGGER cloud_share_schema_migrations_no_delete
BEFORE DELETE ON cloud_share_schema_migrations BEGIN
    SELECT RAISE(ABORT, 'cloud share schema history is immutable');
END;
CREATE TABLE cloud_share_media_migrations (
    version INTEGER PRIMARY KEY,
    migration_checksum TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    installed_at TEXT NOT NULL
);
CREATE TRIGGER cloud_share_media_migrations_no_update
BEFORE UPDATE ON cloud_share_media_migrations BEGIN
    SELECT RAISE(ABORT, 'cloud share media migration history is immutable');
END;
CREATE TRIGGER cloud_share_media_migrations_no_delete
BEFORE DELETE ON cloud_share_media_migrations BEGIN
    SELECT RAISE(ABORT, 'cloud share media migration history is immutable');
END;
CREATE TABLE cloud_share_snapshots (
    remote_snapshot_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    source_share_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    source_watermark INTEGER NOT NULL CHECK(source_watermark >= 0),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    token_sha256 TEXT NOT NULL UNIQUE,
    token_key_id TEXT NOT NULL,
    state_mac_version INTEGER NOT NULL CHECK(state_mac_version IN (1,2)),
    state_mac TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','revoked')),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    UNIQUE(account_id, source_share_id)
);
CREATE TRIGGER cloud_share_identity_immutable
BEFORE UPDATE OF remote_snapshot_id, account_id, source_share_id,
    thread_id, source_watermark, payload_json, payload_sha256,
    token_sha256, token_key_id, state_mac_version, expires_at, created_at
ON cloud_share_snapshots BEGIN
    SELECT RAISE(ABORT, 'cloud share identity is immutable');
END;
CREATE TRIGGER cloud_share_status_transition
BEFORE UPDATE OF status, revoked_at ON cloud_share_snapshots
WHEN NOT (
    OLD.status='active' AND NEW.status='revoked'
    AND OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL
) BEGIN
    SELECT RAISE(ABORT, 'cloud share status transition is invalid');
END;
CREATE TRIGGER cloud_share_snapshots_no_delete
BEFORE DELETE ON cloud_share_snapshots BEGIN
    SELECT RAISE(ABORT, 'cloud share snapshots are append-only');
END;
CREATE TRIGGER cloud_share_key_identity_required
BEFORE INSERT ON cloud_share_snapshots
WHEN NEW.token_key_id IS NULL OR NEW.state_mac_version NOT IN (1,2) BEGIN
    SELECT RAISE(ABORT, 'cloud share key identity is required');
END;
CREATE TABLE cloud_share_objects (
    object_key TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 1 AND size_bytes <= 16777216),
    mime_type TEXT NOT NULL,
    etag TEXT NOT NULL,
    ref_count INTEGER NOT NULL CHECK(ref_count >= 0),
    state TEXT NOT NULL CHECK(state IN ('active','deleting')),
    created_at TEXT NOT NULL,
    last_accessed_at TEXT,
    access_count INTEGER NOT NULL DEFAULT 0 CHECK(access_count >= 0)
);
CREATE TRIGGER cloud_share_objects_identity_immutable
BEFORE UPDATE OF object_key, sha256, size_bytes, mime_type, etag, created_at
ON cloud_share_objects BEGIN
    SELECT RAISE(ABORT, 'cloud share object identity is immutable');
END;
CREATE TRIGGER cloud_share_objects_delete_guard
BEFORE DELETE ON cloud_share_objects
WHEN OLD.state != 'deleting' OR OLD.ref_count != 0 BEGIN
    SELECT RAISE(ABORT, 'referenced cloud share object cannot be deleted');
END;
CREATE TABLE cloud_share_media (
    account_id TEXT NOT NULL,
    source_share_id TEXT NOT NULL,
    media_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('preview','thumbnail')),
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0 AND size_bytes <= 16777216),
    sha256 TEXT NOT NULL,
    object_key TEXT NOT NULL REFERENCES cloud_share_objects(object_key),
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id, source_share_id, media_id),
    UNIQUE(account_id, idempotency_key)
);
CREATE TRIGGER cloud_share_media_no_update
BEFORE UPDATE ON cloud_share_media BEGIN
    SELECT RAISE(ABORT, 'cloud share media is immutable');
END;
CREATE TABLE cloud_share_media_links (
    account_id TEXT NOT NULL,
    source_share_id TEXT NOT NULL,
    media_id TEXT NOT NULL,
    remote_snapshot_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT,
    PRIMARY KEY(account_id, source_share_id, media_id),
    UNIQUE(remote_snapshot_id, media_id)
);
CREATE TRIGGER cloud_share_media_links_identity_immutable
BEFORE UPDATE OF account_id, source_share_id, media_id,
    remote_snapshot_id, created_at
ON cloud_share_media_links BEGIN
    SELECT RAISE(ABORT, 'cloud share media link identity is immutable');
END;
CREATE TRIGGER cloud_share_media_links_release_once
BEFORE UPDATE OF released_at ON cloud_share_media_links
WHEN NOT (OLD.released_at IS NULL AND NEW.released_at IS NOT NULL) BEGIN
    SELECT RAISE(ABORT, 'cloud share media link release is invalid');
END;
CREATE TRIGGER cloud_share_media_links_no_delete
BEFORE DELETE ON cloud_share_media_links BEGIN
    SELECT RAISE(ABORT, 'cloud share media link is immutable');
END;
CREATE TRIGGER cloud_share_published_media_no_delete
BEFORE DELETE ON cloud_share_media
WHEN EXISTS (
    SELECT 1 FROM cloud_share_media_links AS link
    WHERE link.account_id=OLD.account_id
    AND link.source_share_id=OLD.source_share_id
    AND link.media_id=OLD.media_id
    AND link.released_at IS NULL
) BEGIN
    SELECT RAISE(ABORT, 'published cloud share media is immutable');
END;
CREATE INDEX cloud_share_media_orphan_age
ON cloud_share_media(created_at, account_id, source_share_id);
CREATE TABLE cloud_share_operations (
    operation_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, idempotency_key)
);
CREATE TRIGGER cloud_share_operations_no_update
BEFORE UPDATE ON cloud_share_operations BEGIN
    SELECT RAISE(ABORT, 'cloud share operations are append-only');
END;
CREATE TRIGGER cloud_share_operations_no_delete
BEFORE DELETE ON cloud_share_operations BEGIN
    SELECT RAISE(ABORT, 'cloud share operations are append-only');
END;
CREATE TABLE cloud_share_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    previous_digest TEXT NOT NULL,
    key_id TEXT NOT NULL,
    mac_version INTEGER NOT NULL CHECK(mac_version IN (1,2)),
    entry_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER cloud_share_audit_key_identity_required
BEFORE INSERT ON cloud_share_audit
WHEN NEW.key_id IS NULL OR NEW.mac_version NOT IN (1,2) BEGIN
    SELECT RAISE(ABORT, 'cloud share audit key identity is required');
END;
CREATE TRIGGER cloud_share_audit_no_update
BEFORE UPDATE ON cloud_share_audit BEGIN
    SELECT RAISE(ABORT, 'cloud share audit is append-only');
END;
CREATE TRIGGER cloud_share_audit_no_delete
BEFORE DELETE ON cloud_share_audit BEGIN
    SELECT RAISE(ABORT, 'cloud share audit is append-only');
END;
"""


LEGACY_PRE_KEYRING_CLOUD_SHARE_SCHEMA_SQL = """
CREATE TABLE cloud_share_snapshots (
    remote_snapshot_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
    source_share_id TEXT NOT NULL, thread_id TEXT NOT NULL,
    source_watermark INTEGER NOT NULL, payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE,
    state_mac TEXT NOT NULL, status TEXT NOT NULL, expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL, revoked_at TEXT,
    UNIQUE(account_id, source_share_id));
CREATE TRIGGER cloud_share_identity_immutable
BEFORE UPDATE OF remote_snapshot_id, account_id, source_share_id,
    thread_id, source_watermark, payload_json, payload_sha256,
    token_sha256, expires_at, created_at
ON cloud_share_snapshots BEGIN
    SELECT RAISE(ABORT, 'cloud share identity is immutable'); END;
CREATE TRIGGER cloud_share_status_transition
BEFORE UPDATE OF status, revoked_at ON cloud_share_snapshots
WHEN NOT (OLD.status='active' AND NEW.status='revoked'
    AND OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'cloud share status transition is invalid'); END;
CREATE TRIGGER cloud_share_snapshots_no_delete BEFORE DELETE
ON cloud_share_snapshots BEGIN
    SELECT RAISE(ABORT, 'cloud share snapshots are append-only'); END;
CREATE TABLE cloud_share_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL, action TEXT NOT NULL, target_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL, previous_digest TEXT NOT NULL,
    entry_digest TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TRIGGER cloud_share_audit_no_update BEFORE UPDATE ON cloud_share_audit
BEGIN SELECT RAISE(ABORT, 'cloud share audit is append-only'); END;
CREATE TRIGGER cloud_share_audit_no_delete BEFORE DELETE ON cloud_share_audit
BEGIN SELECT RAISE(ABORT, 'cloud share audit is append-only'); END;
"""


# The one accepted pre-authority media layout.  This is the complete physical
# contract used by the last BLOB-backed Cloud Share repository: key identities
# were already persisted, while immutable media bytes still lived in SQLite.
# It is deliberately a full SQL contract rather than a loose column probe; any
# missing constraint, index, or same-name trigger is an unknown source and is
# rejected without repair.
LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SQL = """
CREATE TABLE cloud_share_snapshots (
    remote_snapshot_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    source_share_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    source_watermark INTEGER NOT NULL CHECK(source_watermark >= 0),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    token_sha256 TEXT NOT NULL UNIQUE,
    token_key_id TEXT NOT NULL,
    state_mac_version INTEGER NOT NULL CHECK(state_mac_version IN (1,2)),
    state_mac TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','revoked')),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    UNIQUE(account_id, source_share_id)
);
CREATE TRIGGER cloud_share_identity_immutable
BEFORE UPDATE OF remote_snapshot_id, account_id, source_share_id,
    thread_id, source_watermark, payload_json, payload_sha256,
    token_sha256, token_key_id, state_mac_version, expires_at, created_at
ON cloud_share_snapshots BEGIN
    SELECT RAISE(ABORT, 'cloud share identity is immutable');
END;
CREATE TRIGGER cloud_share_status_transition
BEFORE UPDATE OF status, revoked_at ON cloud_share_snapshots
WHEN NOT (
    OLD.status='active' AND NEW.status='revoked'
    AND OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL
) BEGIN
    SELECT RAISE(ABORT, 'cloud share status transition is invalid');
END;
CREATE TRIGGER cloud_share_snapshots_no_delete
BEFORE DELETE ON cloud_share_snapshots BEGIN
    SELECT RAISE(ABORT, 'cloud share snapshots are append-only');
END;
CREATE TRIGGER cloud_share_key_identity_required
BEFORE INSERT ON cloud_share_snapshots
WHEN NEW.token_key_id IS NULL OR NEW.state_mac_version NOT IN (1,2) BEGIN
    SELECT RAISE(ABORT, 'cloud share key identity is required');
END;
CREATE TABLE cloud_share_media (
    account_id TEXT NOT NULL,
    source_share_id TEXT NOT NULL,
    media_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('preview','thumbnail')),
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0 AND size_bytes <= 16777216),
    sha256 TEXT NOT NULL,
    content BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id, source_share_id, media_id),
    UNIQUE(account_id, idempotency_key)
);
CREATE TRIGGER cloud_share_media_no_update
BEFORE UPDATE ON cloud_share_media BEGIN
    SELECT RAISE(ABORT, 'cloud share media is immutable');
END;
CREATE TABLE cloud_share_media_links (
    account_id TEXT NOT NULL,
    source_share_id TEXT NOT NULL,
    media_id TEXT NOT NULL,
    remote_snapshot_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT,
    PRIMARY KEY(account_id, source_share_id, media_id),
    UNIQUE(remote_snapshot_id, media_id)
);
CREATE TRIGGER cloud_share_media_links_identity_immutable
BEFORE UPDATE OF account_id, source_share_id, media_id,
    remote_snapshot_id, created_at
ON cloud_share_media_links BEGIN
    SELECT RAISE(ABORT, 'cloud share media link identity is immutable');
END;
CREATE TRIGGER cloud_share_media_links_release_once
BEFORE UPDATE OF released_at ON cloud_share_media_links
WHEN NOT (OLD.released_at IS NULL AND NEW.released_at IS NOT NULL) BEGIN
    SELECT RAISE(ABORT, 'cloud share media link release is invalid');
END;
CREATE TRIGGER cloud_share_media_links_no_delete
BEFORE DELETE ON cloud_share_media_links BEGIN
    SELECT RAISE(ABORT, 'cloud share media link is immutable');
END;
CREATE TRIGGER cloud_share_published_media_no_delete
BEFORE DELETE ON cloud_share_media
WHEN EXISTS (
    SELECT 1 FROM cloud_share_media_links AS link
    WHERE link.account_id=OLD.account_id
    AND link.source_share_id=OLD.source_share_id
    AND link.media_id=OLD.media_id
    AND link.released_at IS NULL
) BEGIN
    SELECT RAISE(ABORT, 'published cloud share media is immutable');
END;
CREATE INDEX cloud_share_media_orphan_age
ON cloud_share_media(created_at, account_id, source_share_id);
CREATE TABLE cloud_share_operations (
    operation_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, idempotency_key)
);
CREATE TRIGGER cloud_share_operations_no_update
BEFORE UPDATE ON cloud_share_operations BEGIN
    SELECT RAISE(ABORT, 'cloud share operations are append-only');
END;
CREATE TRIGGER cloud_share_operations_no_delete
BEFORE DELETE ON cloud_share_operations BEGIN
    SELECT RAISE(ABORT, 'cloud share operations are append-only');
END;
CREATE TABLE cloud_share_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    previous_digest TEXT NOT NULL,
    key_id TEXT NOT NULL,
    mac_version INTEGER NOT NULL CHECK(mac_version IN (1,2)),
    entry_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER cloud_share_audit_key_identity_required
BEFORE INSERT ON cloud_share_audit
WHEN NEW.key_id IS NULL OR NEW.mac_version NOT IN (1,2) BEGIN
    SELECT RAISE(ABORT, 'cloud share audit key identity is required');
END;
CREATE TRIGGER cloud_share_audit_no_update
BEFORE UPDATE ON cloud_share_audit BEGIN
    SELECT RAISE(ABORT, 'cloud share audit is append-only');
END;
CREATE TRIGGER cloud_share_audit_no_delete
BEFORE DELETE ON cloud_share_audit BEGIN
    SELECT RAISE(ABORT, 'cloud share audit is append-only');
END;
"""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _records(connection: sqlite3.Connection) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": " ".join(str(row[3]).split()),
        }
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE name LIKE 'cloud_share_%' AND sql IS NOT NULL "
            "ORDER BY type,name"
        )
    )


def _schema_digest(connection: sqlite3.Connection) -> str:
    return _digest(_canonical(_records(connection)))


def _compiled_digest(sql: str) -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(sql)
        return _schema_digest(connection)
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
        raise CloudShareSchemaError("cloud share schema SQL is incomplete")


EMPTY_CLOUD_SHARE_SCHEMA_SHA256 = _digest(_canonical(()))
_EXPECTED_CLOUD_SHARE_SCHEMA_SHA256 = (
    "c2a4fad325d7b5cc590781e4bfc3a5ea9a705d7019000eb1984cae4a819a3e87"
)
_EXPECTED_LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256 = (
    "8f9734a8a0ee1364f3748b2de324025c0b85aad898cbc67f15a5dcd8a1d4c23a"
)
_compiled_target = _compiled_digest(CLOUD_SHARE_SCHEMA_SQL)
if _compiled_target != _EXPECTED_CLOUD_SHARE_SCHEMA_SHA256:  # pragma: no cover
    raise RuntimeError("compiled Cloud Share target schema fingerprint drifted")
CLOUD_SHARE_SCHEMA_SHA256 = _EXPECTED_CLOUD_SHARE_SCHEMA_SHA256
LEGACY_PRE_KEYRING_CLOUD_SHARE_SCHEMA_SHA256 = _compiled_digest(
    LEGACY_PRE_KEYRING_CLOUD_SHARE_SCHEMA_SQL
)
_compiled_legacy_blob = _compiled_digest(LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SQL)
if (  # pragma: no cover
    _compiled_legacy_blob != _EXPECTED_LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256
):
    raise RuntimeError("compiled legacy Cloud Share BLOB schema fingerprint drifted")
LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256 = (
    _EXPECTED_LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256
)
MIGRATION_001_NAME = "cloud-share-object-authority-v1"
MIGRATION_001_CHECKSUM = _digest(
    b"ecorex-cloud-share-schema-v1\0" + CLOUD_SHARE_SCHEMA_SQL.encode("utf-8")
)


@dataclass(frozen=True, slots=True)
class CloudShareSchemaReceipt:
    schema_version: int
    migration_version: int
    migration_name: str
    migration_checksum: str
    source_schema_sha256: str
    target_schema_sha256: str
    transformed_rows: int
    installed_at: str

    def __post_init__(self) -> None:
        if (
            self.schema_version != CLOUD_SHARE_SCHEMA_RECEIPT_VERSION
            or self.migration_version != CURRENT_CLOUD_SHARE_SCHEMA_VERSION
            or self.migration_name != MIGRATION_001_NAME
            or self.migration_checksum != MIGRATION_001_CHECKSUM
            or self.source_schema_sha256
            not in {
                EMPTY_CLOUD_SHARE_SCHEMA_SHA256,
                LEGACY_PRE_KEYRING_CLOUD_SHARE_SCHEMA_SHA256,
                LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256,
            }
            or self.target_schema_sha256 != CLOUD_SHARE_SCHEMA_SHA256
            or not isinstance(self.transformed_rows, int)
            or isinstance(self.transformed_rows, bool)
            or self.transformed_rows < 0
        ):
            raise CloudShareSchemaError("cloud share schema migration receipt is invalid")
        for digest in (
            self.migration_checksum,
            self.source_schema_sha256,
            self.target_schema_sha256,
        ):
            if not _is_digest(digest):
                raise CloudShareSchemaError(
                    "cloud share schema migration receipt is invalid"
                )
        try:
            installed = datetime.fromisoformat(self.installed_at)
        except (TypeError, ValueError) as error:
            raise CloudShareSchemaError(
                "cloud share schema migration receipt is invalid"
            ) from error
        if installed.tzinfo is None or installed.utcoffset() is None:
            raise CloudShareSchemaError("cloud share schema migration receipt is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "migration_version": self.migration_version,
            "migration_name": self.migration_name,
            "migration_checksum": self.migration_checksum,
            "source_schema_sha256": self.source_schema_sha256,
            "target_schema_sha256": self.target_schema_sha256,
            "transformed_rows": self.transformed_rows,
            "installed_at": self.installed_at,
        }


class CloudShareSchemaManager:
    """Deployment-owned migrator; repository processes call ``validate`` only."""

    def __init__(self, path: str | Path, *, keyring: Any) -> None:
        self.path = Path(path).expanduser().resolve()
        self.keyring = keyring

    def migrate(self) -> CloudShareSchemaReceipt:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _require_regular_or_absent(self.path)
        connection = self._connect(read_only=False)
        try:
            connection.execute("BEGIN EXCLUSIVE")
            source = _schema_digest(connection)
            names = {record["name"] for record in _records(connection)}
            if "cloud_share_schema_migrations" in names:
                receipt = self._validate_connection(connection)
                connection.commit()
                self._activate_wal(connection)
                return receipt
            if source == EMPTY_CLOUD_SHARE_SCHEMA_SHA256:
                _execute_sql(connection, CLOUD_SHARE_SCHEMA_SQL)
                transformed = 0
            elif source == LEGACY_PRE_KEYRING_CLOUD_SHARE_SCHEMA_SHA256:
                transformed = self._migrate_pre_keyring(connection)
            elif source == LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256:
                raise CloudShareSchemaError(
                    "legacy cloud share media requires the object migration command"
                )
            else:
                raise CloudShareSchemaError("cloud share schema source shape is unknown")
            return self._record_migration(
                connection,
                source_schema_sha256=source,
                transformed_rows=transformed,
            )
        except CloudShareSchemaError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            if connection.in_transaction:
                connection.rollback()
            raise CloudShareSchemaError("cloud share schema migration failed") from None
        finally:
            connection.close()

    def validate(self) -> CloudShareSchemaReceipt:
        _require_regular(self.path)
        connection = self._connect(read_only=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            receipt = self._validate_connection(connection)
            connection.commit()
            return receipt
        except CloudShareSchemaError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            if connection.in_transaction:
                connection.rollback()
            raise CloudShareSchemaError("cloud share schema validation failed") from None
        finally:
            connection.close()

    def _record_migration(
        self,
        connection: sqlite3.Connection,
        *,
        source_schema_sha256: str,
        transformed_rows: int,
    ) -> CloudShareSchemaReceipt:
        target = _schema_digest(connection)
        if target != CLOUD_SHARE_SCHEMA_SHA256:
            raise CloudShareSchemaError("cloud share schema migration target drifted")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise CloudShareSchemaError("cloud share schema migration violated references")
        installed_at = datetime.now(UTC).isoformat()
        receipt = CloudShareSchemaReceipt(
            schema_version=CLOUD_SHARE_SCHEMA_RECEIPT_VERSION,
            migration_version=CURRENT_CLOUD_SHARE_SCHEMA_VERSION,
            migration_name=MIGRATION_001_NAME,
            migration_checksum=MIGRATION_001_CHECKSUM,
            source_schema_sha256=source_schema_sha256,
            target_schema_sha256=target,
            transformed_rows=transformed_rows,
            installed_at=installed_at,
        )
        receipt_json = _canonical(receipt.to_dict()).decode("utf-8")
        connection.execute(
            "INSERT INTO cloud_share_schema_migrations("
            "version,migration_name,migration_checksum,source_schema_sha256,"
            "target_schema_sha256,transformed_rows,receipt_json,receipt_sha256,installed_at"
            ") VALUES(1,?,?,?,?,?,?,?,?)",
            (
                receipt.migration_name,
                receipt.migration_checksum,
                receipt.source_schema_sha256,
                receipt.target_schema_sha256,
                receipt.transformed_rows,
                receipt_json,
                _digest(receipt_json.encode("utf-8")),
                receipt.installed_at,
            ),
        )
        self._validate_connection(connection)
        connection.commit()
        self._activate_wal(connection)
        return receipt

    def _validate_connection(self, connection: sqlite3.Connection) -> CloudShareSchemaReceipt:
        if _schema_digest(connection) != CLOUD_SHARE_SCHEMA_SHA256:
            raise CloudShareSchemaError("cloud share schema object fingerprint is incompatible")
        rows = connection.execute(
            "SELECT * FROM cloud_share_schema_migrations ORDER BY version"
        ).fetchall()
        if not rows:
            raise CloudShareSchemaError("cloud share schema migration history is missing")
        versions = [int(row["version"]) for row in rows]
        if any(version > CURRENT_CLOUD_SHARE_SCHEMA_VERSION for version in versions):
            raise CloudShareSchemaError("cloud share schema is newer than this process")
        if versions != [1]:
            raise CloudShareSchemaError("cloud share schema migration history is incomplete")
        row = rows[0]
        receipt_json = str(row["receipt_json"])
        if (
            row["migration_name"] != MIGRATION_001_NAME
            or row["migration_checksum"] != MIGRATION_001_CHECKSUM
            or row["target_schema_sha256"] != CLOUD_SHARE_SCHEMA_SHA256
            or row["receipt_sha256"] != _digest(receipt_json.encode("utf-8"))
        ):
            raise CloudShareSchemaError("cloud share schema migration history is invalid")
        raw = json.loads(receipt_json)
        if not isinstance(raw, Mapping) or set(raw) != {
            "schema_version",
            "migration_version",
            "migration_name",
            "migration_checksum",
            "source_schema_sha256",
            "target_schema_sha256",
            "transformed_rows",
            "installed_at",
        }:
            raise CloudShareSchemaError("cloud share schema migration receipt is invalid")
        receipt = CloudShareSchemaReceipt(**dict(raw))
        if receipt_json.encode("utf-8") != _canonical(receipt.to_dict()):
            raise CloudShareSchemaError(
                "cloud share schema migration receipt is non-canonical"
            )
        if (
            receipt.schema_version != 1
            or receipt.migration_version != 1
            or receipt.migration_name != row["migration_name"]
            or receipt.migration_checksum != row["migration_checksum"]
            or receipt.source_schema_sha256 != row["source_schema_sha256"]
            or receipt.target_schema_sha256 != row["target_schema_sha256"]
            or receipt.transformed_rows != row["transformed_rows"]
            or receipt.installed_at != row["installed_at"]
        ):
            raise CloudShareSchemaError("cloud share schema migration receipt is invalid")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise CloudShareSchemaError("cloud share schema references are invalid")
        # Import lazily to keep the migration module dependent on this schema
        # authority while still making its optional history part of every
        # repository validation.  Fresh databases legitimately have no media
        # migration row; any present row is immutable and fully verified.
        from .share_media_migration import (
            validate_cloud_share_media_history_connection,
        )

        validate_cloud_share_media_history_connection(connection)
        quick = connection.execute("PRAGMA quick_check").fetchone()
        if quick is None or str(quick[0]).casefold() != "ok":
            raise CloudShareSchemaError("cloud share schema integrity check failed")
        return receipt

    def _migrate_pre_keyring(self, connection: sqlite3.Connection) -> int:
        snapshots = connection.execute("SELECT * FROM cloud_share_snapshots").fetchall()
        audits = connection.execute(
            "SELECT * FROM cloud_share_audit ORDER BY sequence"
        ).fetchall()
        key_id, key = self._legacy_key(len(snapshots) + len(audits))
        for row in snapshots:
            encoded = (
                f"ecorex-cloud-share-state-v1\n{row['remote_snapshot_id']}\0"
                f"{row['account_id']}\0{row['source_share_id']}\0{row['thread_id']}\0"
                f"{row['source_watermark']}\0{row['payload_sha256']}\0{row['token_sha256']}\0"
                f"{row['status']}\0{row['expires_at']}\0{row['created_at']}\0"
                f"{row['revoked_at'] or ''}"
            ).encode("utf-8")
            if not hmac.compare_digest(
                hmac.new(key, encoded, hashlib.sha256).hexdigest(),
                str(row["state_mac"]),
            ):
                raise CloudShareSchemaError("legacy cloud share state key is invalid")
        for row in audits:
            encoded = (
                f"ecorex-cloud-share-audit-v1\n{row['sequence']}\0{row['event_id']}\0"
                f"{row['account_id']}\0{row['action']}\0{row['target_id']}\0"
                f"{row['payload_sha256']}\0{row['previous_digest']}\0{row['created_at']}"
            ).encode("utf-8")
            expected = hmac.new(
                key,
                b"ecorex-cloud-share-audit-mac-v1\n" + encoded,
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, str(row["entry_digest"])):
                raise CloudShareSchemaError("legacy cloud share audit key is invalid")
        for trigger in (
            "cloud_share_identity_immutable",
            "cloud_share_status_transition",
            "cloud_share_snapshots_no_delete",
            "cloud_share_audit_no_update",
            "cloud_share_audit_no_delete",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP TABLE cloud_share_audit")
        connection.execute("DROP TABLE cloud_share_snapshots")
        _execute_sql(connection, CLOUD_SHARE_SCHEMA_SQL)
        connection.executemany(
            "INSERT INTO cloud_share_snapshots("
            "remote_snapshot_id,account_id,source_share_id,thread_id,source_watermark,"
            "payload_json,payload_sha256,token_sha256,token_key_id,state_mac_version,"
            "state_mac,status,expires_at,created_at,revoked_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)",
            [
                (
                    row["remote_snapshot_id"], row["account_id"], row["source_share_id"],
                    row["thread_id"], row["source_watermark"], row["payload_json"],
                    row["payload_sha256"], row["token_sha256"], key_id,
                    row["state_mac"], row["status"], row["expires_at"],
                    row["created_at"], row["revoked_at"],
                )
                for row in snapshots
            ],
        )
        connection.executemany(
            "INSERT INTO cloud_share_audit("
            "sequence,event_id,account_id,action,target_id,payload_sha256,previous_digest,"
            "key_id,mac_version,entry_digest,created_at) VALUES(?,?,?,?,?,?,?, ?,1,?,?)",
            [
                (
                    row["sequence"], row["event_id"], row["account_id"], row["action"],
                    row["target_id"], row["payload_sha256"], row["previous_digest"],
                    key_id, row["entry_digest"], row["created_at"],
                )
                for row in audits
            ],
        )
        return len(snapshots) + len(audits)

    def _legacy_key(self, row_count: int) -> tuple[str, bytes]:
        keys = getattr(self.keyring, "keys", None)
        active = getattr(self.keyring, "active_key_id", None)
        legacy = getattr(self.keyring, "legacy_key_id", None)
        if not isinstance(keys, Mapping) or not isinstance(active, str):
            raise CloudShareSchemaError("cloud share legacy keyring is invalid")
        if row_count and legacy is None and len(keys) > 1:
            raise ValueError("populated legacy cloud share storage requires an explicit legacy key identity")
        key_id = legacy or active
        key = keys.get(key_id)
        if not isinstance(key, bytes) or len(key) != 32:
            raise CloudShareSchemaError("cloud share legacy signing key is unavailable")
        return key_id, key

    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        target = (
            f"{self.path.as_uri()}?mode=ro&nofollow=1"
            if read_only
            else f"{self.path.as_uri()}?mode=rwc&nofollow=1"
        )
        connection = sqlite3.connect(
            target,
            uri=True,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA recursive_triggers=ON")
        if not read_only:
            connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _activate_wal(connection: sqlite3.Connection) -> None:
        # ``journal_mode`` changes cannot run inside the schema transaction.
        # After that transaction commits, another authorized migrator may win
        # ``BEGIN EXCLUSIVE`` before this connection can reassert WAL. SQLite
        # does not consistently apply ``busy_timeout`` to this PRAGMA, so only
        # BUSY/LOCKED contention is retried here. Corruption, I/O failures and
        # all other schema errors remain immediate, fail-closed errors.
        deadline = time.monotonic() + _WAL_ACTIVATION_TIMEOUT_SECONDS
        retry_seconds = _WAL_ACTIVATION_INITIAL_RETRY_SECONDS
        while True:
            try:
                mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
                verified = connection.execute("PRAGMA journal_mode").fetchone()
            except sqlite3.OperationalError as error:
                if not _is_sqlite_lock_contention(error):
                    raise CloudShareSchemaError(
                        "cloud share schema WAL activation failed"
                    ) from None
                mode = None
                verified = None
            if (
                mode is not None
                and verified is not None
                and str(mode[0]).casefold() == "wal"
                and str(verified[0]).casefold() == "wal"
            ):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CloudShareSchemaError(
                    "cloud share schema WAL activation timed out"
                )
            time.sleep(min(retry_seconds, remaining))
            retry_seconds = min(
                retry_seconds * 2,
                _WAL_ACTIVATION_MAX_RETRY_SECONDS,
            )


def migrate_cloud_share_database(
    path: str | Path,
    *,
    keyring: Any,
) -> CloudShareSchemaReceipt:
    """Explicit deployment/test composition entry point; never called by Repository."""

    return CloudShareSchemaManager(path, keyring=keyring).migrate()


def validate_cloud_share_database(
    path: str | Path,
    *,
    keyring: Any,
) -> CloudShareSchemaReceipt:
    """Validate Cloud Share storage without creating, repairing, or enabling WAL."""

    return CloudShareSchemaManager(path, keyring=keyring).validate()


def _is_digest(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX_DIGEST


def _is_sqlite_lock_contention(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    message = str(error).strip().casefold()
    return message in {
        "database is locked",
        "database table is locked",
        "database schema is locked",
    }


def _require_regular_or_absent(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise CloudShareSchemaError("cloud share schema database is unavailable") from error
    # A concurrent authorized migrator may have created the SQLite inode but
    # not written its first page yet.  Empty regular files are valid only at
    # this migration boundary; runtime validation still rejects them.
    _require_regular(path, allow_empty=True)


def _require_regular(path: Path, *, allow_empty: bool = False) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CloudShareSchemaError("cloud share schema database is unavailable") from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or not stat.S_ISREG(metadata.st_mode)
        or (metadata.st_size <= 0 and not allow_empty)
    ):
        raise CloudShareSchemaError("cloud share schema database must be a regular file")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ecorex-share-schema")
    parser.add_argument("command", choices=("migrate", "validate"))
    parser.add_argument("database", type=Path)
    args = parser.parse_args(argv)
    # CLI fresh migrations need no legacy key material.  Legacy key migration
    # is intentionally performed by deployment composition with a real keyring.
    class _FreshKeyring:
        active_key_id = "deployment"
        legacy_key_id = None
        keys = {"deployment": b"\0" * 32}

    manager = CloudShareSchemaManager(args.database, keyring=_FreshKeyring())
    receipt = manager.migrate() if args.command == "migrate" else manager.validate()
    print(_canonical(receipt.to_dict()).decode("utf-8"))
    return 0


if __name__ == "__main__":  # pragma: no cover - deployment CLI
    raise SystemExit(main())


__all__ = [
    "CLOUD_SHARE_SCHEMA_SHA256",
    "CLOUD_SHARE_SCHEMA_SQL",
    "CURRENT_CLOUD_SHARE_SCHEMA_VERSION",
    "CloudShareSchemaError",
    "CloudShareSchemaManager",
    "CloudShareSchemaReceipt",
    "EMPTY_CLOUD_SHARE_SCHEMA_SHA256",
    "LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256",
    "LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SQL",
    "LEGACY_PRE_KEYRING_CLOUD_SHARE_SCHEMA_SHA256",
    "LEGACY_PRE_KEYRING_CLOUD_SHARE_SCHEMA_SQL",
    "migrate_cloud_share_database",
    "validate_cloud_share_database",
]
