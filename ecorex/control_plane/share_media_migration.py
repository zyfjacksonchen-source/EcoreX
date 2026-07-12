"""Crash-safe two-phase migration from legacy Cloud Share BLOBs to CAS.

Preparation reads at most one bounded BLOB in each short read transaction,
writes and verifies immutable objects without holding a database lock, then
persists a canonical checkpoint.  Finalization re-verifies both the source
metadata and every prepared object before taking a short exclusive transaction
that only replaces the media metadata table and records immutable receipts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from typing import Any, Mapping, Sequence

from .share_objects import ShareObjectError, ShareObjectStore, ShareStoredObject
from .share_schema import (
    CLOUD_SHARE_SCHEMA_RECEIPT_VERSION,
    CLOUD_SHARE_SCHEMA_SHA256,
    CLOUD_SHARE_SCHEMA_SQL,
    CURRENT_CLOUD_SHARE_SCHEMA_VERSION,
    LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256,
    MIGRATION_001_CHECKSUM,
    MIGRATION_001_NAME,
    CloudShareSchemaError,
    CloudShareSchemaManager,
    CloudShareSchemaReceipt,
    _canonical,
    _digest,
    _schema_digest,
)


CURRENT_CLOUD_SHARE_MEDIA_MIGRATION_VERSION = 1
CLOUD_SHARE_MEDIA_RECEIPT_VERSION = 1
CLOUD_SHARE_MEDIA_MIGRATION_NAME = "cloud-share-blob-to-cas-v1"
CLOUD_SHARE_MEDIA_MIGRATION_CHECKSUM = hashlib.sha256(
    b"ecorex-cloud-share-blob-to-cas-v1\0"
    + LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256.encode("ascii")
    + b"\0"
    + CLOUD_SHARE_SCHEMA_SHA256.encode("ascii")
).hexdigest()

_MAX_MEDIA_BYTES = 16 * 1024 * 1024
_DEFAULT_MAX_ROWS = 4096
_DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_ABSOLUTE_MAX_ROWS = 100_000
_ABSOLUTE_MAX_TOTAL_BYTES = 64 * 1024 * 1024 * 1024
_MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_MEDIA_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif", "image/avif"}
)


@dataclass(frozen=True, slots=True)
class _PreparedMedia:
    account_id: str
    source_share_id: str
    media_id: str
    idempotency_key: str
    kind: str
    mime_type: str
    size_bytes: int
    sha256: str
    object_key: str
    etag: str
    created_at: str

    def __post_init__(self) -> None:
        if (
            not _ID.fullmatch(self.account_id)
            or not _ID.fullmatch(self.source_share_id)
            or not _MEDIA_ID.fullmatch(self.media_id)
            or not _IDEMPOTENCY.fullmatch(self.idempotency_key)
            or self.kind not in {"preview", "thumbnail"}
            or self.mime_type not in _MIME_TYPES
            or not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or not 1 <= self.size_bytes <= _MAX_MEDIA_BYTES
            or not _DIGEST.fullmatch(self.sha256)
            or self.etag != self.sha256
        ):
            raise CloudShareSchemaError("legacy cloud share media metadata is invalid")
        try:
            created = datetime.fromisoformat(self.created_at)
            ShareStoredObject(
                object_key=self.object_key,
                sha256=self.sha256,
                size_bytes=self.size_bytes,
                mime_type=self.mime_type,
                etag=self.etag,
            )
        except (TypeError, ValueError) as error:
            raise CloudShareSchemaError(
                "legacy cloud share media metadata is invalid"
            ) from error
        if created.tzinfo is None or created.utcoffset() is None:
            raise CloudShareSchemaError("legacy cloud share media timestamp is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "source_share_id": self.source_share_id,
            "media_id": self.media_id,
            "idempotency_key": self.idempotency_key,
            "kind": self.kind,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "object_key": self.object_key,
            "etag": self.etag,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class CloudShareMediaMigrationCheckpoint:
    checkpoint_version: int
    migration_checksum: str
    database_identity_sha256: str
    source_schema_sha256: str
    source_data_sha256: str
    prepared_rows: tuple[_PreparedMedia, ...]
    total_bytes: int
    prepared_at: str

    def __post_init__(self) -> None:
        if (
            self.checkpoint_version != 1
            or self.migration_checksum != CLOUD_SHARE_MEDIA_MIGRATION_CHECKSUM
            or not _DIGEST.fullmatch(self.database_identity_sha256)
            or self.source_schema_sha256 != LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256
            or not _DIGEST.fullmatch(self.source_data_sha256)
            or not isinstance(self.prepared_rows, tuple)
            or len(self.prepared_rows) > _ABSOLUTE_MAX_ROWS
            or not isinstance(self.total_bytes, int)
            or isinstance(self.total_bytes, bool)
            or self.total_bytes != sum(row.size_bytes for row in self.prepared_rows)
            or self.total_bytes > _ABSOLUTE_MAX_TOTAL_BYTES
        ):
            raise CloudShareSchemaError("cloud share media checkpoint is invalid")
        identities: set[tuple[str, str, str]] = set()
        idempotency: set[tuple[str, str]] = set()
        descriptors: dict[str, tuple[str, int, str, str]] = {}
        object_keys: dict[str, str] = {}
        previous: tuple[str, str, str] | None = None
        for row in self.prepared_rows:
            identity = (row.account_id, row.source_share_id, row.media_id)
            if (
                identity in identities
                or (row.account_id, row.idempotency_key) in idempotency
                or (previous is not None and identity <= previous)
            ):
                raise CloudShareSchemaError("cloud share media checkpoint is ambiguous")
            identities.add(identity)
            idempotency.add((row.account_id, row.idempotency_key))
            previous = identity
            descriptor = (row.object_key, row.size_bytes, row.mime_type, row.etag)
            prior = descriptors.setdefault(row.sha256, descriptor)
            key_digest = object_keys.setdefault(row.object_key, row.sha256)
            if prior != descriptor or key_digest != row.sha256:
                raise CloudShareSchemaError("cloud share media object identity is ambiguous")
        if self.source_data_sha256 != _source_data_digest(self.prepared_rows):
            raise CloudShareSchemaError("cloud share media checkpoint source digest is invalid")
        try:
            prepared = datetime.fromisoformat(self.prepared_at)
        except (TypeError, ValueError) as error:
            raise CloudShareSchemaError("cloud share media checkpoint is invalid") from error
        if prepared.tzinfo is None or prepared.utcoffset() is None:
            raise CloudShareSchemaError("cloud share media checkpoint is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_version": self.checkpoint_version,
            "migration_checksum": self.migration_checksum,
            "database_identity_sha256": self.database_identity_sha256,
            "source_schema_sha256": self.source_schema_sha256,
            "source_data_sha256": self.source_data_sha256,
            "prepared_rows": [row.to_dict() for row in self.prepared_rows],
            "total_bytes": self.total_bytes,
            "prepared_at": self.prepared_at,
        }


@dataclass(frozen=True, slots=True)
class CloudShareMediaMigrationReceipt:
    receipt_version: int
    migration_version: int
    migration_name: str
    migration_checksum: str
    source_schema_sha256: str
    target_schema_sha256: str
    source_data_sha256: str
    checkpoint_sha256: str
    migrated_rows: int
    migrated_objects: int
    migrated_bytes: int
    prepared_at: str
    installed_at: str

    def __post_init__(self) -> None:
        if (
            self.receipt_version != CLOUD_SHARE_MEDIA_RECEIPT_VERSION
            or self.migration_version != CURRENT_CLOUD_SHARE_MEDIA_MIGRATION_VERSION
            or self.migration_name != CLOUD_SHARE_MEDIA_MIGRATION_NAME
            or self.migration_checksum != CLOUD_SHARE_MEDIA_MIGRATION_CHECKSUM
            or self.source_schema_sha256 != LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256
            or self.target_schema_sha256 != CLOUD_SHARE_SCHEMA_SHA256
            or not _DIGEST.fullmatch(self.source_data_sha256)
            or not _DIGEST.fullmatch(self.checkpoint_sha256)
            or not isinstance(self.migrated_rows, int)
            or isinstance(self.migrated_rows, bool)
            or not 0 <= self.migrated_rows <= _ABSOLUTE_MAX_ROWS
            or not isinstance(self.migrated_objects, int)
            or isinstance(self.migrated_objects, bool)
            or not 0 <= self.migrated_objects <= self.migrated_rows
            or not isinstance(self.migrated_bytes, int)
            or isinstance(self.migrated_bytes, bool)
            or not 0 <= self.migrated_bytes <= _ABSOLUTE_MAX_TOTAL_BYTES
        ):
            raise CloudShareSchemaError("cloud share media migration receipt is invalid")
        for value in (self.prepared_at, self.installed_at):
            try:
                timestamp = datetime.fromisoformat(value)
            except (TypeError, ValueError) as error:
                raise CloudShareSchemaError(
                    "cloud share media migration receipt is invalid"
                ) from error
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise CloudShareSchemaError(
                    "cloud share media migration receipt is invalid"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_version": self.receipt_version,
            "migration_version": self.migration_version,
            "migration_name": self.migration_name,
            "migration_checksum": self.migration_checksum,
            "source_schema_sha256": self.source_schema_sha256,
            "target_schema_sha256": self.target_schema_sha256,
            "source_data_sha256": self.source_data_sha256,
            "checkpoint_sha256": self.checkpoint_sha256,
            "migrated_rows": self.migrated_rows,
            "migrated_objects": self.migrated_objects,
            "migrated_bytes": self.migrated_bytes,
            "prepared_at": self.prepared_at,
            "installed_at": self.installed_at,
        }


def prepare_cloud_share_media_objects(
    path: str | Path,
    *,
    object_store: ShareObjectStore,
    checkpoint_path: str | Path | None = None,
    max_rows: int = _DEFAULT_MAX_ROWS,
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
) -> CloudShareMediaMigrationCheckpoint:
    """Prepare and durably checkpoint legacy media without a database write lock."""

    database = _database_path(path)
    checkpoint_file = _checkpoint_path(database, checkpoint_path)
    _require_limits(max_rows, max_total_bytes)
    if not isinstance(object_store, ShareObjectStore):
        raise TypeError("cloud share object store is invalid")
    if os.path.lexists(checkpoint_file):
        checkpoint = _read_checkpoint(checkpoint_file)
        if checkpoint.database_identity_sha256 != _database_identity(database):
            raise CloudShareSchemaError("cloud share media checkpoint database is invalid")
        _verify_source_metadata(database, checkpoint)
        _verify_objects(object_store, checkpoint.prepared_rows)
        return checkpoint

    rows: list[_PreparedMedia] = []
    cursor: tuple[str, str, str] | None = None
    total_bytes = 0
    while True:
        raw = _read_one_legacy_row(database, cursor)
        if raw is None:
            break
        if len(rows) >= max_rows:
            raise CloudShareSchemaError("legacy cloud share media exceeds migration row limit")
        content = raw.pop("content")
        if not isinstance(content, bytes):
            content = bytes(content)
        _validate_content(raw, content)
        total_bytes += len(content)
        if total_bytes > max_total_bytes:
            raise CloudShareSchemaError("legacy cloud share media exceeds migration byte limit")
        try:
            stored = object_store.put(
                content,
                sha256=str(raw["sha256"]),
                mime_type=str(raw["mime_type"]),
            )
        except ShareObjectError:
            raise CloudShareSchemaError("legacy cloud share media CAS preparation failed") from None
        if not isinstance(stored, ShareStoredObject):
            raise CloudShareSchemaError("legacy cloud share media CAS descriptor is invalid")
        prepared = _PreparedMedia(
            **raw,
            object_key=stored.object_key,
            etag=stored.etag,
        )
        if (
            stored.sha256 != prepared.sha256
            or stored.size_bytes != prepared.size_bytes
            or stored.mime_type != prepared.mime_type
            or stored.etag != prepared.sha256
        ):
            raise CloudShareSchemaError("legacy cloud share media CAS descriptor is invalid")
        rows.append(prepared)
        cursor = (prepared.account_id, prepared.source_share_id, prepared.media_id)

    checkpoint = CloudShareMediaMigrationCheckpoint(
        checkpoint_version=1,
        migration_checksum=CLOUD_SHARE_MEDIA_MIGRATION_CHECKSUM,
        database_identity_sha256=_database_identity(database),
        source_schema_sha256=LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256,
        source_data_sha256=_source_data_digest(tuple(rows)),
        prepared_rows=tuple(rows),
        total_bytes=total_bytes,
        prepared_at=datetime.now(UTC).isoformat(),
    )
    _verify_source_metadata(database, checkpoint)
    _verify_objects(object_store, checkpoint.prepared_rows)
    persisted = _write_checkpoint(checkpoint_file, checkpoint)
    _verify_source_metadata(database, persisted)
    _verify_objects(object_store, persisted.prepared_rows)
    return persisted


def finalize_cloud_share_media_objects(
    path: str | Path,
    *,
    object_store: ShareObjectStore,
    checkpoint_path: str | Path | None = None,
) -> CloudShareMediaMigrationReceipt:
    """Atomically switch prepared legacy BLOB rows to immutable object references."""

    database = _database_path(path)
    checkpoint_file = _checkpoint_path(database, checkpoint_path)
    existing = _existing_receipt(database)
    if existing is not None:
        _remove_checkpoint(checkpoint_file)
        return existing
    try:
        checkpoint = _read_checkpoint(checkpoint_file)
    except CloudShareSchemaError:
        # Another authorized finalizer may have committed and removed the
        # checkpoint between our initial receipt read and this open.
        concurrent = _existing_receipt(database)
        if concurrent is not None:
            return concurrent
        raise
    if checkpoint.database_identity_sha256 != _database_identity(database):
        raise CloudShareSchemaError("cloud share media checkpoint database is invalid")
    # Both operations are intentionally before BEGIN EXCLUSIVE.  A malicious or
    # incomplete remote object store must never lengthen the database lock.
    try:
        _verify_source_metadata(database, checkpoint)
    except CloudShareSchemaError:
        concurrent = _existing_receipt(database)
        if concurrent is not None:
            _remove_checkpoint(checkpoint_file)
            return concurrent
        raise
    _verify_objects(object_store, checkpoint.prepared_rows)
    checkpoint_sha256 = _digest(_canonical(checkpoint.to_dict()))
    installed_at = datetime.now(UTC).isoformat()
    object_count = len({row.sha256 for row in checkpoint.prepared_rows})
    receipt = CloudShareMediaMigrationReceipt(
        receipt_version=CLOUD_SHARE_MEDIA_RECEIPT_VERSION,
        migration_version=CURRENT_CLOUD_SHARE_MEDIA_MIGRATION_VERSION,
        migration_name=CLOUD_SHARE_MEDIA_MIGRATION_NAME,
        migration_checksum=CLOUD_SHARE_MEDIA_MIGRATION_CHECKSUM,
        source_schema_sha256=checkpoint.source_schema_sha256,
        target_schema_sha256=CLOUD_SHARE_SCHEMA_SHA256,
        source_data_sha256=checkpoint.source_data_sha256,
        checkpoint_sha256=checkpoint_sha256,
        migrated_rows=len(checkpoint.prepared_rows),
        migrated_objects=object_count,
        migrated_bytes=checkpoint.total_bytes,
        prepared_at=checkpoint.prepared_at,
        installed_at=installed_at,
    )

    connection = _connect(database, read_only=False)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        locked_digest = _schema_digest(connection)
        if locked_digest == CLOUD_SHARE_SCHEMA_SHA256:
            CloudShareSchemaManager(
                database, keyring=_UnusedKeyring()
            )._validate_connection(connection)
            concurrent = validate_cloud_share_media_history_connection(connection)
            if concurrent is None:
                raise CloudShareSchemaError(
                    "current cloud share schema has no legacy media receipt"
                )
            connection.commit()
            _activate_wal(connection)
            _remove_checkpoint(checkpoint_file)
            return concurrent
        if locked_digest != LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256:
            raise CloudShareSchemaError("legacy cloud share media source changed before finalize")
        locked_totals = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM cloud_share_media"
        ).fetchone()
        if (
            int(locked_totals[0]) != len(checkpoint.prepared_rows)
            or int(locked_totals[1]) != checkpoint.total_bytes
        ):
            raise CloudShareSchemaError("legacy cloud share media source changed before finalize")
        current_source = _source_metadata(connection)
        expected_source = _checkpoint_source_metadata(checkpoint)
        if (
            _digest(_canonical(current_source)) != checkpoint.source_data_sha256
            or current_source != expected_source
        ):
            raise CloudShareSchemaError("legacy cloud share media source changed before finalize")
        for statement in (
            "DROP TRIGGER cloud_share_media_no_update",
            "DROP TRIGGER cloud_share_published_media_no_delete",
            "DROP INDEX cloud_share_media_orphan_age",
            "DROP TABLE cloud_share_media",
        ):
            connection.execute(statement)
        _create_missing_target_objects(connection)
        _insert_object_metadata(connection, checkpoint.prepared_rows)
        connection.executemany(
            "INSERT INTO cloud_share_media("
            "account_id,source_share_id,media_id,idempotency_key,kind,mime_type,"
            "size_bytes,sha256,object_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row.account_id,
                    row.source_share_id,
                    row.media_id,
                    row.idempotency_key,
                    row.kind,
                    row.mime_type,
                    row.size_bytes,
                    row.sha256,
                    row.object_key,
                    row.created_at,
                )
                for row in checkpoint.prepared_rows
            ],
        )
        _insert_schema_receipt(connection, receipt)
        _insert_media_receipt(connection, receipt)
        if _schema_digest(connection) != CLOUD_SHARE_SCHEMA_SHA256:
            raise CloudShareSchemaError("cloud share media migration target drifted")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise CloudShareSchemaError("cloud share media migration references are invalid")
        quick = connection.execute("PRAGMA quick_check").fetchone()
        if quick is None or str(quick[0]).casefold() != "ok":
            raise CloudShareSchemaError("cloud share media migration integrity check failed")
        CloudShareSchemaManager(database, keyring=_UnusedKeyring())._validate_connection(
            connection
        )
        validated = validate_cloud_share_media_history_connection(connection)
        if validated != receipt:
            raise CloudShareSchemaError("cloud share media migration receipt is inconsistent")
        connection.commit()
        _activate_wal(connection)
    except CloudShareSchemaError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        if connection.in_transaction:
            connection.rollback()
        raise CloudShareSchemaError("cloud share media migration failed") from None
    finally:
        connection.close()
    _remove_checkpoint(checkpoint_file)
    return receipt


def migrate_cloud_share_media_objects(
    path: str | Path,
    *,
    object_store: ShareObjectStore,
    checkpoint_path: str | Path | None = None,
    max_rows: int = _DEFAULT_MAX_ROWS,
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
) -> CloudShareMediaMigrationReceipt:
    """Idempotently prepare then finalize the one known legacy BLOB layout."""

    database = _database_path(path)
    existing = _existing_receipt(database)
    if existing is not None:
        _remove_checkpoint(_checkpoint_path(database, checkpoint_path))
        return existing
    try:
        prepare_cloud_share_media_objects(
            database,
            object_store=object_store,
            checkpoint_path=checkpoint_path,
            max_rows=max_rows,
            max_total_bytes=max_total_bytes,
        )
    except CloudShareSchemaError:
        concurrent = _existing_receipt(database)
        if concurrent is not None:
            return concurrent
        raise
    return finalize_cloud_share_media_objects(
        database,
        object_store=object_store,
        checkpoint_path=checkpoint_path,
    )


def validate_cloud_share_media_history_connection(
    connection: sqlite3.Connection,
) -> CloudShareMediaMigrationReceipt | None:
    rows = connection.execute(
        "SELECT * FROM cloud_share_media_migrations ORDER BY version"
    ).fetchall()
    if not rows:
        return None
    versions = [int(row["version"]) for row in rows]
    if any(version > CURRENT_CLOUD_SHARE_MEDIA_MIGRATION_VERSION for version in versions):
        raise CloudShareSchemaError("cloud share media schema is newer than this process")
    if versions != [1]:
        raise CloudShareSchemaError("cloud share media migration history is incomplete")
    row = rows[0]
    receipt_json = str(row["receipt_json"])
    if (
        row["migration_checksum"] != CLOUD_SHARE_MEDIA_MIGRATION_CHECKSUM
        or row["receipt_sha256"] != _digest(receipt_json.encode("utf-8"))
    ):
        raise CloudShareSchemaError("cloud share media migration history is invalid")
    raw = json.loads(receipt_json)
    expected = set(CloudShareMediaMigrationReceipt.__dataclass_fields__)
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise CloudShareSchemaError("cloud share media migration receipt is invalid")
    receipt = CloudShareMediaMigrationReceipt(**dict(raw))
    if receipt_json.encode("utf-8") != _canonical(receipt.to_dict()):
        raise CloudShareSchemaError("cloud share media migration receipt is non-canonical")
    if (
        receipt.migration_version != int(row["version"])
        or receipt.migration_checksum != str(row["migration_checksum"])
        or receipt.installed_at != str(row["installed_at"])
    ):
        raise CloudShareSchemaError("cloud share media migration receipt is inconsistent")
    return receipt


def _read_one_legacy_row(
    database: Path,
    cursor: tuple[str, str, str] | None,
) -> dict[str, Any] | None:
    connection = _connect(database, read_only=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if _schema_digest(connection) != LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256:
            raise CloudShareSchemaError("cloud share media source shape is unknown")
        where = ""
        parameters: tuple[str, ...] = ()
        if cursor is not None:
            where = "WHERE (account_id,source_share_id,media_id) > (?,?,?) "
            parameters = cursor
        row = connection.execute(
            "SELECT account_id,source_share_id,media_id,idempotency_key,kind,mime_type,"
            "size_bytes,sha256,content,created_at FROM cloud_share_media "
            + where
            + "ORDER BY account_id,source_share_id,media_id LIMIT 1",
            parameters,
        ).fetchone()
        connection.commit()
        return None if row is None else dict(row)
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _source_metadata(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    raw = connection.execute(
        "SELECT account_id,source_share_id,media_id,idempotency_key,kind,mime_type,"
        "size_bytes,sha256,created_at FROM cloud_share_media "
        "ORDER BY account_id,source_share_id,media_id"
    ).fetchall()
    return [
        {
            "account_id": str(row["account_id"]),
            "source_share_id": str(row["source_share_id"]),
            "media_id": str(row["media_id"]),
            "idempotency_key": str(row["idempotency_key"]),
            "kind": str(row["kind"]),
            "mime_type": str(row["mime_type"]),
            "size_bytes": int(row["size_bytes"]),
            "sha256": str(row["sha256"]),
            "created_at": str(row["created_at"]),
        }
        for row in raw
    ]


def _checkpoint_source_metadata(
    checkpoint: CloudShareMediaMigrationCheckpoint,
) -> list[dict[str, Any]]:
    return [
        {
            key: value
            for key, value in row.to_dict().items()
            if key not in {"object_key", "etag"}
        }
        for row in checkpoint.prepared_rows
    ]


def _source_data_digest(rows: Sequence[_PreparedMedia]) -> str:
    return _digest(
        _canonical(
            [
                {
                    key: value
                    for key, value in row.to_dict().items()
                    if key not in {"object_key", "etag"}
                }
                for row in rows
            ]
        )
    )


def _verify_source_metadata(
    database: Path,
    checkpoint: CloudShareMediaMigrationCheckpoint,
) -> None:
    connection = _connect(database, read_only=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if _schema_digest(connection) != LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256:
            raise CloudShareSchemaError("cloud share media source shape is unknown")
        totals = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM cloud_share_media"
        ).fetchone()
        if (
            int(totals[0]) != len(checkpoint.prepared_rows)
            or int(totals[1]) != checkpoint.total_bytes
        ):
            raise CloudShareSchemaError("legacy cloud share media changed during preparation")
        raw_rows = connection.execute(
            "SELECT account_id,source_share_id,media_id,idempotency_key,kind,mime_type,"
            "size_bytes,sha256,created_at FROM cloud_share_media "
            "ORDER BY account_id,source_share_id,media_id"
        ).fetchall()
        source = _source_metadata_from_rows(raw_rows)
        expected = _checkpoint_source_metadata(checkpoint)
        if source != expected or _digest(_canonical(source)) != checkpoint.source_data_sha256:
            raise CloudShareSchemaError("legacy cloud share media changed during preparation")
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _verify_objects(
    object_store: ShareObjectStore,
    rows: Sequence[_PreparedMedia],
) -> None:
    verified: set[str] = set()
    for row in rows:
        if row.sha256 in verified:
            continue
        opened = None
        try:
            opened = object_store.open(
                row.object_key,
                sha256=row.sha256,
                size_bytes=row.size_bytes,
                mime_type=row.mime_type,
            )
            if (
                opened.descriptor.object_key != row.object_key
                or opened.descriptor.sha256 != row.sha256
                or opened.descriptor.size_bytes != row.size_bytes
                or opened.descriptor.mime_type != row.mime_type
                or opened.descriptor.etag != row.etag
            ):
                opened.close()
                raise ShareObjectError("prepared object descriptor changed")
            digest = hashlib.sha256()
            observed = 0
            for chunk in opened.iter_range(0, row.size_bytes - 1):
                observed += len(chunk)
                digest.update(chunk)
        except (
            AttributeError,
            ShareObjectError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            if opened is not None:
                opened.close()
            raise CloudShareSchemaError("prepared cloud share media object is unavailable") from None
        if observed != row.size_bytes or digest.hexdigest() != row.sha256:
            raise CloudShareSchemaError("prepared cloud share media object is invalid")
        verified.add(row.sha256)


def _source_metadata_from_rows(
    rows: Sequence[sqlite3.Row],
) -> list[dict[str, Any]]:
    return [
        {
            "account_id": str(row["account_id"]),
            "source_share_id": str(row["source_share_id"]),
            "media_id": str(row["media_id"]),
            "idempotency_key": str(row["idempotency_key"]),
            "kind": str(row["kind"]),
            "mime_type": str(row["mime_type"]),
            "size_bytes": int(row["size_bytes"]),
            "sha256": str(row["sha256"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def _validate_content(raw: Mapping[str, Any], content: bytes) -> None:
    try:
        size = int(raw["size_bytes"])
        digest = str(raw["sha256"])
        mime_type = str(raw["mime_type"])
    except (KeyError, TypeError, ValueError) as error:
        raise CloudShareSchemaError("legacy cloud share media metadata is invalid") from error
    if (
        not 1 <= size <= _MAX_MEDIA_BYTES
        or len(content) != size
        or not _DIGEST.fullmatch(digest)
        or hashlib.sha256(content).hexdigest() != digest
        or mime_type not in _MIME_TYPES
        or not _has_signature(content, mime_type)
    ):
        raise CloudShareSchemaError("legacy cloud share media integrity is invalid")


def _has_signature(content: bytes, mime_type: str) -> bool:
    if mime_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if mime_type == "image/webp":
        return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    if mime_type == "image/gif":
        return content.startswith((b"GIF87a", b"GIF89a"))
    if mime_type == "image/avif":
        return b"ftypavif" in content[:64] or b"ftypavis" in content[:64]
    return False


def _insert_object_metadata(
    connection: sqlite3.Connection,
    rows: Sequence[_PreparedMedia],
) -> None:
    by_digest: dict[str, list[_PreparedMedia]] = {}
    for row in rows:
        by_digest.setdefault(row.sha256, []).append(row)
    connection.executemany(
        "INSERT INTO cloud_share_objects("
        "object_key,sha256,size_bytes,mime_type,etag,ref_count,state,created_at,"
        "last_accessed_at,access_count) VALUES(?,?,?,?,?,?,'active',?,NULL,0)",
        [
            (
                group[0].object_key,
                digest,
                group[0].size_bytes,
                group[0].mime_type,
                group[0].etag,
                len(group),
                min(row.created_at for row in group),
            )
            for digest, group in sorted(by_digest.items())
        ],
    )


def _insert_schema_receipt(
    connection: sqlite3.Connection,
    media_receipt: CloudShareMediaMigrationReceipt,
) -> None:
    receipt = CloudShareSchemaReceipt(
        schema_version=CLOUD_SHARE_SCHEMA_RECEIPT_VERSION,
        migration_version=CURRENT_CLOUD_SHARE_SCHEMA_VERSION,
        migration_name=MIGRATION_001_NAME,
        migration_checksum=MIGRATION_001_CHECKSUM,
        source_schema_sha256=LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256,
        target_schema_sha256=CLOUD_SHARE_SCHEMA_SHA256,
        transformed_rows=media_receipt.migrated_rows,
        installed_at=media_receipt.installed_at,
    )
    encoded = _canonical(receipt.to_dict()).decode("utf-8")
    connection.execute(
        "INSERT INTO cloud_share_schema_migrations("
        "version,migration_name,migration_checksum,source_schema_sha256,"
        "target_schema_sha256,transformed_rows,receipt_json,receipt_sha256,installed_at"
        ") VALUES(?,?,?,?,?,?,?,?,?)",
        (
            receipt.migration_version,
            receipt.migration_name,
            receipt.migration_checksum,
            receipt.source_schema_sha256,
            receipt.target_schema_sha256,
            receipt.transformed_rows,
            encoded,
            _digest(encoded.encode("utf-8")),
            receipt.installed_at,
        ),
    )


def _insert_media_receipt(
    connection: sqlite3.Connection,
    receipt: CloudShareMediaMigrationReceipt,
) -> None:
    encoded = _canonical(receipt.to_dict()).decode("utf-8")
    connection.execute(
        "INSERT INTO cloud_share_media_migrations("
        "version,migration_checksum,receipt_json,receipt_sha256,installed_at"
        ") VALUES(?,?,?,?,?)",
        (
            receipt.migration_version,
            receipt.migration_checksum,
            encoded,
            _digest(encoded.encode("utf-8")),
            receipt.installed_at,
        ),
    )


def _create_missing_target_objects(connection: sqlite3.Connection) -> None:
    existing = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE name LIKE 'cloud_share_%'"
        )
    }
    pending = ""
    for line in CLOUD_SHARE_SCHEMA_SQL.splitlines(keepends=True):
        pending += line
        if not sqlite3.complete_statement(pending):
            continue
        statement = pending.strip()
        pending = ""
        if not statement:
            continue
        match = re.match(r"CREATE\s+(?:TABLE|TRIGGER|INDEX)\s+([A-Za-z0-9_]+)", statement)
        if match is None:
            raise CloudShareSchemaError("cloud share target schema SQL is invalid")
        if match.group(1) not in existing:
            connection.execute(statement)
            existing.add(match.group(1))
    if pending.strip():
        raise CloudShareSchemaError("cloud share target schema SQL is incomplete")


def _existing_receipt(database: Path) -> CloudShareMediaMigrationReceipt | None:
    connection = _connect(database, read_only=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        digest = _schema_digest(connection)
        if digest == LEGACY_BLOB_CLOUD_SHARE_SCHEMA_SHA256:
            connection.commit()
            return None
        if digest != CLOUD_SHARE_SCHEMA_SHA256:
            raise CloudShareSchemaError("cloud share media source shape is unknown")
        CloudShareSchemaManager(database, keyring=_UnusedKeyring())._validate_connection(
            connection
        )
        receipt = validate_cloud_share_media_history_connection(connection)
        if receipt is None:
            raise CloudShareSchemaError("current cloud share schema has no legacy media receipt")
        connection.commit()
        return receipt
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _write_checkpoint(
    path: Path,
    checkpoint: CloudShareMediaMigrationCheckpoint,
) -> CloudShareMediaMigrationCheckpoint:
    if os.path.lexists(path):
        return _read_checkpoint(path)
    payload = checkpoint.to_dict()
    envelope = {
        "checkpoint": payload,
        "checkpoint_sha256": _digest(_canonical(payload)),
    }
    data = _canonical(envelope)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".ecorex-share-media-",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return _read_checkpoint(path)
        except OSError:
            # Windows can report a concurrent link winner as access denied.
            if os.path.lexists(path):
                return _read_checkpoint(path)
            raise
        _fsync_directory(path.parent)
    except OSError as error:
        raise CloudShareSchemaError("cloud share media checkpoint could not be persisted") from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return checkpoint


def _read_checkpoint(path: Path) -> CloudShareMediaMigrationCheckpoint:
    metadata = _require_regular(path)
    if metadata.st_size > _MAX_CHECKPOINT_BYTES:
        raise CloudShareSchemaError("cloud share media checkpoint exceeds its size limit")
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            opened = os.fstat(handle.fileno())
            current = _require_regular(path)
            if (
                (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino)
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                or opened.st_size > _MAX_CHECKPOINT_BYTES
            ):
                raise CloudShareSchemaError("cloud share media checkpoint identity changed")
            encoded = handle.read(_MAX_CHECKPOINT_BYTES + 1)
        if len(encoded) > _MAX_CHECKPOINT_BYTES:
            raise CloudShareSchemaError("cloud share media checkpoint exceeds its size limit")
        raw = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CloudShareSchemaError("cloud share media checkpoint is invalid") from error
    if not isinstance(raw, Mapping) or set(raw) != {"checkpoint", "checkpoint_sha256"}:
        raise CloudShareSchemaError("cloud share media checkpoint is invalid")
    payload = raw["checkpoint"]
    if (
        not isinstance(payload, Mapping)
        or str(raw["checkpoint_sha256"]) != _digest(_canonical(payload))
        or set(payload)
        != {
            "checkpoint_version",
            "migration_checksum",
            "database_identity_sha256",
            "source_schema_sha256",
            "source_data_sha256",
            "prepared_rows",
            "total_bytes",
            "prepared_at",
        }
    ):
        raise CloudShareSchemaError("cloud share media checkpoint is invalid")
    prepared_raw = payload["prepared_rows"]
    if not isinstance(prepared_raw, list) or len(prepared_raw) > _ABSOLUTE_MAX_ROWS:
        raise CloudShareSchemaError("cloud share media checkpoint is invalid")
    expected_keys = set(_PreparedMedia.__dataclass_fields__)
    prepared: list[_PreparedMedia] = []
    for item in prepared_raw:
        if not isinstance(item, Mapping) or set(item) != expected_keys:
            raise CloudShareSchemaError("cloud share media checkpoint is invalid")
        prepared.append(_PreparedMedia(**dict(item)))
    checkpoint = CloudShareMediaMigrationCheckpoint(
        **{key: value for key, value in payload.items() if key != "prepared_rows"},
        prepared_rows=tuple(prepared),
    )
    if _canonical(raw) != encoded:
        raise CloudShareSchemaError("cloud share media checkpoint is non-canonical")
    return checkpoint


def _remove_checkpoint(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise CloudShareSchemaError("cloud share media checkpoint cleanup failed") from error
    _require_regular(path)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        raise CloudShareSchemaError("cloud share media checkpoint cleanup failed") from error


def _database_path(path: str | Path) -> Path:
    database = Path(os.path.abspath(Path(path).expanduser()))
    _require_regular(database)
    return database


def _checkpoint_path(database: Path, supplied: str | Path | None) -> Path:
    path = (
        database.with_name(database.name + ".share-media-v1.checkpoint.json")
        if supplied is None
        else Path(os.path.abspath(Path(supplied).expanduser()))
    )
    if path.parent != database.parent:
        raise CloudShareSchemaError("cloud share media checkpoint must be beside its database")
    return path


def _database_identity(database: Path) -> str:
    metadata = database.stat()
    return _digest(
        _canonical(
            {
                "path": os.path.normcase(str(database)),
                "device": int(metadata.st_dev),
                "inode": int(metadata.st_ino),
            }
        )
    )


def _require_regular(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CloudShareSchemaError("cloud share media database or checkpoint is unavailable") from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
    ):
        raise CloudShareSchemaError("cloud share media database or checkpoint is unsafe")
    return metadata


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_limits(max_rows: int, max_total_bytes: int) -> None:
    if (
        not isinstance(max_rows, int)
        or isinstance(max_rows, bool)
        or not 0 <= max_rows <= _ABSOLUTE_MAX_ROWS
        or not isinstance(max_total_bytes, int)
        or isinstance(max_total_bytes, bool)
        or not 0 <= max_total_bytes <= _ABSOLUTE_MAX_TOTAL_BYTES
    ):
        raise ValueError("cloud share media migration limits are invalid")


def _connect(database: Path, *, read_only: bool) -> sqlite3.Connection:
    mode = "ro" if read_only else "rw"
    try:
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode={mode}&nofollow=1",
            uri=True,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
    except sqlite3.Error as error:
        raise CloudShareSchemaError("cloud share media database is unavailable") from error
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA recursive_triggers=ON")
    if not read_only:
        connection.execute("PRAGMA synchronous=FULL")
    return connection


def _activate_wal(connection: sqlite3.Connection) -> None:
    mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
    if mode is None or str(mode[0]).casefold() != "wal":
        raise CloudShareSchemaError("cloud share media migration WAL activation failed")


class _UnusedKeyring:
    active_key_id = "migration-validation"
    legacy_key_id = None
    keys = {"migration-validation": b"\0" * 32}


__all__ = [
    "CLOUD_SHARE_MEDIA_MIGRATION_CHECKSUM",
    "CLOUD_SHARE_MEDIA_MIGRATION_NAME",
    "CURRENT_CLOUD_SHARE_MEDIA_MIGRATION_VERSION",
    "CloudShareMediaMigrationCheckpoint",
    "CloudShareMediaMigrationReceipt",
    "finalize_cloud_share_media_objects",
    "migrate_cloud_share_media_objects",
    "prepare_cloud_share_media_objects",
]
