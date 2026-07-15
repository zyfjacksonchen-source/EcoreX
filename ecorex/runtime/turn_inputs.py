"""Append-only Turn intent revisions and immutable execution batch bindings."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from pathlib import Path
import sqlite3
from typing import Any

from ecorex.protocol import (
    CreateTurnRequest,
    SteerTurnRequest,
    TurnExecutionBatch,
    TurnInputRevision,
)

from .database import SQLiteDatabase, json_dumps, json_loads
from .errors import ConflictError, NotFoundError
from .ids import new_id
from .snapshots import TurnSnapshotContext


TurnIntentRequest = CreateTurnRequest | SteerTurnRequest


def _store_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _read_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _intent_payload(request: TurnIntentRequest) -> dict[str, Any]:
    """Return the complete, canonical user intent covered by idempotency."""

    return {
        "schema_version": 1,
        "input": request.input,
        "agent_model_id": request.agent_model_id,
        "image_model_id": request.image_model_id,
        "explicit_tool_ids": list(request.explicit_tool_ids),
        "metadata": request.metadata,
    }


def intent_fingerprint(request: TurnIntentRequest) -> str:
    payload = json_dumps(_intent_payload(request)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class TurnInputRevisionRepository:
    """Durable authority for initial and steered Turn input."""

    def __init__(self, database: SQLiteDatabase | str | Path) -> None:
        self.database = (
            database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        )

    def append_initial_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        thread_id: str,
        turn_id: str,
        request: CreateTurnRequest,
        created_at: datetime | None = None,
    ) -> tuple[TurnInputRevision, bool]:
        if not connection.in_transaction:
            raise RuntimeError("input revision append requires an active transaction")
        duplicate = self.match_client_intent_in_transaction(
            connection,
            thread_id=thread_id,
            turn_id=turn_id,
            request=request,
        )
        if duplicate is not None:
            return duplicate, False
        latest = connection.execute(
            "SELECT MAX(ordinal) AS ordinal FROM turn_input_revisions WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()["ordinal"]
        if latest is not None:
            raise ConflictError("Turn already has an initial input revision")
        return (
            self._insert_in_transaction(
                connection,
                thread_id=thread_id,
                turn_id=turn_id,
                ordinal=0,
                source="initial",
                request=request,
                created_at=created_at,
            ),
            True,
        )

    def append_steer_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        thread_id: str,
        turn_id: str,
        request: SteerTurnRequest,
        created_at: datetime | None = None,
    ) -> tuple[TurnInputRevision, bool]:
        if not connection.in_transaction:
            raise RuntimeError("input revision append requires an active transaction")
        duplicate = self.match_client_intent_in_transaction(
            connection,
            thread_id=thread_id,
            turn_id=turn_id,
            request=request,
        )
        if duplicate is not None:
            return duplicate, False
        latest = connection.execute(
            "SELECT MAX(ordinal) AS ordinal FROM turn_input_revisions WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()["ordinal"]
        if latest is None:
            raise ConflictError("Turn is missing its initial input revision")
        return (
            self._insert_in_transaction(
                connection,
                thread_id=thread_id,
                turn_id=turn_id,
                ordinal=int(latest) + 1,
                source="steer",
                request=request,
                created_at=created_at,
            ),
            True,
        )

    def append_authority_refresh_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        thread_id: str,
        turn_id: str,
        request: SteerTurnRequest,
        created_at: datetime | None = None,
    ) -> tuple[TurnInputRevision, bool]:
        """Append an internal, non-UI revision that forces fresh authority.

        The revision is not a user message.  Its short input is delivered only
        to the model continuation so it understands why a new immutable batch
        was created after connector login.
        """

        if not connection.in_transaction:
            raise RuntimeError("input revision append requires an active transaction")
        duplicate = self.match_client_intent_in_transaction(
            connection,
            thread_id=thread_id,
            turn_id=turn_id,
            request=request,
        )
        if duplicate is not None:
            if duplicate.source != "authority_refresh":
                raise ConflictError(
                    "authority refresh identity belongs to a user revision"
                )
            return duplicate, False
        latest = connection.execute(
            "SELECT MAX(ordinal) AS ordinal FROM turn_input_revisions WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()["ordinal"]
        if latest is None:
            raise ConflictError("Turn is missing its initial input revision")
        return (
            self._insert_in_transaction(
                connection,
                thread_id=thread_id,
                turn_id=turn_id,
                ordinal=int(latest) + 1,
                source="authority_refresh",
                request=request,
                created_at=created_at,
            ),
            True,
        )

    def match_client_intent_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        thread_id: str,
        turn_id: str,
        request: TurnIntentRequest,
    ) -> TurnInputRevision | None:
        """Return an idempotent match, or reject reuse with different intent."""

        if not connection.in_transaction:
            raise RuntimeError("input revision lookup requires an active transaction")
        if not request.client_message_id:
            return None
        row = connection.execute(
            "SELECT * FROM turn_input_revisions "
            "WHERE thread_id = ? AND client_message_id = ?",
            (thread_id, request.client_message_id),
        ).fetchone()
        if row is None:
            return None
        if row["turn_id"] != turn_id:
            raise ConflictError("client_message_id belongs to a different Turn")
        if row["intent_fingerprint"] != intent_fingerprint(request):
            raise ConflictError(
                "client_message_id was reused with different Turn intent"
            )
        return self._from_revision_row(row)

    def _insert_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        thread_id: str,
        turn_id: str,
        ordinal: int,
        source: str,
        request: TurnIntentRequest,
        created_at: datetime | None,
    ) -> TurnInputRevision:
        revision_id = new_id("rev")
        timestamp = _store_time(created_at or datetime.now(UTC))
        fingerprint = intent_fingerprint(request)
        try:
            connection.execute(
                "INSERT INTO turn_input_revisions("
                "revision_id, thread_id, turn_id, ordinal, source, input_text, "
                "agent_model_id, image_model_id, explicit_tool_ids_json, metadata_json, "
                "client_message_id, intent_fingerprint, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    thread_id,
                    turn_id,
                    ordinal,
                    source,
                    request.input,
                    request.agent_model_id,
                    request.image_model_id,
                    json_dumps(list(request.explicit_tool_ids)),
                    json_dumps(request.metadata),
                    request.client_message_id,
                    fingerprint,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError("Turn input revision identity already exists") from error
        row = connection.execute(
            "SELECT * FROM turn_input_revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - SQLite acknowledged the insert.
            raise RuntimeError("Turn input revision was not persisted")
        return self._from_revision_row(row)

    def get(self, revision_id: str) -> TurnInputRevision:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM turn_input_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"input revision {revision_id!r} does not exist")
        return self._from_revision_row(row)

    def list_for_turn(self, turn_id: str) -> tuple[TurnInputRevision, ...]:
        with self.database.reader() as connection:
            return self.list_for_turn_in_transaction(connection, turn_id)

    def list_for_turn_in_transaction(
        self,
        connection: sqlite3.Connection,
        turn_id: str,
    ) -> tuple[TurnInputRevision, ...]:
        """Read one Turn's immutable input sequence from the caller's snapshot."""

        if not connection.in_transaction:
            raise RuntimeError("input revision lookup requires an active transaction")
        rows = connection.execute(
            "SELECT * FROM turn_input_revisions WHERE turn_id = ? ORDER BY ordinal",
            (turn_id,),
        ).fetchall()
        return tuple(self._from_revision_row(row) for row in rows)

    @staticmethod
    def _from_revision_row(row: sqlite3.Row) -> TurnInputRevision:
        return TurnInputRevision(
            revision_id=str(row["revision_id"]),
            thread_id=str(row["thread_id"]),
            turn_id=str(row["turn_id"]),
            ordinal=int(row["ordinal"]),
            source=str(row["source"]),
            input=str(row["input_text"]),
            agent_model_id=str(row["agent_model_id"]),
            image_model_id=row["image_model_id"],
            explicit_tool_ids=json_loads(row["explicit_tool_ids_json"], []),
            metadata=json_loads(row["metadata_json"], {}),
            client_message_id=row["client_message_id"],
            intent_fingerprint=str(row["intent_fingerprint"]),
            created_at=_read_time(str(row["created_at"])),
        )


class TurnExecutionBatchRepository:
    """Binds a contiguous revision range to one frozen Runtime context."""

    def __init__(self, database: SQLiteDatabase | str | Path) -> None:
        self.database = (
            database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        )

    def create(
        self,
        *,
        turn_id: str,
        first_revision_ordinal: int,
        last_revision_ordinal: int,
        snapshot_context: TurnSnapshotContext,
        batch_id: str | None = None,
        created_at: datetime | None = None,
    ) -> TurnExecutionBatch:
        with self.database.transaction() as connection:
            return self.create_in_transaction(
                connection,
                turn_id=turn_id,
                first_revision_ordinal=first_revision_ordinal,
                last_revision_ordinal=last_revision_ordinal,
                snapshot_context=snapshot_context,
                batch_id=batch_id,
                created_at=created_at,
            )

    def create_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        turn_id: str,
        first_revision_ordinal: int,
        last_revision_ordinal: int,
        snapshot_context: TurnSnapshotContext,
        batch_id: str | None = None,
        created_at: datetime | None = None,
    ) -> TurnExecutionBatch:
        if not connection.in_transaction:
            raise RuntimeError("execution batch creation requires an active transaction")
        if (
            not isinstance(first_revision_ordinal, int)
            or isinstance(first_revision_ordinal, bool)
            or not isinstance(last_revision_ordinal, int)
            or isinstance(last_revision_ordinal, bool)
            or first_revision_ordinal < 0
            or last_revision_ordinal < first_revision_ordinal
        ):
            raise ValueError("execution batch revision range is invalid")
        context = snapshot_context.to_dict()
        if any(not isinstance(value, str) or not value for value in context.values()):
            raise ValueError("execution batch snapshot context is incomplete")
        turn = connection.execute(
            "SELECT thread_id FROM turns WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        if turn is None:
            raise NotFoundError(f"turn {turn_id!r} does not exist")
        rows = connection.execute(
            "SELECT ordinal FROM turn_input_revisions "
            "WHERE turn_id = ? AND ordinal BETWEEN ? AND ? ORDER BY ordinal",
            (turn_id, first_revision_ordinal, last_revision_ordinal),
        ).fetchall()
        expected = list(range(first_revision_ordinal, last_revision_ordinal + 1))
        if [int(row["ordinal"]) for row in rows] != expected:
            raise ConflictError("execution batch revision range is not contiguous")
        identity_payload = {
            "schema_version": 1,
            "turn_id": turn_id,
            "first_revision_ordinal": first_revision_ordinal,
            "last_revision_ordinal": last_revision_ordinal,
            **context,
        }
        identity_sha256 = hashlib.sha256(
            json_dumps(identity_payload).encode("utf-8")
        ).hexdigest()
        existing = connection.execute(
            "SELECT * FROM turn_execution_batches WHERE identity_sha256 = ?",
            (identity_sha256,),
        ).fetchone()
        if existing is not None:
            if batch_id is not None and existing["batch_id"] != batch_id:
                raise ConflictError(
                    "execution batch identity already has a different batch_id"
                )
            return self._from_batch_row(existing)
        previous = connection.execute(
            "SELECT last_revision_ordinal FROM turn_execution_batches "
            "WHERE turn_id = ? ORDER BY last_revision_ordinal DESC LIMIT 1",
            (turn_id,),
        ).fetchone()
        expected_first = (
            0 if previous is None else int(previous["last_revision_ordinal"]) + 1
        )
        if first_revision_ordinal != expected_first:
            if first_revision_ordinal < expected_first:
                raise ConflictError("execution batch revision range overlaps authority")
            raise ConflictError(
                "execution batch revision range does not continue durable authority"
            )
        batch_id = batch_id or f"bat_{identity_sha256[:32]}"
        reused = connection.execute(
            "SELECT * FROM turn_execution_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if reused is not None:
            raise ConflictError("batch_id was reused with different execution identity")
        timestamp = _store_time(created_at or datetime.now(UTC))
        try:
            connection.execute(
                "INSERT INTO turn_execution_batches("
                "batch_id, thread_id, turn_id, first_revision_ordinal, "
                "last_revision_ordinal, config_snapshot_id, capability_snapshot_id, "
                "permission_snapshot_id, model_catalog_snapshot_id, extension_snapshot_id, "
                "identity_sha256, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    turn["thread_id"],
                    turn_id,
                    first_revision_ordinal,
                    last_revision_ordinal,
                    context["config_snapshot_id"],
                    context["capability_snapshot_id"],
                    context["permission_snapshot_id"],
                    context["model_catalog_snapshot_id"],
                    context["extension_snapshot_id"],
                    identity_sha256,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(
                "execution batch revision range conflicts with durable authority"
            ) from error
        row = connection.execute(
            "SELECT * FROM turn_execution_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if row is None:  # pragma: no cover - SQLite acknowledged the insert.
            raise RuntimeError("Turn execution batch was not persisted")
        return self._from_batch_row(row)

    def get(self, batch_id: str) -> TurnExecutionBatch:
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM turn_execution_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"execution batch {batch_id!r} does not exist")
        return self._from_batch_row(row)

    def list_for_turn(self, turn_id: str) -> tuple[TurnExecutionBatch, ...]:
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM turn_execution_batches WHERE turn_id = ? "
                "ORDER BY first_revision_ordinal, last_revision_ordinal, created_at",
                (turn_id,),
            ).fetchall()
        return tuple(self._from_batch_row(row) for row in rows)

    @staticmethod
    def _from_batch_row(row: sqlite3.Row) -> TurnExecutionBatch:
        return TurnExecutionBatch(
            batch_id=str(row["batch_id"]),
            thread_id=str(row["thread_id"]),
            turn_id=str(row["turn_id"]),
            first_revision_ordinal=int(row["first_revision_ordinal"]),
            last_revision_ordinal=int(row["last_revision_ordinal"]),
            config_snapshot_id=str(row["config_snapshot_id"]),
            capability_snapshot_id=str(row["capability_snapshot_id"]),
            permission_snapshot_id=str(row["permission_snapshot_id"]),
            model_catalog_snapshot_id=str(row["model_catalog_snapshot_id"]),
            extension_snapshot_id=str(row["extension_snapshot_id"]),
            identity_sha256=str(row["identity_sha256"]),
            created_at=_read_time(str(row["created_at"])),
        )


__all__ = [
    "TurnExecutionBatchRepository",
    "TurnInputRevisionRepository",
    "intent_fingerprint",
]
