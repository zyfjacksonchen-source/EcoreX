"""Transactional Thread / Turn / Item application service."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from ecorex.protocol import (
    ITEM_TRANSITIONS,
    JOB_TRANSITIONS,
    TERMINAL_JOB_STATUSES,
    TERMINAL_TURN_STATUSES,
    TURN_TRANSITIONS,
    CreateThreadRequest,
    CreateTurnRequest,
    DurableJob,
    EventEnvelope,
    ForkThreadRequest,
    InteractionContract,
    InteractionKind,
    InteractionListResponse,
    InteractionMutationResponse,
    InteractionProjection,
    InteractionResponse,
    InteractionStatus,
    ItemKind,
    ItemProjection,
    ItemStatus,
    JobStatus,
    PublicToolActivity,
    ReplaceTurnRequest,
    ReplaceTurnResponse,
    SteerTurnRequest,
    ThreadProjection,
    ThreadProjectionResponse,
    ThreadStatus,
    TurnMutationResponse,
    TurnProjection,
    TurnStatus,
)

from .database import SQLiteDatabase, json_dumps, json_loads
from .errors import ConflictError, InvalidTransitionError, NotFoundError
from .event_store import EventStore
from .ids import new_id
from .interactions import InteractionStore
from .invariants import RuntimeInvariantAuditor
from .jobs import DurableJobStore
from .public_tools import PublicToolActivityProjector
from .reasoning import ReasoningItemStore, archive_visible_reasoning_in_transaction
from .snapshots import RuntimeSnapshotRepository, TurnSnapshotContext
from .turn_inputs import TurnExecutionBatchRepository, TurnInputRevisionRepository


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _store_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _default_thread_title(value: str) -> str:
    normalized = " ".join(value.split()).strip()
    if not normalized:
        return "新任务"
    return normalized if len(normalized) <= 60 else normalized[:59].rstrip() + "…"


def _read_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class RuntimeKernel:
    """The sole writer for conversational state projections."""

    def __init__(self, database: SQLiteDatabase | str):
        self.database = (
            database if isinstance(database, SQLiteDatabase) else SQLiteDatabase(database)
        )
        self.events = EventStore(self.database)
        self.jobs = DurableJobStore(self.database, self.events)
        self.reasoning = ReasoningItemStore(self.database, self.events)
        self.interactions = InteractionStore(self.database, self.events)
        self.snapshots = RuntimeSnapshotRepository(self.database)
        self.turn_inputs = TurnInputRevisionRepository(self.database)
        self.turn_execution_batches = TurnExecutionBatchRepository(self.database)
        self.invariants = RuntimeInvariantAuditor(self.database)

    def _mutation_transaction(
        self,
        *,
        scope: str,
        subject: str,
        job_id: str | None = None,
        lease_token: str | None = None,
    ):
        """Select the only valid transaction boundary for one Runtime write."""

        if (job_id is None) != (lease_token is None):
            raise ValueError("job_id and lease_token must be supplied together")
        if job_id is not None and lease_token is not None:
            return self.jobs.execution_transaction(job_id, lease_token)
        return self.jobs.control_transaction(scope=scope, subject=subject)

    @staticmethod
    def _thread_from_row(row: sqlite3.Row) -> ThreadProjection:
        return ThreadProjection(
            thread_id=row["thread_id"],
            status=ThreadStatus(row["status"]),
            title=row["title"],
            metadata=json_loads(row["metadata_json"], {}),
            forked_from_thread_id=row["forked_from_thread_id"],
            forked_from_turn_id=row["forked_from_turn_id"],
            forked_from_seq=row["forked_from_seq"],
            created_at=_read_time(row["created_at"]),
            updated_at=_read_time(row["updated_at"]),
        )

    @staticmethod
    def _turn_from_row(row: sqlite3.Row) -> TurnProjection:
        return TurnProjection(
            turn_id=row["turn_id"],
            thread_id=row["thread_id"],
            status=TurnStatus(row["status"]),
            input=row["input_text"],
            agent_model_id=row["agent_model_id"],
            image_model_id=row["image_model_id"],
            client_message_id=row["client_message_id"],
            metadata=json_loads(row["metadata_json"], {}),
            terminal_reason=row["terminal_reason"],
            created_at=_read_time(row["created_at"]),
            updated_at=_read_time(row["updated_at"]),
        )

    @staticmethod
    def _item_from_row(row: sqlite3.Row) -> ItemProjection:
        kind = ItemKind(row["kind"])
        content = json_loads(row["content_json"], {})
        if kind is ItemKind.TOOL_CALL:
            try:
                content = PublicToolActivity.model_validate(content).model_dump(
                    mode="json"
                )
            except ValueError:
                raise ConflictError(
                    "Tool Item public activity is invalid"
                ) from None
        return ItemProjection(
            item_id=row["item_id"],
            thread_id=row["thread_id"],
            turn_id=row["turn_id"],
            kind=kind,
            status=ItemStatus(row["status"]),
            content=content,
            created_at=_read_time(row["created_at"]),
            updated_at=_read_time(row["updated_at"]),
        )

    @staticmethod
    def _require_thread(
        connection: sqlite3.Connection, thread_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"thread {thread_id!r} does not exist")
        return row

    @staticmethod
    def _require_turn(connection: sqlite3.Connection, turn_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM turns WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"turn {turn_id!r} does not exist")
        return row

    @staticmethod
    def _require_item(connection: sqlite3.Connection, item_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM items WHERE item_id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"item {item_id!r} does not exist")
        return row

    @staticmethod
    def _assert_turn_snapshot_context(
        connection: sqlite3.Connection,
        turn_id: str,
        snapshot_context: TurnSnapshotContext | None,
    ) -> None:
        if snapshot_context is None:
            return
        accepted = connection.execute(
            "SELECT config_snapshot_id, capability_snapshot_id, "
            "permission_snapshot_id, extension_snapshot_id, payload_json FROM events "
            "WHERE turn_id = ? AND event_type = 'turn.accepted' "
            "ORDER BY seq LIMIT 1",
            (turn_id,),
        ).fetchone()
        if accepted is None:
            raise ConflictError("idempotent turn is missing its acceptance event")
        payload = json_loads(accepted["payload_json"], {})
        if (
            accepted["config_snapshot_id"] != snapshot_context.config_snapshot_id
            or accepted["capability_snapshot_id"]
            != snapshot_context.capability_snapshot_id
            or accepted["permission_snapshot_id"]
            != snapshot_context.permission_snapshot_id
            or accepted["extension_snapshot_id"]
            != snapshot_context.extension_snapshot_id
            or payload.get("model_catalog_snapshot_id")
            != snapshot_context.model_catalog_snapshot_id
        ):
            raise ConflictError(
                "client_message_id was reused under a different Runtime snapshot"
            )

    def create_thread(
        self, request: CreateThreadRequest | None = None
    ) -> ThreadProjection:
        request = request or CreateThreadRequest()
        now = _utc_now()
        thread_id = new_id("thr")
        request_fingerprint = hashlib.sha256(
            json_dumps(
                {"title": request.title, "metadata": request.metadata}
            ).encode("utf-8")
        ).hexdigest()
        with self.jobs.control_transaction(
            scope="thread_create",
            subject=request.client_request_id or thread_id,
        ) as connection:
            if request.client_request_id:
                duplicate = connection.execute(
                    "SELECT * FROM threads WHERE client_request_id = ?",
                    (request.client_request_id,),
                ).fetchone()
                if duplicate is not None:
                    if duplicate["request_fingerprint"] != request_fingerprint:
                        raise ConflictError(
                            "client_request_id was reused with different thread input"
                        )
                    return self._thread_from_row(duplicate)
            self.events.append_in_transaction(
                connection,
                thread_id=thread_id,
                event_type="thread.created",
                payload={"title": request.title, "metadata": request.metadata},
                correlation_id=request.client_request_id,
                idempotency_key="thread:created",
                created_at=now,
            )
            timestamp = _store_time(now)
            connection.execute(
                """
                INSERT INTO threads(
                    thread_id, status, title, metadata_json,
                    client_request_id, request_fingerprint,
                    forked_from_thread_id, forked_from_turn_id,
                    forked_from_seq, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    thread_id,
                    ThreadStatus.ACTIVE.value,
                    request.title,
                    json_dumps(request.metadata),
                    request.client_request_id,
                    request_fingerprint,
                    timestamp,
                    timestamp,
                ),
            )
            row = self._require_thread(connection, thread_id)
            return self._thread_from_row(row)

    def get_thread(self, thread_id: str) -> ThreadProjection:
        with self.database.reader() as connection:
            row = self._require_thread(connection, thread_id)
            return self._thread_from_row(row)

    def list_threads(
        self,
        *,
        status: ThreadStatus | None = ThreadStatus.ACTIVE,
        limit: int = 50,
        before_updated_at: datetime | None = None,
        before_thread_id: str | None = None,
    ) -> tuple[list[ThreadProjection], bool]:
        if not 1 <= limit <= 200:
            raise ValueError("thread list limit must be between one and 200")
        if (before_updated_at is None) != (before_thread_id is None):
            raise ValueError("thread list cursor is incomplete")
        with self.database.reader() as connection:
            conditions: list[str] = []
            parameters: list[Any] = []
            if status is not None:
                conditions.append("status = ?")
                parameters.append(status.value)
            if before_updated_at is not None and before_thread_id is not None:
                encoded_time = _store_time(before_updated_at)
                conditions.append(
                    "(updated_at < ? OR (updated_at = ? AND thread_id < ?))"
                )
                parameters.extend((encoded_time, encoded_time, before_thread_id))
            query = "SELECT * FROM threads"
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY updated_at DESC, thread_id DESC LIMIT ?"
            parameters.append(limit + 1)
            rows = connection.execute(query, parameters).fetchall()
        return [self._thread_from_row(row) for row in rows[:limit]], len(rows) > limit

    def rename_thread(
        self,
        thread_id: str,
        title: str,
        *,
        client_request_id: str,
    ) -> ThreadProjection:
        title = " ".join(title.split()).strip()
        if not title or len(title) > 200:
            raise ValueError("thread title must contain between one and 200 characters")
        now = _utc_now()
        with self.jobs.control_transaction(
            scope="thread_rename",
            subject=client_request_id,
        ) as connection:
            self._require_thread(connection, thread_id)
            event = self.events.append_in_transaction(
                connection,
                thread_id=thread_id,
                event_type="thread.renamed",
                payload={"title": title},
                correlation_id=client_request_id,
                idempotency_key=f"thread:rename:{client_request_id}",
            )
            latest = connection.execute(
                "SELECT MAX(seq) AS seq FROM events WHERE thread_id=? "
                "AND event_type IN ('thread.renamed','thread.title_generated')",
                (thread_id,),
            ).fetchone()["seq"]
            if event.seq == latest:
                connection.execute(
                    "UPDATE threads SET title=?, updated_at=? WHERE thread_id=?",
                    (title, _store_time(event.created_at), thread_id),
                )
            return self._thread_from_row(self._require_thread(connection, thread_id))

    def archive_thread(
        self, thread_id: str, *, client_request_id: str | None = None
    ) -> ThreadProjection:
        return self._set_thread_status(
            thread_id,
            ThreadStatus.ARCHIVED,
            client_request_id=client_request_id or new_id("req"),
        )

    def restore_thread(
        self, thread_id: str, *, client_request_id: str
    ) -> ThreadProjection:
        return self._set_thread_status(
            thread_id, ThreadStatus.ACTIVE, client_request_id=client_request_id
        )

    def _set_thread_status(
        self,
        thread_id: str,
        target: ThreadStatus,
        *,
        client_request_id: str,
    ) -> ThreadProjection:
        now = _utc_now()
        with self.jobs.control_transaction(
            scope="thread_transition",
            subject=client_request_id,
        ) as connection:
            row = self._require_thread(connection, thread_id)
            if ThreadStatus(row["status"]) == target:
                return self._thread_from_row(row)
            event_type = (
                "thread.archived"
                if target is ThreadStatus.ARCHIVED
                else "thread.restored"
            )
            event = self.events.append_in_transaction(
                connection,
                thread_id=thread_id,
                event_type=event_type,
                payload={},
                correlation_id=client_request_id,
                idempotency_key=f"thread:status:{client_request_id}",
            )
            latest = connection.execute(
                "SELECT MAX(seq) AS seq FROM events WHERE thread_id=? "
                "AND event_type IN ('thread.archived','thread.restored')",
                (thread_id,),
            ).fetchone()["seq"]
            if event.seq == latest:
                connection.execute(
                    "UPDATE threads SET status = ?, updated_at = ? WHERE thread_id = ?",
                    (target.value, _store_time(event.created_at), thread_id),
                )
            return self._thread_from_row(self._require_thread(connection, thread_id))

    def _create_item_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        thread_id: str,
        turn_id: str,
        kind: ItemKind,
        content: dict[str, Any],
        status: ItemStatus = ItemStatus.CREATED,
        item_id: str | None = None,
        idempotency_key: str | None = None,
        client_message_id: str | None = None,
        now: datetime | None = None,
        snapshot_context: TurnSnapshotContext | None = None,
    ) -> ItemProjection:
        if not connection.in_transaction:
            raise RuntimeError("_create_item_in_transaction requires an active transaction")
        now = now or _utc_now()
        item_id = item_id or new_id("itm")
        if kind is ItemKind.TOOL_CALL:
            try:
                activity = PublicToolActivity.model_validate(content)
            except ValueError:
                raise ConflictError(
                    "Tool Items require PublicToolActivity content"
                ) from None
            if activity.status != status.value:
                raise ConflictError(
                    "Tool Item status differs from its public activity"
                )
            content = activity.model_dump(mode="json")
        self.events.append_in_transaction(
            connection,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            client_message_id=client_message_id,
            event_type="item.created",
            payload={"kind": kind.value, "status": status.value, "content": content},
            config_snapshot_id=(
                snapshot_context.config_snapshot_id if snapshot_context else None
            ),
            capability_snapshot_id=(
                snapshot_context.capability_snapshot_id if snapshot_context else None
            ),
            permission_snapshot_id=(
                snapshot_context.permission_snapshot_id if snapshot_context else None
            ),
            extension_snapshot_id=(
                snapshot_context.extension_snapshot_id if snapshot_context else None
            ),
            idempotency_key=idempotency_key,
            created_at=now,
        )
        timestamp = _store_time(now)
        connection.execute(
            """
            INSERT INTO items(
                item_id, thread_id, turn_id, kind, status, content_json,
                client_message_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                thread_id,
                turn_id,
                kind.value,
                status.value,
                json_dumps(content),
                client_message_id,
                timestamp,
                timestamp,
            ),
        )
        return self._item_from_row(self._require_item(connection, item_id))

    def create_item(
        self,
        *,
        turn_id: str,
        kind: ItemKind,
        content: dict[str, Any],
        status: ItemStatus = ItemStatus.CREATED,
        snapshot_context: TurnSnapshotContext | None = None,
        job_id: str | None = None,
        lease_token: str | None = None,
    ) -> ItemProjection:
        with self._mutation_transaction(
            scope="item_create",
            subject=turn_id,
            job_id=job_id,
            lease_token=lease_token,
        ) as connection:
            turn = self._require_turn(connection, turn_id)
            if TurnStatus(turn["status"]) in TERMINAL_TURN_STATUSES:
                raise ConflictError("items cannot be added to a terminal turn")
            return self._create_item_in_transaction(
                connection,
                thread_id=turn["thread_id"],
                turn_id=turn_id,
                kind=kind,
                content=content,
                status=status,
                snapshot_context=snapshot_context,
            )

    def append_message_delta(
        self,
        item_id: str,
        delta: str,
        *,
        idempotency_key: str,
        job_id: str | None = None,
        lease_token: str | None = None,
    ) -> ItemProjection:
        if not delta:
            raise ValueError("message delta must not be empty")
        if len(delta) > 1_000_000:
            raise ValueError("message delta is too large")
        if not idempotency_key:
            raise ValueError("message delta idempotency_key is required")
        now = _utc_now()
        with self._mutation_transaction(
            scope="message_delta",
            subject=item_id,
            job_id=job_id,
            lease_token=lease_token,
        ) as connection:
            row = self._require_item(connection, item_id)
            if ItemKind(row["kind"]) is not ItemKind.MESSAGE:
                raise ConflictError("only message items accept text deltas")
            if ItemStatus(row["status"]) is not ItemStatus.IN_PROGRESS:
                raise ConflictError("message deltas require an in-progress item")
            parent = self._require_turn(connection, row["turn_id"])
            if TurnStatus(parent["status"]) in TERMINAL_TURN_STATUSES:
                raise ConflictError("terminal turns cannot accept message deltas")
            existing = connection.execute(
                "SELECT payload_json FROM events "
                "WHERE thread_id = ? AND idempotency_key = ?",
                (row["thread_id"], idempotency_key),
            ).fetchone()
            if existing is not None:
                payload = json_loads(existing["payload_json"], {})
                if payload != {"delta": delta}:
                    raise ConflictError(
                        "message delta idempotency key was reused with different content"
                    )
                return self._item_from_row(row)
            content = json_loads(row["content_json"], {})
            if content.get("role") != "assistant":
                raise ConflictError("only assistant messages may stream output")
            text = str(content.get("text") or "") + delta
            if len(text) > 4_000_000:
                raise ConflictError("assistant message exceeded the durable size limit")
            content["text"] = text
            self.events.append_in_transaction(
                connection,
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                item_id=item_id,
                event_type="item.delta",
                payload={"delta": delta},
                idempotency_key=idempotency_key,
                created_at=now,
            )
            connection.execute(
                "UPDATE items SET content_json = ?, updated_at = ? WHERE item_id = ?",
                (json_dumps(content), _store_time(now), item_id),
            )
            return self._item_from_row(self._require_item(connection, item_id))

    def complete_tool_item(
        self,
        item_id: str,
        activity: PublicToolActivity | dict[str, Any],
        *,
        idempotency_key: str,
        job_id: str | None = None,
        lease_token: str | None = None,
    ) -> ItemProjection:
        try:
            public_activity = PublicToolActivity.model_validate(activity)
        except ValueError:
            raise ConflictError("tool result public activity is invalid") from None
        if (
            public_activity.phase != "completed"
            or public_activity.status != ItemStatus.COMPLETED.value
        ):
            raise ConflictError("tool result requires a completed public activity")
        now = _utc_now()
        with self._mutation_transaction(
            scope="tool_item_complete",
            subject=item_id,
            job_id=job_id,
            lease_token=lease_token,
        ) as connection:
            row = self._require_item(connection, item_id)
            if ItemKind(row["kind"]) is not ItemKind.TOOL_CALL:
                raise ConflictError("only tool-call items accept tool results")
            status = ItemStatus(row["status"])
            existing = connection.execute(
                "SELECT payload_json FROM events WHERE thread_id = ? AND idempotency_key = ?",
                (row["thread_id"], idempotency_key),
            ).fetchone()
            current_content = json_loads(row["content_json"], {})
            try:
                current_activity = PublicToolActivity.model_validate(current_content)
            except ValueError:
                raise ConflictError("stored tool public activity is invalid") from None
            identity_fields = (
                "tool_call_id",
                "tool_id",
                "tool_name",
                "display_label",
                "effects",
                "risk",
                "argument_summary",
                "argument_sha256",
            )
            if any(
                getattr(current_activity, field) != getattr(public_activity, field)
                for field in identity_fields
            ):
                raise ConflictError("tool result public identity changed")
            payload = {
                "activity": public_activity.model_dump(mode="json")
            }
            if existing is not None:
                if json_loads(existing["payload_json"], {}) != payload:
                    raise ConflictError(
                        "tool result idempotency key was reused with different content"
                    )
                return self._item_from_row(row)
            if status is not ItemStatus.IN_PROGRESS:
                raise ConflictError("tool result requires an in-progress tool item")
            content = public_activity.model_dump(mode="json")
            self.events.append_in_transaction(
                connection,
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                item_id=item_id,
                tool_call_id=public_activity.tool_call_id,
                event_type="tool.result",
                payload=payload,
                idempotency_key=idempotency_key,
                created_at=now,
            )
            self.events.append_in_transaction(
                connection,
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                item_id=item_id,
                event_type="item.status_changed",
                payload={
                    "from": ItemStatus.IN_PROGRESS.value,
                    "to": ItemStatus.COMPLETED.value,
                },
                idempotency_key=f"{idempotency_key}:completed",
                created_at=now,
            )
            connection.execute(
                "UPDATE items SET content_json = ?, status = ?, updated_at = ? "
                "WHERE item_id = ?",
                (
                    json_dumps(content),
                    ItemStatus.COMPLETED.value,
                    _store_time(now),
                    item_id,
                ),
            )
            return self._item_from_row(self._require_item(connection, item_id))

    def transition_item(
        self,
        item_id: str,
        target: ItemStatus,
        *,
        job_id: str | None = None,
        lease_token: str | None = None,
    ) -> ItemProjection:
        now = _utc_now()
        with self._mutation_transaction(
            scope="item_transition",
            subject=item_id,
            job_id=job_id,
            lease_token=lease_token,
        ) as connection:
            row = self._require_item(connection, item_id)
            current = ItemStatus(row["status"])
            if current == target:
                return self._item_from_row(row)
            parent = self._require_turn(connection, row["turn_id"])
            if TurnStatus(parent["status"]) in TERMINAL_TURN_STATUSES:
                raise ConflictError("items in a terminal turn cannot change state")
            if target not in ITEM_TRANSITIONS[current]:
                raise InvalidTransitionError(
                    f"item cannot transition from {current.value} to {target.value}"
                )
            kind = ItemKind(row["kind"])
            content_json: str | None = None
            tool_call_id: str | None = None
            if kind is ItemKind.TOOL_CALL:
                try:
                    activity = PublicToolActivity.model_validate(
                        json_loads(row["content_json"], {})
                    )
                    activity = PublicToolActivityProjector.transition(
                        activity,
                        target,
                    )
                except ValueError:
                    raise ConflictError(
                        "Tool Item public activity transition is invalid"
                    ) from None
                content_json = activity.model_dump_json()
                tool_call_id = activity.tool_call_id
            self.events.append_in_transaction(
                connection,
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                item_id=item_id,
                tool_call_id=tool_call_id,
                event_type="item.status_changed",
                payload={"from": current.value, "to": target.value},
                created_at=now,
            )
            if content_json is None:
                connection.execute(
                    "UPDATE items SET status = ?, updated_at = ? WHERE item_id = ?",
                    (target.value, _store_time(now), item_id),
                )
            else:
                connection.execute(
                    "UPDATE items SET status = ?, content_json = ?, updated_at = ? "
                    "WHERE item_id = ?",
                    (target.value, content_json, _store_time(now), item_id),
                )
            return self._item_from_row(self._require_item(connection, item_id))

    def append_execution_event(
        self,
        *,
        job_id: str,
        lease_token: str,
        thread_id: str,
        turn_id: str,
        event_type: str,
        payload: dict[str, Any],
        item_id: str | None = None,
        tool_call_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> EventEnvelope:
        """Append a Worker fact inside the exact leased execution epoch."""

        with self.jobs.execution_transaction(job_id, lease_token) as connection:
            job = connection.execute(
                "SELECT thread_id, turn_id FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if (
                job is None
                or job["thread_id"] != thread_id
                or job["turn_id"] != turn_id
            ):
                raise ConflictError("execution Event scope differs from its Job")
            return self.events.append_in_transaction(
                connection,
                thread_id=thread_id,
                turn_id=turn_id,
                item_id=item_id,
                job_id=job_id,
                tool_call_id=tool_call_id,
                event_type=event_type,
                payload=payload,
                idempotency_key=idempotency_key,
            )

    def _create_turn_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        thread_id: str,
        request: CreateTurnRequest,
        now: datetime | None = None,
        snapshot_context: TurnSnapshotContext | None = None,
        permission_account_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[TurnProjection, DurableJob]:
        thread = self._require_thread(connection, thread_id)
        if ThreadStatus(thread["status"]) != ThreadStatus.ACTIVE:
            raise ConflictError("turns cannot be created in an archived thread")
        if snapshot_context is not None:
            self.snapshots.validate_extension_snapshot_in_transaction(
                connection,
                snapshot_context.extension_snapshot_id,
                config_snapshot_id=snapshot_context.config_snapshot_id,
            )
            self.snapshots.validate_model_selection_in_transaction(
                connection,
                snapshot_context,
                expected_agent_model_id=request.agent_model_id,
                expected_image_model_id=request.image_model_id,
            )
        if request.client_message_id:
            message = connection.execute(
                "SELECT * FROM items WHERE thread_id = ? AND client_message_id = ?",
                (thread_id, request.client_message_id),
            ).fetchone()
            if message is not None:
                content = json_loads(message["content_json"], {})
                if (
                    content.get("text") != request.input
                    or content.get("metadata", {}) != request.metadata
                ):
                    raise ConflictError(
                        "client_message_id was reused with different input"
                    )
                duplicate = self._require_turn(connection, message["turn_id"])
                self.turn_inputs.match_client_intent_in_transaction(
                    connection,
                    thread_id=thread_id,
                    turn_id=duplicate["turn_id"],
                    request=request,
                )
                if (
                    duplicate["agent_model_id"] != request.agent_model_id
                    or duplicate["image_model_id"] != request.image_model_id
                ):
                    raise ConflictError(
                        "client_message_id was reused with a different model selection"
                    )
                self._assert_turn_snapshot_context(
                    connection, duplicate["turn_id"], snapshot_context
                )
                job_row = connection.execute(
                    "SELECT * FROM jobs WHERE turn_id = ? AND kind = 'agent_turn' "
                    "ORDER BY created_at LIMIT 1",
                    (duplicate["turn_id"],),
                ).fetchone()
                if job_row is None:
                    raise ConflictError("idempotent turn is missing its durable job")
                return self._turn_from_row(duplicate), self.jobs._from_row(job_row)
            duplicate = connection.execute(
                "SELECT * FROM turns WHERE thread_id = ? AND client_message_id = ?",
                (thread_id, request.client_message_id),
            ).fetchone()
            if duplicate is not None:
                self.turn_inputs.match_client_intent_in_transaction(
                    connection,
                    thread_id=thread_id,
                    turn_id=duplicate["turn_id"],
                    request=request,
                )
                if (
                    duplicate["input_text"] != request.input
                    or duplicate["agent_model_id"] != request.agent_model_id
                    or duplicate["image_model_id"] != request.image_model_id
                    or duplicate["metadata_json"] != json_dumps(request.metadata)
                ):
                    raise ConflictError("client_message_id was reused with different input")
                self._assert_turn_snapshot_context(
                    connection, duplicate["turn_id"], snapshot_context
                )
                job_row = connection.execute(
                    "SELECT * FROM jobs WHERE turn_id = ? AND kind = 'agent_turn' "
                    "ORDER BY created_at LIMIT 1",
                    (duplicate["turn_id"],),
                ).fetchone()
                if job_row is None:
                    raise ConflictError("idempotent turn is missing its durable job")
                return self._turn_from_row(duplicate), self.jobs._from_row(job_row)

        if snapshot_context is not None and permission_account_id is not None:
            self.snapshots.validate_permission_snapshot_current_in_transaction(
                connection,
                snapshot_context.permission_snapshot_id,
                account_id=permission_account_id,
            )
        now = now or _utc_now()
        turn_id = new_id("trn")
        self.events.append_in_transaction(
            connection,
            thread_id=thread_id,
            turn_id=turn_id,
            client_message_id=request.client_message_id,
            causation_id=causation_id,
            correlation_id=correlation_id or request.client_message_id or turn_id,
            trace_id=trace_id or f"trace_{turn_id}",
            event_type="turn.accepted",
            payload={
                "input": request.input,
                "agent_model_id": request.agent_model_id,
                "image_model_id": request.image_model_id,
                "explicit_tool_ids": request.explicit_tool_ids,
                "metadata": request.metadata,
                "model_catalog_snapshot_id": (
                    snapshot_context.model_catalog_snapshot_id
                    if snapshot_context
                    else None
                ),
            },
            config_snapshot_id=(
                snapshot_context.config_snapshot_id if snapshot_context else None
            ),
            capability_snapshot_id=(
                snapshot_context.capability_snapshot_id if snapshot_context else None
            ),
            permission_snapshot_id=(
                snapshot_context.permission_snapshot_id if snapshot_context else None
            ),
            extension_snapshot_id=(
                snapshot_context.extension_snapshot_id if snapshot_context else None
            ),
            idempotency_key=f"{turn_id}:accepted",
            created_at=now,
        )
        timestamp = _store_time(now)
        connection.execute(
            """
            INSERT INTO turns(
                turn_id, thread_id, status, input_text,
                agent_model_id, image_model_id,
                client_message_id, metadata_json, terminal_reason,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                turn_id,
                thread_id,
                TurnStatus.ACCEPTED.value,
                request.input,
                request.agent_model_id,
                request.image_model_id,
                request.client_message_id,
                json_dumps(request.metadata),
                timestamp,
                timestamp,
            ),
        )
        self.turn_inputs.append_initial_in_transaction(
            connection,
            thread_id=thread_id,
            turn_id=turn_id,
            request=request,
            created_at=now,
        )
        self._create_item_in_transaction(
            connection,
            thread_id=thread_id,
            turn_id=turn_id,
            kind=ItemKind.MESSAGE,
            status=ItemStatus.COMPLETED,
            content={
                "role": "user",
                "text": request.input,
                "explicit_tool_ids": request.explicit_tool_ids,
                "metadata": request.metadata,
            },
            client_message_id=request.client_message_id,
            idempotency_key=f"{turn_id}:user-message",
            now=now,
            snapshot_context=snapshot_context,
        )
        self.events.append_in_transaction(
            connection,
            thread_id=thread_id,
            turn_id=turn_id,
            event_type="turn.queued",
            payload={"from": TurnStatus.ACCEPTED.value, "to": TurnStatus.QUEUED.value},
            config_snapshot_id=(
                snapshot_context.config_snapshot_id if snapshot_context else None
            ),
            capability_snapshot_id=(
                snapshot_context.capability_snapshot_id if snapshot_context else None
            ),
            permission_snapshot_id=(
                snapshot_context.permission_snapshot_id if snapshot_context else None
            ),
            extension_snapshot_id=(
                snapshot_context.extension_snapshot_id if snapshot_context else None
            ),
            idempotency_key=f"{turn_id}:queued",
            created_at=now,
        )
        connection.execute(
            "UPDATE turns SET status = ?, updated_at = ? WHERE turn_id = ?",
            (TurnStatus.QUEUED.value, timestamp, turn_id),
        )
        job = self.jobs.enqueue_in_transaction(
            connection,
            kind="agent_turn",
            payload={"thread_id": thread_id, "turn_id": turn_id},
            idempotency_key=f"turn:{turn_id}:execute",
            thread_id=thread_id,
            turn_id=turn_id,
            max_attempts=3,
            now=now,
            event_context=(snapshot_context.to_dict() if snapshot_context else None),
        )
        if thread["title"] is None:
            generated_title = _default_thread_title(request.input)
            self.events.append_in_transaction(
                connection,
                thread_id=thread_id,
                turn_id=turn_id,
                event_type="thread.title_generated",
                payload={"title": generated_title},
                causation_id=f"{turn_id}:accepted",
                correlation_id=correlation_id or request.client_message_id or turn_id,
                idempotency_key="thread:title-generated",
                created_at=now,
            )
            connection.execute(
                "UPDATE threads SET title=?, updated_at=? WHERE thread_id=?",
                (generated_title, timestamp, thread_id),
            )
        return self._turn_from_row(self._require_turn(connection, turn_id)), job

    def _create_operation_turn_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        thread_id: str,
        request: CreateTurnRequest,
        snapshot_context: TurnSnapshotContext,
        operation_kind: str,
        account_id: str,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        now: datetime | None = None,
    ) -> TurnProjection:
        """Accept a backend-managed Turn without creating an ``agent_turn`` job.

        The caller must enqueue its one authoritative operation job in the same
        SQLite transaction.  This keeps UI-visible Turn state while preventing a
        second model worker from racing a dedicated executor such as retouch.
        """

        if not connection.in_transaction:
            raise RuntimeError(
                "_create_operation_turn_in_transaction requires an active transaction"
            )
        operation_kind = str(operation_kind or "").strip()
        if not operation_kind:
            raise ValueError("operation_kind is required")
        if request.metadata.get("operation") != operation_kind:
            raise ValueError("operation Turn metadata does not match operation_kind")
        if not request.client_message_id:
            raise ValueError("operation Turn requires a client_message_id")
        if not request.agent_model_id:
            raise ValueError("operation Turn requires a canonical Agent model")
        if not request.image_model_id:
            raise ValueError("operation Turn requires a canonical image model")

        thread = self._require_thread(connection, thread_id)
        if ThreadStatus(thread["status"]) != ThreadStatus.ACTIVE:
            raise ConflictError("turns cannot be created in an archived thread")
        self.snapshots.validate_turn_context_in_transaction(
            connection,
            snapshot_context,
            account_id=account_id,
            expected_intent=request.input,
            expected_agent_model_id=request.agent_model_id,
            expected_image_model_id=request.image_model_id,
        )

        message = connection.execute(
            "SELECT * FROM items WHERE thread_id = ? AND client_message_id = ?",
            (thread_id, request.client_message_id),
        ).fetchone()
        duplicate = (
            self._require_turn(connection, message["turn_id"])
            if message is not None
            else connection.execute(
                "SELECT * FROM turns WHERE thread_id = ? AND client_message_id = ?",
                (thread_id, request.client_message_id),
            ).fetchone()
        )
        if duplicate is not None:
            self.turn_inputs.match_client_intent_in_transaction(
                connection,
                thread_id=thread_id,
                turn_id=duplicate["turn_id"],
                request=request,
            )
            if message is None:
                message = connection.execute(
                    "SELECT * FROM items WHERE thread_id = ? AND turn_id = ? "
                    "AND client_message_id = ?",
                    (thread_id, duplicate["turn_id"], request.client_message_id),
                ).fetchone()
            content = json_loads(message["content_json"], {}) if message else {}
            if (
                duplicate["input_text"] != request.input
                or duplicate["agent_model_id"] != request.agent_model_id
                or duplicate["image_model_id"] != request.image_model_id
                or duplicate["metadata_json"] != json_dumps(request.metadata)
                or content.get("role") != "user"
                or content.get("text") != request.input
                or content.get("metadata", {}) != request.metadata
            ):
                raise ConflictError(
                    "operation client_message_id was reused with different input"
                )
            self._assert_turn_snapshot_context(
                connection, duplicate["turn_id"], snapshot_context
            )
            conflicting_job = connection.execute(
                "SELECT kind FROM jobs WHERE turn_id = ? AND kind != ? LIMIT 1",
                (duplicate["turn_id"], operation_kind),
            ).fetchone()
            if conflicting_job is not None:
                raise ConflictError(
                    "operation Turn is bound to a different durable executor"
                )
            return self._turn_from_row(duplicate)

        now = now or _utc_now()
        turn_id = new_id("trn")
        event_context = snapshot_context.to_dict()
        self.events.append_in_transaction(
            connection,
            thread_id=thread_id,
            turn_id=turn_id,
            client_message_id=request.client_message_id,
            causation_id=causation_id,
            correlation_id=(
                correlation_id or request.client_message_id or turn_id
            ),
            trace_id=f"trace_{turn_id}",
            event_type="turn.accepted",
            payload={
                "input": request.input,
                "agent_model_id": request.agent_model_id,
                "image_model_id": request.image_model_id,
                "explicit_tool_ids": request.explicit_tool_ids,
                "metadata": request.metadata,
                "model_catalog_snapshot_id": (
                    snapshot_context.model_catalog_snapshot_id
                ),
            },
            config_snapshot_id=snapshot_context.config_snapshot_id,
            capability_snapshot_id=snapshot_context.capability_snapshot_id,
            permission_snapshot_id=snapshot_context.permission_snapshot_id,
            extension_snapshot_id=snapshot_context.extension_snapshot_id,
            idempotency_key=f"{turn_id}:accepted",
            created_at=now,
        )
        timestamp = _store_time(now)
        connection.execute(
            "INSERT INTO turns("
            "turn_id, thread_id, status, input_text, agent_model_id, image_model_id, client_message_id, "
            "metadata_json, terminal_reason, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                turn_id,
                thread_id,
                TurnStatus.ACCEPTED.value,
                request.input,
                request.agent_model_id,
                request.image_model_id,
                request.client_message_id,
                json_dumps(request.metadata),
                timestamp,
                timestamp,
            ),
        )
        self.turn_inputs.append_initial_in_transaction(
            connection,
            thread_id=thread_id,
            turn_id=turn_id,
            request=request,
            created_at=now,
        )
        self._create_item_in_transaction(
            connection,
            thread_id=thread_id,
            turn_id=turn_id,
            kind=ItemKind.MESSAGE,
            status=ItemStatus.COMPLETED,
            content={
                "role": "user",
                "text": request.input,
                "explicit_tool_ids": request.explicit_tool_ids,
                "metadata": request.metadata,
            },
            client_message_id=request.client_message_id,
            idempotency_key=f"{turn_id}:user-message",
            now=now,
            snapshot_context=snapshot_context,
        )
        self.events.append_in_transaction(
            connection,
            thread_id=thread_id,
            turn_id=turn_id,
            event_type="turn.queued",
            payload={
                "from": TurnStatus.ACCEPTED.value,
                "to": TurnStatus.QUEUED.value,
                "operation": operation_kind,
            },
            config_snapshot_id=event_context["config_snapshot_id"],
            capability_snapshot_id=event_context["capability_snapshot_id"],
            permission_snapshot_id=event_context["permission_snapshot_id"],
            extension_snapshot_id=event_context["extension_snapshot_id"],
            idempotency_key=f"{turn_id}:queued",
            created_at=now,
        )
        connection.execute(
            "UPDATE turns SET status = ?, updated_at = ? WHERE turn_id = ?",
            (TurnStatus.QUEUED.value, timestamp, turn_id),
        )
        return self._turn_from_row(self._require_turn(connection, turn_id))

    def create_turn(
        self,
        thread_id: str,
        request: CreateTurnRequest,
        *,
        snapshot_context: TurnSnapshotContext | None = None,
        permission_account_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        trace_id: str | None = None,
    ) -> TurnMutationResponse:
        with self.jobs.control_transaction(
            scope="turn_create",
            subject=request.client_message_id,
        ) as connection:
            turn, job = self._create_turn_in_transaction(
                connection,
                thread_id=thread_id,
                request=request,
                snapshot_context=snapshot_context,
                permission_account_id=permission_account_id,
                causation_id=causation_id,
                correlation_id=correlation_id,
                trace_id=trace_id,
            )
            return TurnMutationResponse(
                turn=turn,
                job=job,
                watermark=self.events.watermark(thread_id, connection),
            )

    def queue_turn(
        self,
        thread_id: str,
        request: CreateTurnRequest,
        *,
        snapshot_context: TurnSnapshotContext | None = None,
        permission_account_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        trace_id: str | None = None,
    ) -> TurnMutationResponse:
        return self.create_turn(
            thread_id,
            request,
            snapshot_context=snapshot_context,
            permission_account_id=permission_account_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            trace_id=trace_id,
        )

    def get_turn(self, turn_id: str) -> TurnProjection:
        with self.database.reader() as connection:
            return self._turn_from_row(self._require_turn(connection, turn_id))

    def transition_turn(
        self,
        turn_id: str,
        target: TurnStatus,
        *,
        reason: str | None = None,
        job_id: str | None = None,
        lease_token: str | None = None,
    ) -> TurnProjection:
        now = _utc_now()
        with self._mutation_transaction(
            scope="turn_transition",
            subject=turn_id,
            job_id=job_id,
            lease_token=lease_token,
        ) as connection:
            row = self._require_turn(connection, turn_id)
            current = TurnStatus(row["status"])
            if current == target:
                return self._turn_from_row(row)
            if target not in TURN_TRANSITIONS[current]:
                raise InvalidTransitionError(
                    f"turn cannot transition from {current.value} to {target.value}"
                )
            self._transition_turn_in_transaction(
                connection, row=row, target=target, reason=reason, now=now
            )
            return self._turn_from_row(self._require_turn(connection, turn_id))

    def begin_finalizing_if_inputs_applied(
        self,
        turn_id: str,
        *,
        applied_through_ordinal: int,
        job_id: str | None = None,
        lease_token: str | None = None,
    ) -> bool:
        """Linearize finalization against concurrent steer admission.

        Returning ``False`` means a newer input revision committed first and
        must receive another model batch. If this transaction commits the
        FINALIZING transition first, a concurrent steer observes that state and
        is rejected, so a 202 steer can never disappear behind completion.
        """

        if (
            isinstance(applied_through_ordinal, bool)
            or not isinstance(applied_through_ordinal, int)
            or applied_through_ordinal < 0
        ):
            raise ValueError("applied input ordinal is invalid")
        now = _utc_now()
        with self._mutation_transaction(
            scope="turn_finalizing",
            subject=turn_id,
            job_id=job_id,
            lease_token=lease_token,
        ) as connection:
            turn = self._require_turn(connection, turn_id)
            current = TurnStatus(turn["status"])
            if current in TERMINAL_TURN_STATUSES:
                raise ConflictError("a terminal Turn cannot begin finalizing")
            if current not in {TurnStatus.STREAMING, TurnStatus.FINALIZING}:
                raise InvalidTransitionError(
                    f"turn cannot begin finalizing from {current.value}"
                )
            row = connection.execute(
                "SELECT MAX(ordinal) AS ordinal FROM turn_input_revisions "
                "WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            latest = row["ordinal"] if row is not None else None
            if latest is None:
                raise ConflictError("Turn has no durable input revision")
            if int(latest) > applied_through_ordinal:
                if current is TurnStatus.FINALIZING:
                    raise ConflictError(
                        "finalizing input ordinal is behind the durable head"
                    )
                return False
            if int(latest) < applied_through_ordinal:
                raise ConflictError("applied input ordinal exceeds the durable head")
            if current is TurnStatus.FINALIZING:
                return True
            self._transition_turn_in_transaction(
                connection,
                row=turn,
                target=TurnStatus.FINALIZING,
                reason=None,
                now=now,
            )
            return True

    def finish_turn_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_token: str,
        target: TurnStatus,
        reason: str | None = None,
    ) -> TurnMutationResponse:
        """Atomically settle a conversational turn and its durable execution job."""

        if target not in TERMINAL_TURN_STATUSES:
            raise ValueError("finish_turn_job requires a terminal turn status")
        now = _utc_now()
        if self.jobs.expire_deadline(job_id, now=now):
            with self.database.reader() as connection:
                job_row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                job = self.jobs._from_row(job_row)
                if job.turn_id is None or job.thread_id is None:
                    raise ConflictError("expired job is not attached to a turn")
                return TurnMutationResponse(
                    turn=self._turn_from_row(
                        self._require_turn(connection, job.turn_id)
                    ),
                    job=job,
                    watermark=self.events.watermark(job.thread_id, connection),
                )
        with self.jobs.execution_transaction(job_id, lease_token) as connection:
            job_row = self.jobs._owned_row(
                connection, job_id, worker_id, lease_token, now
            )
            if job_row["turn_id"] is None or job_row["thread_id"] is None:
                raise ConflictError("job is not attached to a conversational turn")
            turn = self._require_turn(connection, job_row["turn_id"])
            self._transition_turn_in_transaction(
                connection, row=turn, target=target, reason=reason, now=now
            )
            updated_turn = self._require_turn(connection, job_row["turn_id"])
            updated_job = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            response = TurnMutationResponse(
                turn=self._turn_from_row(updated_turn),
                job=self.jobs._from_row(updated_job),
                watermark=self.events.watermark(job_row["thread_id"], connection),
            )
        self.jobs.retire_execution_permit(job_id, lease_token)
        return response

    def fail_turn_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_token: str,
        error: str,
        retryable: bool,
        retry_delay_seconds: int = 0,
        preserve_attempt: bool = False,
    ) -> TurnMutationResponse:
        """Atomically schedule an agent-turn retry or fail the whole turn."""

        now = _utc_now()
        if self.jobs.expire_deadline(job_id, now=now):
            with self.database.reader() as connection:
                job_row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                job = self.jobs._from_row(job_row)
                if job.turn_id is None or job.thread_id is None:
                    raise ConflictError("expired job is not attached to a turn")
                return TurnMutationResponse(
                    turn=self._turn_from_row(
                        self._require_turn(connection, job.turn_id)
                    ),
                    job=job,
                    watermark=self.events.watermark(job.thread_id, connection),
                )
        with self.jobs.execution_transaction(job_id, lease_token) as connection:
            job = self.jobs._owned_row(
                connection, job_id, worker_id, lease_token, now
            )
            if job["turn_id"] is None or job["thread_id"] is None:
                raise ConflictError("job is not attached to a conversational turn")
            turn = self._require_turn(connection, job["turn_id"])
            if retryable and int(job["attempt"]) < int(job["max_attempts"]):
                retry_available_at = now + timedelta(
                    seconds=max(0, retry_delay_seconds)
                )
                retry_max_attempts = int(job["max_attempts"]) + int(
                    preserve_attempt
                )
                self.jobs._assert_transition(
                    JobStatus(job["status"]), JobStatus.RETRY_SCHEDULED
                )
                self.jobs._append_job_event(
                    connection,
                    row_or_values=job,
                    event_type="job.retry_scheduled",
                    payload={
                        "attempt": job["attempt"],
                        "error": error,
                        "available_at": _store_time(retry_available_at),
                        "max_attempts": retry_max_attempts,
                    },
                    created_at=now,
                )
                connection.execute(
                    "UPDATE jobs SET status = ?, max_attempts = CASE WHEN ? THEN "
                    "max_attempts + 1 ELSE max_attempts END, "
                    "lease_owner = NULL, lease_token = NULL, "
                    "lease_expires_at = NULL, heartbeat_at = NULL, available_at = ?, "
                    "last_error = ?, updated_at = ? WHERE job_id = ?",
                    (
                        JobStatus.RETRY_SCHEDULED.value,
                        int(preserve_attempt),
                        _store_time(retry_available_at),
                        error,
                        _store_time(now),
                        job_id,
                    ),
                )
                self._transition_turn_in_transaction(
                    connection,
                    row=turn,
                    target=TurnStatus.RETRY_WAIT,
                    reason=error,
                    now=now,
                )
            else:
                job_target = (
                    JobStatus.DEAD_LETTER if retryable else JobStatus.FAILED
                )
                self.jobs._assert_transition(JobStatus(job["status"]), job_target)
                self.jobs._append_job_event(
                    connection,
                    row_or_values=job,
                    event_type=(
                        "job.dead_lettered"
                        if job_target == JobStatus.DEAD_LETTER
                        else "job.failed"
                    ),
                    payload={"attempt": job["attempt"], "error": error},
                    created_at=now,
                )
                connection.execute(
                    "UPDATE jobs SET status = ?, lease_owner = NULL, lease_token = NULL, "
                    "lease_expires_at = NULL, heartbeat_at = NULL, last_error = ?, "
                    "updated_at = ? WHERE job_id = ?",
                    (job_target.value, error, _store_time(now), job_id),
                )
                self._transition_turn_in_transaction(
                    connection,
                    row=turn,
                    target=TurnStatus.FAILED,
                    reason=error,
                    now=now,
                )
            updated_turn = self._require_turn(connection, job["turn_id"])
            updated_job = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            response = TurnMutationResponse(
                turn=self._turn_from_row(updated_turn),
                job=self.jobs._from_row(updated_job),
                watermark=self.events.watermark(job["thread_id"], connection),
            )
        self.jobs.retire_execution_permit(job_id, lease_token)
        return response

    def _transition_turn_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        target: TurnStatus,
        reason: str | None,
        now: datetime,
    ) -> None:
        current = TurnStatus(row["status"])
        if current != target and target not in TURN_TRANSITIONS[current]:
            raise InvalidTransitionError(
                f"turn cannot transition from {current.value} to {target.value}"
            )
        if target in TERMINAL_TURN_STATUSES:
            self._settle_turn_dependents_in_transaction(
                connection, row=row, target=target, reason=reason, now=now
            )
        turn_event = self.events.append_in_transaction(
            connection,
            thread_id=row["thread_id"],
            turn_id=row["turn_id"],
            event_type="turn.status_changed",
            payload={"from": current.value, "to": target.value, "reason": reason},
            created_at=now,
        )
        if target in TERMINAL_TURN_STATUSES:
            archive_visible_reasoning_in_transaction(
                connection,
                self.events,
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                terminal_event_id=turn_event.event_id,
                terminal_status=target,
                reason=reason,
                now=now,
            )
        terminal_reason = reason if target in TERMINAL_TURN_STATUSES else None
        connection.execute(
            "UPDATE turns SET status = ?, terminal_reason = ?, updated_at = ? "
            "WHERE turn_id = ?",
            (target.value, terminal_reason, _store_time(now), row["turn_id"]),
        )

    def _settle_turn_dependents_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        target: TurnStatus,
        reason: str | None,
        now: datetime,
    ) -> None:
        if target == TurnStatus.COMPLETED:
            job_target, job_event = JobStatus.COMPLETED, "job.completed"
            item_target = ItemStatus.COMPLETED
        elif target == TurnStatus.FAILED:
            job_target, job_event = JobStatus.FAILED, "job.failed"
            item_target = ItemStatus.FAILED
        else:
            job_target, job_event = JobStatus.CANCELLED, "job.cancelled"
            item_target = ItemStatus.CANCELLED

        jobs = connection.execute(
            "SELECT * FROM jobs WHERE turn_id = ? ORDER BY created_at, job_id",
            (row["turn_id"],),
        ).fetchall()
        for job in jobs:
            status = JobStatus(job["status"])
            if status in TERMINAL_JOB_STATUSES:
                continue
            self.events.append_in_transaction(
                connection,
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                job_id=job["job_id"],
                event_type=job_event,
                payload={
                    "attempt": job["attempt"],
                    "reason": reason or target.value,
                    "settled_by_turn": True,
                },
                idempotency_key=f"{job['job_id']}:turn-terminal:{target.value}",
                created_at=now,
            )
            connection.execute(
                "UPDATE jobs SET status = ?, lease_owner = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, heartbeat_at = NULL, last_error = ?, "
                "updated_at = ? WHERE job_id = ?",
                (
                    job_target.value,
                    reason,
                    _store_time(now),
                    job["job_id"],
                ),
            )

        interactions = connection.execute(
            "SELECT * FROM interactions WHERE turn_id = ? AND status = ?",
            (row["turn_id"], InteractionStatus.PENDING.value),
        ).fetchall()
        for interaction in interactions:
            self.events.append_in_transaction(
                connection,
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                job_id=interaction["job_id"],
                item_id=interaction["interaction_id"],
                event_type="interaction.cancelled",
                payload={"reason": reason or target.value},
                idempotency_key=f"{interaction['interaction_id']}:turn-terminal",
                created_at=now,
            )
            connection.execute(
                "UPDATE interactions SET status = ?, updated_at = ? "
                "WHERE interaction_id = ?",
                (
                    InteractionStatus.CANCELLED.value,
                    _store_time(now),
                    interaction["interaction_id"],
                ),
            )

        items = connection.execute(
            "SELECT * FROM items WHERE turn_id = ? ORDER BY created_at, item_id",
            (row["turn_id"],),
        ).fetchall()
        for item in items:
            status = ItemStatus(item["status"])
            if status in {ItemStatus.COMPLETED, ItemStatus.FAILED, ItemStatus.CANCELLED}:
                continue
            self.events.append_in_transaction(
                connection,
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                item_id=item["item_id"],
                event_type="item.status_changed",
                payload={
                    "from": status.value,
                    "to": item_target.value,
                    "reason": reason or target.value,
                },
                idempotency_key=f"{item['item_id']}:turn-terminal:{target.value}",
                created_at=now,
            )
            connection.execute(
                "UPDATE items SET status = ?, updated_at = ? WHERE item_id = ?",
                (item_target.value, _store_time(now), item["item_id"]),
            )

    def steer_turn(
        self, turn_id: str, request: SteerTurnRequest
    ) -> TurnMutationResponse:
        with self.jobs.control_transaction(
            scope="turn_steer",
            subject=request.client_message_id,
        ) as connection:
            return self._steer_turn_in_transaction(
                connection,
                turn_id=turn_id,
                request=request,
            )

    def _steer_turn_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        turn_id: str,
        request: SteerTurnRequest,
        now: datetime | None = None,
    ) -> TurnMutationResponse:
        """Append steer intent inside an existing Runtime transaction."""

        if not connection.in_transaction:
            raise RuntimeError("steer requires an active transaction")
        now = now or _utc_now()
        turn = self._require_turn(connection, turn_id)
        request = request.model_copy(
            update={
                "agent_model_id": request.agent_model_id or turn["agent_model_id"],
                "image_model_id": request.image_model_id or turn["image_model_id"],
            }
        )
        if (
            turn["agent_model_id"] != request.agent_model_id
            or turn["image_model_id"] != request.image_model_id
        ):
            raise ConflictError(
                "steer model selection differs from the active Turn snapshot"
            )
        matched_revision = self.turn_inputs.match_client_intent_in_transaction(
            connection,
            thread_id=turn["thread_id"],
            turn_id=turn_id,
            request=request,
        )
        if matched_revision is not None:
            return TurnMutationResponse(
                turn=self._turn_from_row(turn),
                job=None,
                watermark=self.events.watermark(turn["thread_id"], connection),
            )
        if request.client_message_id:
            # Compatibility fence for storage imported before input revisions
            # became authoritative. Fresh v1 Turns always match above.
            message = connection.execute(
                "SELECT * FROM items WHERE thread_id = ? AND client_message_id = ?",
                (turn["thread_id"], request.client_message_id),
            ).fetchone()
            if message is not None:
                content = json_loads(message["content_json"], {})
                if (
                    content.get("text") != request.input
                    or content.get("metadata", {}) != request.metadata
                ):
                    raise ConflictError(
                        "client_message_id was reused with different steer input"
                    )
                existing_turn = self._require_turn(connection, message["turn_id"])
                if existing_turn["turn_id"] != turn_id:
                    raise ConflictError("client_message_id belongs to a different Turn")
                return TurnMutationResponse(
                    turn=self._turn_from_row(existing_turn),
                    job=None,
                    watermark=self.events.watermark(turn["thread_id"], connection),
                )
        status = TurnStatus(turn["status"])
        if status is TurnStatus.FINALIZING or status in TERMINAL_TURN_STATUSES:
            raise ConflictError(
                "a finalizing or terminal Turn cannot accept new steer input"
            )
        idempotency_key = (
            f"{turn_id}:steer:{request.client_message_id}"
            if request.client_message_id
            else None
        )
        if idempotency_key:
            duplicate = connection.execute(
                "SELECT * FROM events WHERE thread_id = ? AND idempotency_key = ?",
                (turn["thread_id"], idempotency_key),
            ).fetchone()
            if duplicate is not None:
                if duplicate["payload_json"] != json_dumps(
                    {
                        "input": request.input,
                        "agent_model_id": request.agent_model_id,
                        "image_model_id": request.image_model_id,
                        "explicit_tool_ids": request.explicit_tool_ids,
                        "metadata": request.metadata,
                    }
                ):
                    raise ConflictError(
                        "client_message_id was reused with different steer input"
                    )
                return TurnMutationResponse(
                    turn=self._turn_from_row(turn),
                    job=None,
                    watermark=self.events.watermark(turn["thread_id"], connection),
                )
        item_id = new_id("itm")
        self.events.append_in_transaction(
            connection,
            thread_id=turn["thread_id"],
            turn_id=turn_id,
            item_id=item_id,
            client_message_id=request.client_message_id,
            event_type="turn.steered",
            payload={
                "input": request.input,
                "agent_model_id": request.agent_model_id,
                "image_model_id": request.image_model_id,
                "explicit_tool_ids": request.explicit_tool_ids,
                "metadata": request.metadata,
            },
            idempotency_key=idempotency_key,
            created_at=now,
        )
        timestamp = _store_time(now)
        connection.execute(
            """
            INSERT INTO items(
                item_id, thread_id, turn_id, kind, status, content_json,
                client_message_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                turn["thread_id"],
                turn_id,
                ItemKind.MESSAGE.value,
                ItemStatus.COMPLETED.value,
                json_dumps(
                    {
                        "role": "user",
                        "text": request.input,
                        "explicit_tool_ids": request.explicit_tool_ids,
                        "metadata": request.metadata,
                        "steer": True,
                    }
                ),
                request.client_message_id,
                timestamp,
                timestamp,
            ),
        )
        self.turn_inputs.append_steer_in_transaction(
            connection,
            thread_id=turn["thread_id"],
            turn_id=turn_id,
            request=request,
            created_at=now,
        )
        return TurnMutationResponse(
            turn=self._turn_from_row(turn),
            job=None,
            watermark=self.events.watermark(turn["thread_id"], connection),
        )

    def _cancel_turn_jobs_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        turn: sqlite3.Row,
        reason: str,
        now: datetime,
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM jobs WHERE turn_id = ? ORDER BY created_at, job_id",
            (turn["turn_id"],),
        ).fetchall()
        for job in rows:
            status = JobStatus(job["status"])
            if status in TERMINAL_JOB_STATUSES:
                continue
            if JobStatus.CANCELLED not in JOB_TRANSITIONS[status]:
                continue
            self.events.append_in_transaction(
                connection,
                thread_id=turn["thread_id"],
                turn_id=turn["turn_id"],
                job_id=job["job_id"],
                event_type="job.cancelled",
                payload={"attempt": job["attempt"], "reason": reason},
                idempotency_key=f"{job['job_id']}:cancelled",
                created_at=now,
            )
            connection.execute(
                "UPDATE jobs SET status = ?, lease_owner = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, heartbeat_at = NULL, last_error = ?, "
                "updated_at = ? WHERE job_id = ?",
                (
                    JobStatus.CANCELLED.value,
                    reason,
                    _store_time(now),
                    job["job_id"],
                ),
            )

    def replace_turn(
        self,
        turn_id: str,
        request: ReplaceTurnRequest,
        *,
        snapshot_context: TurnSnapshotContext | None = None,
        permission_account_id: str | None = None,
    ) -> ReplaceTurnResponse:
        now = _utc_now()
        with self.jobs.control_transaction(
            scope="turn_replace",
            subject=request.client_message_id,
        ) as connection:
            current = self._require_turn(connection, turn_id)
            current_status = TurnStatus(current["status"])
            if current_status in TERMINAL_TURN_STATUSES:
                if (
                    current_status == TurnStatus.SUPERSEDED
                    and request.client_message_id
                ):
                    existing = connection.execute(
                        "SELECT * FROM turns WHERE thread_id = ? AND client_message_id = ?",
                        (current["thread_id"], request.client_message_id),
                    ).fetchone()
                    if existing is not None:
                        metadata = json_loads(existing["metadata_json"], {})
                        user_metadata = {
                            key: value
                            for key, value in metadata.items()
                            if key not in {"replaces_turn_id", "checkpoint_item_id"}
                        }
                        if (
                            metadata.get("replaces_turn_id") != turn_id
                            or existing["input_text"] != request.input
                            or existing["agent_model_id"] != request.agent_model_id
                            or existing["image_model_id"] != request.image_model_id
                            or user_metadata != request.metadata
                        ):
                            raise ConflictError(
                                "replacement client_message_id was reused with different input"
                            )
                        self._assert_turn_snapshot_context(
                            connection, existing["turn_id"], snapshot_context
                        )
                        job_row = connection.execute(
                            "SELECT * FROM jobs WHERE turn_id = ? AND kind = 'agent_turn' "
                            "ORDER BY created_at LIMIT 1",
                            (existing["turn_id"],),
                        ).fetchone()
                        if job_row is None:
                            raise ConflictError("replacement turn is missing its durable job")
                        return ReplaceTurnResponse(
                            superseded_turn=self._turn_from_row(current),
                            replacement_turn=self._turn_from_row(existing),
                            job=self.jobs._from_row(job_row),
                            watermark=self.events.watermark(
                                current["thread_id"], connection
                            ),
                        )
                raise ConflictError("a terminal turn cannot be replaced")
            checkpoint = self._create_item_in_transaction(
                connection,
                thread_id=current["thread_id"],
                turn_id=turn_id,
                kind=ItemKind.CHECKPOINT,
                status=ItemStatus.COMPLETED,
                content={"reason": request.reason, "last_status": current_status.value},
                idempotency_key=f"{turn_id}:replacement-checkpoint",
                now=now,
            )
            self._cancel_turn_jobs_in_transaction(
                connection, turn=current, reason=request.reason, now=now
            )
            self._transition_turn_in_transaction(
                connection,
                row=current,
                target=TurnStatus.SUPERSEDED,
                reason=request.reason,
                now=now,
            )
            replacement_request = CreateTurnRequest(
                input=request.input,
                agent_model_id=request.agent_model_id,
                image_model_id=request.image_model_id,
                client_message_id=request.client_message_id,
                metadata={
                    **request.metadata,
                    "replaces_turn_id": turn_id,
                    "checkpoint_item_id": checkpoint.item_id,
                },
            )
            replacement, job = self._create_turn_in_transaction(
                connection,
                thread_id=current["thread_id"],
                request=replacement_request,
                now=now,
                snapshot_context=snapshot_context,
                permission_account_id=permission_account_id,
            )
            superseded = self._turn_from_row(self._require_turn(connection, turn_id))
            return ReplaceTurnResponse(
                superseded_turn=superseded,
                replacement_turn=replacement,
                job=job,
                watermark=self.events.watermark(current["thread_id"], connection),
            )

    def interrupt_turn(self, turn_id: str, *, reason: str) -> TurnMutationResponse:
        now = _utc_now()
        with self.jobs.control_transaction(
            scope="turn_interrupt",
            subject=turn_id,
        ) as connection:
            turn = self._require_turn(connection, turn_id)
            current = TurnStatus(turn["status"])
            if current == TurnStatus.INTERRUPTED:
                return TurnMutationResponse(
                    turn=self._turn_from_row(turn),
                    watermark=self.events.watermark(turn["thread_id"], connection),
                )
            if current in TERMINAL_TURN_STATUSES:
                raise ConflictError(f"a {current.value} turn cannot be interrupted")
            self._cancel_turn_jobs_in_transaction(
                connection, turn=turn, reason=reason, now=now
            )
            self._transition_turn_in_transaction(
                connection,
                row=turn,
                target=TurnStatus.INTERRUPTED,
                reason=reason,
                now=now,
            )
            updated = self._require_turn(connection, turn_id)
            return TurnMutationResponse(
                turn=self._turn_from_row(updated),
                watermark=self.events.watermark(turn["thread_id"], connection),
            )

    def request_interaction(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_token: str,
        kind: InteractionKind,
        prompt: str,
        idempotency_key: str,
        options: list[dict[str, Any]] | None = None,
        contract: InteractionContract | dict[str, Any] | None = None,
        expires_at: datetime | None = None,
        checkpoint: dict[str, Any] | None = None,
    ) -> InteractionProjection:
        now = _utc_now()
        if self.jobs.expire_deadline(job_id, now=now):
            raise ConflictError("job deadline expired before interaction request")
        with self.jobs.execution_transaction(job_id, lease_token) as connection:
            duplicate = connection.execute(
                "SELECT * FROM interactions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if duplicate is not None:
                if duplicate["job_id"] != job_id:
                    raise ConflictError(
                        "interaction idempotency key belongs to another job"
                    )
                return InteractionProjection.model_validate(
                    self.interactions.create_in_transaction(
                        connection,
                        kind=kind,
                        prompt=prompt,
                        thread_id=duplicate["thread_id"],
                        turn_id=duplicate["turn_id"],
                        job_id=duplicate["job_id"],
                        idempotency_key=idempotency_key,
                        options=options,
                        contract=contract,
                        expires_at=expires_at,
                        now=now,
                    )
                )
            job = self.jobs._owned_row(
                connection, job_id, worker_id, lease_token, now
            )
            if JobStatus(job["status"]) != JobStatus.RUNNING:
                raise InvalidTransitionError(
                    "only a running job can request human interaction"
                )
            if job["thread_id"] is None or job["turn_id"] is None:
                raise ConflictError("human interaction requires a thread-bound job")
            turn = self._require_turn(connection, job["turn_id"])
            current_turn = TurnStatus(turn["status"])
            if TurnStatus.WAITING_HUMAN not in TURN_TRANSITIONS[current_turn]:
                raise InvalidTransitionError(
                    f"turn cannot request interaction from {current_turn.value}"
                )
            interaction_id = new_id("hitl")
            interaction = self.interactions.create_in_transaction(
                connection,
                kind=kind,
                prompt=prompt,
                thread_id=job["thread_id"],
                turn_id=job["turn_id"],
                job_id=job_id,
                idempotency_key=idempotency_key,
                options=options,
                contract=contract,
                expires_at=expires_at,
                interaction_id=interaction_id,
                now=now,
            )
            self._create_item_in_transaction(
                connection,
                thread_id=job["thread_id"],
                turn_id=job["turn_id"],
                kind=ItemKind.INTERACTION,
                content={
                    "interaction_id": interaction_id,
                    "kind": kind.value,
                    "prompt": prompt,
                    "options": interaction.options,
                    "contract": interaction.contract.model_dump(mode="json"),
                },
                status=ItemStatus.WAITING_HUMAN,
                item_id=interaction_id,
                idempotency_key=f"{interaction_id}:item-created",
                now=now,
            )
            self.events.append_in_transaction(
                connection,
                thread_id=job["thread_id"],
                turn_id=job["turn_id"],
                job_id=job_id,
                event_type="job.waiting_human",
                payload={
                    "attempt": job["attempt"],
                    "interaction_id": interaction_id,
                },
                idempotency_key=f"{job_id}:interaction:{interaction_id}:waiting",
                created_at=now,
            )
            connection.execute(
                "UPDATE jobs SET status = ?, lease_owner = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, heartbeat_at = NULL, checkpoint_json = COALESCE(?, checkpoint_json), updated_at = ? "
                "WHERE job_id = ?",
                (
                    JobStatus.WAITING_HUMAN.value,
                    None
                    if checkpoint is None
                    else json_dumps({**checkpoint, "interaction_id": interaction_id}),
                    _store_time(now),
                    job_id,
                ),
            )
            self._transition_turn_in_transaction(
                connection,
                row=turn,
                target=TurnStatus.WAITING_HUMAN,
                reason="interaction_requested",
                now=now,
            )
            projection = InteractionProjection.model_validate(interaction)
        self.jobs.retire_execution_permit(job_id, lease_token)
        return projection

    def get_interaction_mutation(
        self,
        interaction_id: str,
    ) -> InteractionMutationResponse:
        """Project one interaction and its related state from one DB snapshot.

        Replay/read paths must never assemble this public boundary from
        independent store calls: doing so can pair an Interaction with a Turn,
        Job, or event watermark from different SQLite snapshots.  Constructing
        the typed response here also guarantees that the internal DurableJob is
        reduced to the secret-free JobProjection before it leaves Runtime.
        """

        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    f"interaction {interaction_id!r} does not exist"
                )
            interaction = self.interactions._from_row(row)
            turn = (
                None
                if interaction.turn_id is None
                else self._turn_from_row(
                    self._require_turn(connection, interaction.turn_id)
                )
            )
            job = None
            if interaction.job_id is not None:
                job_row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id=?",
                    (interaction.job_id,),
                ).fetchone()
                if job_row is not None:
                    job = self.jobs._from_row(job_row)
            return InteractionMutationResponse(
                interaction=interaction,
                turn=turn,
                job=job,
                watermark=self.events.watermark(
                    interaction.thread_id,
                    connection,
                ),
            )

    def respond_interaction(
        self,
        interaction_id: str,
        response: InteractionResponse | dict[str, Any],
        *,
        client_request_id: str,
    ) -> InteractionMutationResponse:
        now = _utc_now()
        with self.jobs.control_transaction(
            scope="interaction_respond",
            subject=client_request_id,
        ) as connection:
            row = connection.execute(
                "SELECT kind FROM interactions WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if row is not None and row["kind"] == InteractionKind.CONNECTOR_LOGIN.value:
                raise ConflictError(
                    "connector login actions require the dedicated lifecycle endpoint"
                )
            interaction = self.interactions.respond_in_transaction(
                connection,
                interaction_id,
                response,
                client_request_id=client_request_id,
                now=now,
            )
            turn = (
                None
                if interaction.turn_id is None
                else self._turn_from_row(
                    self._require_turn(connection, interaction.turn_id)
                )
            )
            job = None
            if interaction.job_id is not None:
                job_row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (interaction.job_id,)
                ).fetchone()
                if job_row is not None:
                    job = self.jobs._from_row(job_row)
            return InteractionMutationResponse(
                interaction=interaction,
                turn=turn,
                job=job,
                watermark=self.events.watermark(interaction.thread_id, connection),
            )

    def cancel_connector_login_interaction(
        self,
        interaction_id: str,
        *,
        client_request_id: str,
    ) -> InteractionMutationResponse:
        now = _utc_now()
        with self.jobs.control_transaction(
            scope="connector_login_cancel",
            subject=client_request_id,
        ) as connection:
            row = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"interaction {interaction_id!r} does not exist")
            if row["kind"] != InteractionKind.CONNECTOR_LOGIN.value:
                raise ConflictError("interaction is not a connector login request")
            interaction = self.interactions.respond_in_transaction(
                connection,
                interaction_id,
                InteractionResponse(action_id="cancel", values={}),
                client_request_id=client_request_id,
                now=now,
            )
            turn = (
                None
                if interaction.turn_id is None
                else self._turn_from_row(
                    self._require_turn(connection, interaction.turn_id)
                )
            )
            job = None
            if interaction.job_id is not None:
                job_row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id=?",
                    (interaction.job_id,),
                ).fetchone()
                if job_row is not None:
                    job = self.jobs._from_row(job_row)
            return InteractionMutationResponse(
                interaction=interaction,
                turn=turn,
                job=job,
                watermark=self.events.watermark(interaction.thread_id, connection),
            )

    def resolve_connector_login_interaction(
        self,
        interaction_id: str,
        *,
        connector_id: str,
        refresh_request: SteerTurnRequest,
        client_request_id: str,
    ) -> tuple[InteractionMutationResponse, str]:
        """Atomically refresh Turn authority and resolve a successful login.

        The old execution batch remains immutable.  A non-UI input revision
        advances the durable authority range; the resumed Worker must create a
        new batch/config/Connector snapshot before another model request.
        """

        now = _utc_now()
        metadata = refresh_request.metadata.get("authority_refresh")
        if (
            not isinstance(metadata, dict)
            or metadata.get("kind") != "connector_login"
            or metadata.get("interaction_id") != interaction_id
            or metadata.get("connector_id") != connector_id
        ):
            raise ValueError("connector authority refresh metadata is invalid")
        with self.jobs.control_transaction(
            scope="connector_login_resolve",
            subject=client_request_id,
        ) as connection:
            row = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"interaction {interaction_id!r} does not exist")
            if row["kind"] != InteractionKind.CONNECTOR_LOGIN.value:
                raise ConflictError("interaction is not a connector login request")
            if row["turn_id"] is None or row["job_id"] is None:
                raise ConflictError("connector login is not attached to an Agent Turn")
            turn = self._require_turn(connection, str(row["turn_id"]))
            refresh_request = refresh_request.model_copy(
                update={
                    "agent_model_id": refresh_request.agent_model_id
                    or turn["agent_model_id"],
                    "image_model_id": refresh_request.image_model_id
                    or turn["image_model_id"],
                }
            )
            if (
                refresh_request.agent_model_id != turn["agent_model_id"]
                or refresh_request.image_model_id != turn["image_model_id"]
            ):
                raise ConflictError(
                    "connector authority refresh cannot change Turn models"
                )
            revision, _created = self.turn_inputs.append_authority_refresh_in_transaction(
                connection,
                thread_id=str(row["thread_id"]),
                turn_id=str(row["turn_id"]),
                request=refresh_request,
                created_at=now,
            )
            self.events.append_in_transaction(
                connection,
                thread_id=str(row["thread_id"]),
                turn_id=str(row["turn_id"]),
                job_id=str(row["job_id"]),
                event_type="turn.authority_refresh_requested",
                payload={
                    "source": "connector_login",
                    "connector_id": connector_id,
                    "interaction_id": interaction_id,
                    "revision_id": revision.revision_id,
                    "revision_ordinal": revision.ordinal,
                },
                causation_id=interaction_id,
                correlation_id=client_request_id,
                idempotency_key=f"{interaction_id}:connector-authority-refresh",
                created_at=now,
            )
            interaction = self.interactions.respond_in_transaction(
                connection,
                interaction_id,
                InteractionResponse(action_id="check_status", values={}),
                client_request_id=client_request_id,
                now=now,
            )
            updated_turn = self._turn_from_row(
                self._require_turn(connection, str(row["turn_id"]))
            )
            job_row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?",
                (row["job_id"],),
            ).fetchone()
            response = InteractionMutationResponse(
                interaction=interaction,
                turn=updated_turn,
                job=None if job_row is None else self.jobs._from_row(job_row),
                watermark=self.events.watermark(str(row["thread_id"]), connection),
            )
            return response, revision.revision_id

    def list_interactions(
        self, thread_id: str, *, pending_only: bool = True
    ) -> InteractionListResponse:
        with self.database.reader() as connection:
            self._require_thread(connection, thread_id)
            query = "SELECT * FROM interactions WHERE thread_id = ?"
            parameters: list[Any] = [thread_id]
            if pending_only:
                query += " AND status = ?"
                parameters.append(InteractionStatus.PENDING.value)
            query += " ORDER BY created_at, interaction_id"
            rows = connection.execute(query, parameters).fetchall()
            return InteractionListResponse(
                interactions=[self.interactions._from_row(row) for row in rows],
                watermark=self.events.watermark(thread_id, connection),
            )

    def fork_thread(
        self, thread_id: str, request: ForkThreadRequest
    ) -> ThreadProjection:
        now = _utc_now()
        request_fingerprint = hashlib.sha256(
            json_dumps(
                {
                    "source_thread_id": thread_id,
                    "from_turn_id": request.from_turn_id,
                    "title": request.title,
                    "metadata": request.metadata,
                }
            ).encode("utf-8")
        ).hexdigest()
        with self.jobs.control_transaction(
            scope="thread_fork",
            subject=request.client_request_id or request_fingerprint,
        ) as connection:
            if request.client_request_id:
                duplicate = connection.execute(
                    "SELECT * FROM threads WHERE client_request_id = ?",
                    (request.client_request_id,),
                ).fetchone()
                if duplicate is not None:
                    if duplicate["request_fingerprint"] != request_fingerprint:
                        raise ConflictError(
                            "fork client_request_id was reused with different input"
                        )
                    return self._thread_from_row(duplicate)
            source = self._require_thread(connection, thread_id)
            forked_from_turn_id = request.from_turn_id
            if forked_from_turn_id is not None:
                turn = self._require_turn(connection, forked_from_turn_id)
                if turn["thread_id"] != thread_id:
                    raise ConflictError("fork turn does not belong to the source thread")
                boundary = connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS seq FROM events "
                    "WHERE thread_id = ? AND turn_id = ?",
                    (thread_id, forked_from_turn_id),
                ).fetchone()["seq"]
            else:
                boundary = self.events.watermark(thread_id, connection)
            title = request.title or source["title"]
            metadata = {
                **json_loads(source["metadata_json"], {}),
                **request.metadata,
            }
            fork_id = new_id("thr")
            self.events.append_in_transaction(
                connection,
                thread_id=fork_id,
                event_type="thread.forked",
                payload={
                    "source_thread_id": thread_id,
                    "source_turn_id": forked_from_turn_id,
                    "source_seq": boundary,
                    "title": title,
                    "metadata": metadata,
                },
                causation_id=f"{thread_id}:{boundary}",
                correlation_id=request.client_request_id,
                idempotency_key="thread:forked",
                created_at=now,
            )
            timestamp = _store_time(now)
            connection.execute(
                """
                INSERT INTO threads(
                    thread_id, status, title, metadata_json,
                    client_request_id, request_fingerprint,
                    forked_from_thread_id, forked_from_turn_id,
                    forked_from_seq, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fork_id,
                    ThreadStatus.ACTIVE.value,
                    title,
                    json_dumps(metadata),
                    request.client_request_id,
                    request_fingerprint,
                    thread_id,
                    forked_from_turn_id,
                    boundary,
                    timestamp,
                    timestamp,
                ),
            )
            return self._thread_from_row(self._require_thread(connection, fork_id))

    def _replay_thread_history(
        self,
        connection: sqlite3.Connection,
        thread_id: str,
        through_seq: int,
        *,
        seen: set[str] | None = None,
    ) -> tuple[list[TurnProjection], list[ItemProjection]]:
        seen = set() if seen is None else seen
        if thread_id in seen:
            raise ConflictError("fork lineage contains a cycle")
        seen.add(thread_id)
        thread_row = self._require_thread(connection, thread_id)
        inherited_turns: list[TurnProjection] = []
        inherited_items: list[ItemProjection] = []
        if thread_row["forked_from_thread_id"] is not None:
            ancestor_turns, ancestor_items = self._replay_thread_history(
                connection,
                thread_row["forked_from_thread_id"],
                int(thread_row["forked_from_seq"] or 0),
                seen=seen,
            )
            inherited_turns.extend(ancestor_turns)
            inherited_items.extend(ancestor_items)

        rows = connection.execute(
            "SELECT * FROM events WHERE thread_id = ? AND seq <= ? ORDER BY seq",
            (thread_id, through_seq),
        ).fetchall()
        turns: dict[str, dict[str, Any]] = {}
        items: dict[str, dict[str, Any]] = {}
        for event_row in rows:
            event = self.events._from_row(event_row)
            payload = event.payload
            if event.event_type == "turn.accepted" and event.turn_id is not None:
                turns[event.turn_id] = {
                    "turn_id": event.turn_id,
                    "thread_id": thread_id,
                    "status": TurnStatus.ACCEPTED,
                    "input": payload.get("input", ""),
                    "agent_model_id": payload.get("agent_model_id"),
                    "image_model_id": payload.get("image_model_id"),
                    "client_message_id": event.client_message_id,
                    "metadata": payload.get("metadata", {}),
                    "inherited": True,
                    "terminal_reason": None,
                    "created_at": event.created_at,
                    "updated_at": event.created_at,
                }
            elif event.event_type == "turn.queued" and event.turn_id in turns:
                turns[event.turn_id]["status"] = TurnStatus.QUEUED
                turns[event.turn_id]["updated_at"] = event.created_at
            elif event.event_type == "turn.status_changed" and event.turn_id in turns:
                status = TurnStatus(payload["to"])
                turns[event.turn_id]["status"] = status
                turns[event.turn_id]["updated_at"] = event.created_at
                turns[event.turn_id]["terminal_reason"] = (
                    payload.get("reason") if status in TERMINAL_TURN_STATUSES else None
                )
            elif event.event_type == "item.created" and event.item_id is not None:
                kind = ItemKind(payload["kind"])
                status = ItemStatus(payload["status"])
                content = payload.get("content", {})
                if kind is ItemKind.TOOL_CALL:
                    try:
                        activity = PublicToolActivity.model_validate(content)
                    except ValueError:
                        raise ConflictError(
                            "Inherited Tool Item public activity is invalid"
                        ) from None
                    if activity.status != status.value:
                        raise ConflictError(
                            "Inherited Tool Item public status is inconsistent"
                        )
                    content = activity.model_dump(mode="json")
                items[event.item_id] = {
                    "item_id": event.item_id,
                    "thread_id": thread_id,
                    "turn_id": event.turn_id,
                    "kind": kind,
                    "status": status,
                    "content": content,
                    "inherited": True,
                    "created_at": event.created_at,
                    "updated_at": event.created_at,
                }
            elif event.event_type == "turn.steered" and event.item_id is not None:
                items[event.item_id] = {
                    "item_id": event.item_id,
                    "thread_id": thread_id,
                    "turn_id": event.turn_id,
                    "kind": ItemKind.MESSAGE,
                    "status": ItemStatus.COMPLETED,
                    "content": {
                        "role": "user",
                        "text": payload.get("input", ""),
                        "metadata": payload.get("metadata", {}),
                        "steer": True,
                    },
                    "inherited": True,
                    "created_at": event.created_at,
                    "updated_at": event.created_at,
                }
            elif event.event_type == "reasoning.replaced" and event.item_id is not None:
                previous_item_id = payload.get("previous_item_id")
                if previous_item_id in items:
                    previous_content = dict(items[previous_item_id]["content"])
                    previous_content.update(
                        {
                            "revision": payload.get(
                                "previous_revision", previous_content.get("revision", 1)
                            ),
                            "presentation": payload.get(
                                "previous_presentation", "archived"
                            ),
                            "archived_reason": "replaced_by_next_atom",
                        }
                    )
                    items[previous_item_id]["content"] = previous_content
                    items[previous_item_id]["status"] = ItemStatus.COMPLETED
                    items[previous_item_id]["updated_at"] = event.created_at
                items[event.item_id] = {
                    "item_id": event.item_id,
                    "thread_id": thread_id,
                    "turn_id": event.turn_id,
                    "kind": ItemKind.REASONING,
                    "status": ItemStatus.IN_PROGRESS,
                    "content": {
                        "channel": "reasoning_summary",
                        "atom_id": payload.get("atom_id", ""),
                        "text": payload.get("delta", ""),
                        "revision": payload.get("revision", 1),
                        "presentation": payload.get("presentation", "visible"),
                        "archived_reason": None,
                    },
                    "inherited": True,
                    "created_at": event.created_at,
                    "updated_at": event.created_at,
                }
            elif event.event_type == "reasoning.delta" and event.item_id in items:
                content = dict(items[event.item_id]["content"])
                content["text"] = str(content.get("text") or "") + str(
                    payload.get("delta") or ""
                )
                content["revision"] = payload.get(
                    "revision", int(content.get("revision") or 1) + 1
                )
                items[event.item_id]["content"] = content
                items[event.item_id]["updated_at"] = event.created_at
            elif event.event_type == "reasoning.archived" and event.item_id in items:
                content = dict(items[event.item_id]["content"])
                content.update(
                    {
                        "revision": payload.get(
                            "revision", int(content.get("revision") or 1) + 1
                        ),
                        "presentation": payload.get("presentation", "collapsed"),
                        "archived_reason": payload.get("reason"),
                    }
                )
                items[event.item_id]["content"] = content
                items[event.item_id]["updated_at"] = event.created_at
            elif event.event_type == "tool.result" and event.item_id in items:
                if items[event.item_id]["kind"] is not ItemKind.TOOL_CALL:
                    raise ConflictError(
                        "Inherited tool result references a non-Tool Item"
                    )
                try:
                    current = PublicToolActivity.model_validate(
                        items[event.item_id]["content"]
                    )
                    activity = PublicToolActivity.model_validate(
                        payload.get("activity")
                    )
                except ValueError:
                    raise ConflictError(
                        "Inherited tool result public activity is invalid"
                    ) from None
                if (
                    current.tool_call_id != activity.tool_call_id
                    or current.tool_id != activity.tool_id
                    or current.argument_sha256 != activity.argument_sha256
                ):
                    raise ConflictError(
                        "Inherited tool result public identity changed"
                    )
                items[event.item_id]["content"] = activity.model_dump(mode="json")
                items[event.item_id]["updated_at"] = event.created_at
            elif event.event_type == "item.status_changed" and event.item_id in items:
                target = ItemStatus(payload["to"])
                if items[event.item_id]["kind"] is ItemKind.TOOL_CALL:
                    try:
                        activity = PublicToolActivity.model_validate(
                            items[event.item_id]["content"]
                        )
                        activity = PublicToolActivityProjector.transition(
                            activity,
                            target,
                        )
                    except ValueError:
                        raise ConflictError(
                            "Inherited Tool Item public lifecycle is invalid"
                        ) from None
                    items[event.item_id]["content"] = activity.model_dump(
                        mode="json"
                    )
                items[event.item_id]["status"] = target
                items[event.item_id]["updated_at"] = event.created_at

        inherited_turns.extend(TurnProjection(**value) for value in turns.values())
        inherited_items.extend(ItemProjection(**value) for value in items.values())
        seen.remove(thread_id)
        return inherited_turns, inherited_items

    def projection(self, thread_id: str) -> ThreadProjectionResponse:
        with self.database.reader() as connection:
            thread = self._thread_from_row(self._require_thread(connection, thread_id))
            inherited_turns: list[TurnProjection] = []
            inherited_items: list[ItemProjection] = []
            if thread.forked_from_thread_id is not None:
                inherited_turns, inherited_items = self._replay_thread_history(
                    connection,
                    thread.forked_from_thread_id,
                    int(thread.forked_from_seq or 0),
                )
            # Event sequence is the conversation's ordering authority. Wall
            # clocks can legitimately tie (coarse filesystems, frozen clocks,
            # batch imports) and opaque identifiers are identity, not order.
            turn_rows = connection.execute(
                "SELECT turns.* FROM turns LEFT JOIN ("
                "SELECT turn_id, MIN(seq) AS first_seq FROM events "
                "WHERE thread_id = ? AND turn_id IS NOT NULL GROUP BY turn_id"
                ") AS event_order ON event_order.turn_id = turns.turn_id "
                "WHERE turns.thread_id = ? "
                "ORDER BY COALESCE(event_order.first_seq, 9223372036854775807), "
                "turns.created_at ASC, turns.turn_id ASC",
                (thread_id, thread_id),
            ).fetchall()
            item_rows = connection.execute(
                "SELECT items.* FROM items LEFT JOIN ("
                "SELECT item_id, MIN(seq) AS first_seq FROM events "
                "WHERE thread_id = ? AND item_id IS NOT NULL GROUP BY item_id"
                ") AS event_order ON event_order.item_id = items.item_id "
                "WHERE items.thread_id = ? "
                "ORDER BY COALESCE(event_order.first_seq, 9223372036854775807), "
                "items.created_at ASC, items.item_id ASC",
                (thread_id, thread_id),
            ).fetchall()
            job_rows = connection.execute(
                "SELECT jobs.* FROM jobs LEFT JOIN ("
                "SELECT job_id, MIN(seq) AS first_seq FROM events "
                "WHERE thread_id = ? AND job_id IS NOT NULL GROUP BY job_id"
                ") AS event_order ON event_order.job_id = jobs.job_id "
                "WHERE jobs.thread_id = ? "
                "ORDER BY COALESCE(event_order.first_seq, 9223372036854775807), "
                "jobs.created_at ASC, jobs.job_id ASC",
                (thread_id, thread_id),
            ).fetchall()
            interaction_rows = connection.execute(
                "SELECT interactions.* FROM interactions LEFT JOIN ("
                "SELECT item_id, MIN(seq) AS first_seq FROM events "
                "WHERE thread_id = ? AND item_id IS NOT NULL GROUP BY item_id"
                ") AS event_order ON event_order.item_id = interactions.interaction_id "
                "WHERE interactions.thread_id = ? AND interactions.status = ? "
                "ORDER BY COALESCE(event_order.first_seq, 9223372036854775807), "
                "interactions.created_at ASC, interactions.interaction_id ASC",
                (thread_id, thread_id, InteractionStatus.PENDING.value),
            ).fetchall()
            watermark = self.events.watermark(thread_id, connection)
        return ThreadProjectionResponse(
            thread=thread,
            turns=inherited_turns + [self._turn_from_row(row) for row in turn_rows],
            items=inherited_items + [self._item_from_row(row) for row in item_rows],
            jobs=[self.jobs._from_row(row) for row in job_rows],
            interactions=[
                self.interactions._from_row(row) for row in interaction_rows
            ],
            watermark=watermark,
        )
