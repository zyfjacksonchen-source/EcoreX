"""Durable SQLite projection and append-only facts for Extension Registry v1."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from ecorex.runtime.database import SQLiteDatabase, json_dumps, json_loads
from ecorex.runtime.schema_catalog import validate_product_schema

from .errors import ExtensionIntegrityError, ExtensionNotFound
from .models import ExtensionManifest, utc_now_iso


EXTENSION_STORAGE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ExtensionStateRecord:
    extension_id: str
    active_revision_id: str | None
    staged_revision_id: str | None
    prior_known_good_revision_id: str | None
    enabled: bool
    health: str
    revision: int
    consecutive_failures: int
    restart_attempts: int
    restart_window_started_at: str | None
    circuit_open_until: str | None
    negotiated_protocol_version: str | None
    catalog_digest: str | None
    last_error_code: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class ExtensionRequestRecord:
    client_request_id: str
    operation: str
    request_sha256: str
    response: Mapping[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class ExtensionEventRecord:
    seq: int
    event_id: str
    extension_id: str
    revision_id: str | None
    event_type: str
    payload: Mapping[str, Any]
    client_request_id: str
    request_sha256: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ExtensionSignatureEvidenceRecord:
    evidence_id: str
    revision_id: str
    manifest_sha256: str
    signature_key_id: str
    signature_sha256: str
    signature: Mapping[str, Any]
    verified_at: str


class SQLiteExtensionRepository:
    """One transactional source of truth shared with the Runtime WAL database."""

    def __init__(
        self,
        database: SQLiteDatabase | str | Path,
        *,
        initialize: bool = True,
    ) -> None:
        self.database = (
            database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        )
        if initialize:
            self.initialize()
        else:
            self.validate()

    def validate(self) -> None:
        """Validate present Extension metadata without creating any facts."""

        with self.database.reader() as connection:
            validate_product_schema(connection)
            row = connection.execute(
                "SELECT value FROM extension_meta WHERE key = 'storage_schema_version'"
            ).fetchone()
        if row is not None and row["value"] != str(EXTENSION_STORAGE_SCHEMA_VERSION):
            raise ExtensionIntegrityError("extension storage schema is incompatible")

    def initialize(self) -> None:
        """Persist or verify Extension version metadata during convergence."""

        # Product DDL is compiled centrally. Extension startup may initialize
        # its version fact only after the complete schema validates exactly.
        with self.database.reader() as connection:
            validate_product_schema(connection)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT value FROM extension_meta WHERE key = 'storage_schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO extension_meta(key, value) VALUES ('storage_schema_version', ?)",
                    (str(EXTENSION_STORAGE_SCHEMA_VERSION),),
                )
            elif row["value"] != str(EXTENSION_STORAGE_SCHEMA_VERSION):
                raise ExtensionIntegrityError("extension storage schema is incompatible")

    def converge_startup(self) -> None:
        """Alias used by the healthy startup convergence coordinator."""

        self.initialize()

    def state(self, extension_id: str, *, connection: sqlite3.Connection | None = None) -> ExtensionStateRecord | None:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM extension_states WHERE extension_id = ?", (extension_id,)
            ).fetchone()
            return self._state(row) if row is not None else None
        with self.database.reader() as reader:
            row = reader.execute(
                "SELECT * FROM extension_states WHERE extension_id = ?", (extension_id,)
            ).fetchone()
        return self._state(row) if row is not None else None

    def require_state(self, extension_id: str, *, connection: sqlite3.Connection | None = None) -> ExtensionStateRecord:
        result = self.state(extension_id, connection=connection)
        if result is None:
            raise ExtensionNotFound(f"unknown extension: {extension_id!r}")
        return result

    def states(self) -> tuple[ExtensionStateRecord, ...]:
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM extension_states ORDER BY extension_id"
            ).fetchall()
        return tuple(self._state(row) for row in rows)

    def manifest(self, revision_id: str, *, connection: sqlite3.Connection | None = None) -> ExtensionManifest:
        if connection is not None:
            row = connection.execute(
                "SELECT manifest_json, manifest_sha256 FROM extension_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            return self._manifest(row, revision_id)
        with self.database.reader() as reader:
            row = reader.execute(
                "SELECT manifest_json, manifest_sha256 FROM extension_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
        return self._manifest(row, revision_id)

    def revisions(self, extension_id: str) -> tuple[ExtensionManifest, ...]:
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT manifest_json, manifest_sha256, revision_id FROM extension_revisions "
                "WHERE extension_id = ? ORDER BY installed_at, revision_id",
                (extension_id,),
            ).fetchall()
        return tuple(self._manifest(row, str(row["revision_id"])) for row in rows)

    def signature_evidence(
        self,
        revision_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[ExtensionSignatureEvidenceRecord, ...]:
        sql = (
            "SELECT * FROM extension_signature_evidence WHERE revision_id = ? "
            "ORDER BY verified_at, evidence_id"
        )
        if connection is not None:
            rows = connection.execute(sql, (revision_id,)).fetchall()
        else:
            with self.database.reader() as reader:
                rows = reader.execute(sql, (revision_id,)).fetchall()
        result: list[ExtensionSignatureEvidenceRecord] = []
        for row in rows:
            evidence_json = str(row["evidence_json"])
            signature_sha256 = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
            if signature_sha256 != row["signature_sha256"]:
                raise ExtensionIntegrityError("stored extension signature evidence digest is invalid")
            try:
                signature = json_loads(evidence_json, {})
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ExtensionIntegrityError("stored extension signature evidence is invalid") from error
            if not isinstance(signature, Mapping):
                raise ExtensionIntegrityError("stored extension signature evidence is not an object")
            result.append(
                ExtensionSignatureEvidenceRecord(
                    evidence_id=str(row["evidence_id"]),
                    revision_id=str(row["revision_id"]),
                    manifest_sha256=str(row["manifest_sha256"]),
                    signature_key_id=str(row["signature_key_id"]),
                    signature_sha256=str(row["signature_sha256"]),
                    signature=dict(signature),
                    verified_at=str(row["verified_at"]),
                )
            )
        return tuple(result)

    def is_quarantined(self, revision_id: str, *, connection: sqlite3.Connection | None = None) -> bool:
        sql = "SELECT 1 FROM extension_quarantines WHERE revision_id = ?"
        if connection is not None:
            return connection.execute(sql, (revision_id,)).fetchone() is not None
        with self.database.reader() as reader:
            return reader.execute(sql, (revision_id,)).fetchone() is not None

    def request(self, client_request_id: str, *, connection: sqlite3.Connection | None = None) -> ExtensionRequestRecord | None:
        sql = "SELECT * FROM extension_requests WHERE client_request_id = ?"
        if connection is not None:
            row = connection.execute(sql, (client_request_id,)).fetchone()
        else:
            with self.database.reader() as reader:
                row = reader.execute(sql, (client_request_id,)).fetchone()
        if row is None:
            return None
        try:
            response = json_loads(row["response_json"], {})
        except (TypeError, json.JSONDecodeError) as error:
            raise ExtensionIntegrityError("stored extension request is invalid") from error
        if not isinstance(response, Mapping):
            raise ExtensionIntegrityError("stored extension request response is invalid")
        return ExtensionRequestRecord(
            client_request_id=str(row["client_request_id"]),
            operation=str(row["operation"]),
            request_sha256=str(row["request_sha256"]),
            response=dict(response),
            created_at=str(row["created_at"]),
        )

    def append_event(
        self,
        connection: sqlite3.Connection,
        *,
        event_id: str,
        extension_id: str,
        revision_id: str | None,
        event_type: str,
        payload: Mapping[str, Any],
        client_request_id: str,
        request_sha256: str,
        created_at: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO extension_events(event_id, extension_id, revision_id, event_type, "
            "payload_json, client_request_id, request_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                extension_id,
                revision_id,
                event_type,
                json_dumps(dict(payload)),
                client_request_id,
                request_sha256,
                created_at or utc_now_iso(),
            ),
        )

    def save_request(
        self,
        connection: sqlite3.Connection,
        *,
        client_request_id: str,
        operation: str,
        request_sha256: str,
        response: Mapping[str, Any],
        created_at: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO extension_requests(client_request_id, operation, request_sha256, response_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                client_request_id,
                operation,
                request_sha256,
                json_dumps(dict(response)),
                created_at or utc_now_iso(),
            ),
        )

    def save_snapshot(
        self,
        payload: Mapping[str, Any],
        *,
        prefix: str = "ext",
    ) -> tuple[str, str]:
        if prefix not in {"ext", "extcontrib"}:
            raise ValueError("extension snapshot prefix is invalid")
        payload_json = json_dumps(dict(payload))
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        snapshot_id = prefix + "_" + digest
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json, payload_sha256 FROM extension_catalog_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO extension_catalog_snapshots(snapshot_id, payload_json, payload_sha256, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (snapshot_id, payload_json, digest, utc_now_iso()),
                )
            elif row["payload_json"] != payload_json or row["payload_sha256"] != digest:
                raise ExtensionIntegrityError("extension snapshot identity was reused")
        return snapshot_id, digest

    def snapshot_payload(self, snapshot_id: str) -> Mapping[str, Any]:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT payload_json, payload_sha256 FROM extension_catalog_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            raise ExtensionNotFound(f"unknown extension snapshot: {snapshot_id!r}")
        payload_json = str(row["payload_json"])
        if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != row["payload_sha256"]:
            raise ExtensionIntegrityError("stored extension snapshot digest is invalid")
        payload = json_loads(payload_json, {})
        if not isinstance(payload, Mapping):
            raise ExtensionIntegrityError("stored extension snapshot is invalid")
        return dict(payload)

    def events(self, *, after_seq: int = 0, limit: int = 1000) -> tuple[ExtensionEventRecord, ...]:
        if (
            isinstance(after_seq, bool)
            or isinstance(limit, bool)
            or not isinstance(after_seq, int)
            or not isinstance(limit, int)
            or after_seq < 0
            or not 1 <= limit <= 5000
        ):
            raise ValueError("extension event cursor or limit is invalid")
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM extension_events WHERE seq > ? ORDER BY seq LIMIT ?",
                (after_seq, limit),
            ).fetchall()
        result: list[ExtensionEventRecord] = []
        for row in rows:
            payload = json_loads(row["payload_json"], {})
            if not isinstance(payload, Mapping):
                raise ExtensionIntegrityError("stored extension event is invalid")
            result.append(
                ExtensionEventRecord(
                    seq=int(row["seq"]),
                    event_id=str(row["event_id"]),
                    extension_id=str(row["extension_id"]),
                    revision_id=str(row["revision_id"]) if row["revision_id"] else None,
                    event_type=str(row["event_type"]),
                    payload=dict(payload),
                    client_request_id=str(row["client_request_id"]),
                    request_sha256=str(row["request_sha256"]),
                    created_at=str(row["created_at"]),
                )
            )
        return tuple(result)

    def generation(self, *, connection: sqlite3.Connection | None = None) -> int:
        """Return the durable Extension mutation generation.

        The append-only event sequence already advances for every committed
        lifecycle change, so it is the authority instead of a second counter.
        """

        if connection is not None:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS generation FROM extension_events"
            ).fetchone()
        else:
            with self.database.reader() as reader:
                row = reader.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS generation FROM extension_events"
                ).fetchone()
        return int(row["generation"])

    @staticmethod
    def _state(row: sqlite3.Row) -> ExtensionStateRecord:
        return ExtensionStateRecord(
            extension_id=str(row["extension_id"]),
            active_revision_id=str(row["active_revision_id"]) if row["active_revision_id"] else None,
            staged_revision_id=str(row["staged_revision_id"]) if row["staged_revision_id"] else None,
            prior_known_good_revision_id=(
                str(row["prior_known_good_revision_id"])
                if row["prior_known_good_revision_id"] else None
            ),
            enabled=bool(row["enabled"]),
            health=str(row["health"]),
            revision=int(row["revision"]),
            consecutive_failures=int(row["consecutive_failures"]),
            restart_attempts=int(row["restart_attempts"]),
            restart_window_started_at=(
                str(row["restart_window_started_at"])
                if row["restart_window_started_at"] else None
            ),
            circuit_open_until=str(row["circuit_open_until"]) if row["circuit_open_until"] else None,
            negotiated_protocol_version=(
                str(row["negotiated_protocol_version"])
                if row["negotiated_protocol_version"] else None
            ),
            catalog_digest=str(row["catalog_digest"]) if row["catalog_digest"] else None,
            last_error_code=str(row["last_error_code"]) if row["last_error_code"] else None,
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _manifest(row: sqlite3.Row | None, revision_id: str) -> ExtensionManifest:
        if row is None:
            raise ExtensionNotFound(f"unknown extension revision: {revision_id!r}")
        payload = str(row["manifest_json"]).encode("utf-8")
        if hashlib.sha256(payload).hexdigest() != row["manifest_sha256"]:
            raise ExtensionIntegrityError("stored extension manifest digest is invalid")
        manifest = ExtensionManifest.from_bytes(payload)
        if manifest.revision_id != revision_id:
            raise ExtensionIntegrityError("stored extension revision identity is invalid")
        return manifest


__all__ = [
    "EXTENSION_STORAGE_SCHEMA_VERSION",
    "ExtensionEventRecord",
    "ExtensionRequestRecord",
    "ExtensionSignatureEvidenceRecord",
    "ExtensionStateRecord",
    "SQLiteExtensionRepository",
]
